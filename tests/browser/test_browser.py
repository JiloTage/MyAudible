"""Chromium integration tests; run with python -m unittest discover -s tests/browser -v.

Uses rendered page pixels and production extension modules. A fake local backend and
Chrome API make the side panel reproducible without a U-NEXT account or GPU/model.
"""
import asyncio
import base64
import functools
import json
import os
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import unittest

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "/tests/browser/fixtures/reader.html"
ARTIFACTS = Path(os.environ.get("BROWSER_ARTIFACTS", "/tmp/myaudible-browser-artifacts"))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(QuietHandler, directory=str(ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            **({"executable_path": os.environ["CHROMIUM_PATH"]} if os.environ.get("CHROMIUM_PATH") else {}),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        )
        self.context = await self.browser.new_context(viewport={"width": 1200, "height": 820})
        self.page = await self.context.new_page()
        await self.page.goto(self.origin + FIXTURE)
        await self.page.wait_for_function("Boolean(window.readerFixture)")
        await self.page.evaluate("""async () => {
            window.detectorModule = await import('/extension/lib/detector.mjs');
            window.captureModule = await import('/extension/lib/capture.mjs');
        }""")

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def data_url(self):
        screenshot = await self.page.screenshot(type="png")
        return "data:image/png;base64," + base64.b64encode(screenshot).decode()

    async def signature(self):
        return await self.page.evaluate("""async dataURL => {
            const {screenshotSignature} = window.captureModule;
            const signature = await screenshotSignature(dataURL);
            return Array.from(signature);
        }""", await self.data_url())

    async def open_panel(self):
        panel = await self.context.new_page()
        await panel.set_viewport_size({"width": 390, "height": 900})
        await panel.expose_function("__captureVisibleFixture", self.data_url)
        await panel.expose_function("__executeFixtureScript", self.execute_fixture_script)
        await panel.add_init_script(path=Path(__file__).parent / "chrome_stub.js")
        await panel.goto(self.origin + "/extension/panel.html")
        await panel.wait_for_function("document.querySelector('#health').textContent.includes('準備OK')", timeout=5000)
        await panel.wait_for_function("__mock.fetches.some(request => request.path === '/v1/voices')", timeout=5000)
        return panel

    async def execute_fixture_script(self, options):
        results = []
        for index, frame in enumerate(self.page.frames):
            document_id = f"fixture-{index}"
            target = options["target"]
            if target.get("documentIds") and document_id not in target["documentIds"]:
                continue
            if not target.get("allFrames") and not target.get("documentIds") and index:
                continue
            result = await frame.evaluate("""({source, args}) => source ? (0, eval)('(' + source + ')')(...args) : null""",
                                          {"source": options.get("source"), "args": options.get("args", [])})
            results.append({"frameId": index, "documentId": document_id, "result": result})
        return results

    async def request(self, panel, count=1):
        await panel.wait_for_function("""count => __backend.frames.filter(frame => frame.type === 'synthesize').length >= count""",
                                      arg=count, timeout=15000)
        return await panel.evaluate("""count => __backend.frames.filter(frame => frame.type === 'synthesize')[count - 1]""", count)

    async def configure_action(self, panel, action="click", x=85, y=45, scroll_y=85):
        await panel.locator("#afterReadSettings > summary").click()
        await panel.locator("#afterReadAction").select_option(action)
        await panel.locator("#afterReadX").fill(str(x))
        await panel.locator("#afterReadY").fill(str(y))
        if action == "scroll":
            await panel.locator("#afterReadScrollY").fill(str(scroll_y))

    async def complete_audio(self, panel, request_id, chunks=1):
        await panel.evaluate("""({id, chunks}) => {
            for (let index = 0; index < chunks; index++) __backend.emit(id, 'audio.chunk', {
                index, format: 'pcm_s16le', sample_rate: 48000, channels: 1,
                data_base64: btoa(String.fromCharCode(0).repeat(96000)),
            });
            __backend.emit(id, 'completed');
        }""", {"id": request_id, "chunks": chunks})
        await panel.wait_for_function("count => __mock.sources.length >= count && __mock.sources.at(-1).starts === 1", arg=chunks)

    async def test_same_url_single_glyph_repaint_at_default_sensitivity(self):
        initial_url = self.page.url
        before = await self.signature()
        mutation_count = await self.page.evaluate("readerFixture.mutations()")
        await self.page.evaluate("readerFixture.goTo(2)")
        after = await self.signature()
        result = await self.page.evaluate("""({before, after}) => {
            const {PageTurnDetector, visualDifference} = window.detectorModule;
            const detector = new PageTurnDetector();
            const states = [detector.observe(before, 0), detector.observe(before, 700),
                detector.observe(after, 1400), detector.observe(after, 2100),
                detector.observe(after, 2800)];
            return {states: states.map(event => event.type), difference: visualDifference(before, after)};
        }""", {"before": before, "after": after})
        self.assertEqual(self.page.url, initial_url)
        self.assertEqual(await self.page.evaluate("readerFixture.mutations()"), mutation_count)
        self.assertEqual(result["states"], ["settling", "page", "changing", "page", "unchanged"], result)

    async def test_animation_waits_for_stable_pixels_and_does_not_repeat(self):
        initial = await self.signature()
        await self.page.evaluate("""signature => {
            window.actualDetector = new detectorModule.PageTurnDetector();
            actualDetector.observe(signature, 0);
            actualDetector.observe(signature, 700);
            window.detectedPages = [];
        }""", initial)
        await self.page.evaluate("readerFixture.goTo(2, true)")
        started = asyncio.get_running_loop().time()
        states = []
        for _ in range(11):
            signature = await self.signature()
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            event = await self.page.evaluate("""({signature, now}) => actualDetector.observe(signature, now)""",
                                             {"signature": signature, "now": 1400 + elapsed})
            states.append((elapsed, event["type"]))
            await asyncio.sleep(0.18)
        pages = [time for time, kind in states if kind == "page"]
        self.assertEqual(len(pages), 1, states)
        self.assertGreaterEqual(pages[0], 650, states)
        self.assertEqual(states[-1][1], "unchanged", states)

    async def test_click_touch_and_keyboard_turn_canvas_without_navigation(self):
        initial_url = self.page.url
        mutation_count = await self.page.evaluate("readerFixture.mutations()")
        await self.page.locator("#next").click()
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 2)
        await self.page.keyboard.press("ArrowRight")
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 3)
        await self.page.locator("#book").evaluate("""canvas => {
            const start = new Touch({identifier: 1, target: canvas, clientX: 800, clientY: 300});
            const end = new Touch({identifier: 1, target: canvas, clientX: 400, clientY: 300});
            canvas.dispatchEvent(new TouchEvent('touchstart', {changedTouches: [start]}));
            canvas.dispatchEvent(new TouchEvent('touchend', {changedTouches: [end]}));
        }""")
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 4)
        self.assertEqual(self.page.url, initial_url)
        self.assertEqual(await self.page.evaluate("readerFixture.mutations()"), mutation_count)

    async def test_iframe_canvas_and_image_animation_detected_without_outer_mutation(self):
        initial_url = self.page.url
        mutation_count = await self.page.evaluate("readerFixture.mutations()")
        before = await self.signature()
        await self.page.evaluate("readerFixture.iframeChange()")
        await self.page.wait_for_timeout(60)
        iframe = await self.signature()
        await self.page.evaluate("readerFixture.imageChange()")
        await self.page.wait_for_timeout(650)
        picture = await self.signature()
        result = await self.page.evaluate("""({before, iframe, picture}) => {
            const d = new detectorModule.PageTurnDetector();
            return [d.observe(before, 0), d.observe(before, 700),
                d.observe(iframe, 1400), d.observe(iframe, 2100),
                d.observe(picture, 2800), d.observe(picture, 3500)].map(e => e.type);
        }""", {"before": before, "iframe": iframe, "picture": picture})
        self.assertEqual(result, ["settling", "page", "changing", "page", "changing", "page"])
        self.assertEqual(self.page.url, initial_url)
        self.assertEqual(await self.page.evaluate("readerFixture.mutations()"), mutation_count)

    async def test_capture_rejects_tab_switch_and_switch_back_race(self):
        data_url = await self.data_url()
        result = await self.page.evaluate("""async dataURL => {
            const {captureLockedTab, CaptureSafetyError} = captureModule;
            const cases = [];
            for (const mode of ['stable', 'already-other', 'during', 'switch-back', 'other-window']) {
                let active = {id: 1, windowId: 2};
                let epoch = 0;
                let captures = 0;
                if (mode === 'already-other') active = {id: 9, windowId: 2};
                try {
                    const result = await captureLockedTab({tabId: 1, windowId: 2,
                        queryActive: async () => active, revision: () => epoch,
                        capture: async () => {
                            captures++;
                            if (mode === 'during') { active = {id: 9, windowId: 2}; epoch++; }
                            if (mode === 'switch-back') epoch += 2;
                            if (mode === 'other-window') { active = {id: 1, windowId: 3}; epoch++; }
                            return dataURL;
                        }});
                    cases.push({mode, allowed: result === dataURL, captures});
                } catch (error) {
                    cases.push({mode, allowed: false, safe: error instanceof CaptureSafetyError, captures});
                }
            }
            return cases;
        }""", data_url)
        self.assertTrue(result[0]["allowed"])
        for case in result[1:]:
            self.assertFalse(case["allowed"], case)
            self.assertTrue(case["safe"], case)
        self.assertEqual(result[1]["captures"], 0, result)

    async def test_pcm_stream_playback_and_cancel_releases_sources(self):
        result = await self.page.evaluate("""async () => {
            const {AudioPlayer} = await import('/extension/lib/audio.mjs');
            const states = [];
            const player = new AudioPlayer({onState: state => states.push(state)});
            await player.unlock();
            const raw = new Uint8Array(48000 * 2); // One second of silence; no generated voice.
            let binary = '';
            for (const value of raw) binary += String.fromCharCode(value);
            const chunk = {format: 'pcm_s16le', sample_rate: 48000, channels: 1, data_base64: btoa(binary)};
            await player.enqueue(chunk);
            await player.enqueue(chunk);
            const queued = player.sources.size;
            const previousGeneration = player.generation;
            player.stop();
            const stopped = player.sources.size;
            await player.close();
            return {queued, stopped, generationChanged: player.generation > previousGeneration,
                contextState: player.context.state, states};
        }""")
        self.assertEqual(result["queued"], 2, result)
        self.assertEqual(result["stopped"], 0, result)
        self.assertTrue(result["generationChanged"], result)
        self.assertEqual(result["contextState"], "closed", result)

    async def test_audio_backpressure_and_pending_decode_cancel_release_resources(self):
        result = await self.page.evaluate("""async () => {
            const {AudioPlayer} = await import('/extension/lib/audio.mjs');
            let starts = 0;
            let resolveDecode;
            const context = {state: 'running', currentTime: 0, destination: {},
                resume: async () => {}, close: async () => {},
                createBuffer: (channels, frames, rate) => ({duration: frames / rate, copyToChannel() {}}),
                createBufferSource: () => ({playbackRate: {value: 1}, connect() {},
                    start() { starts++; }, stop() {}, disconnect() {}}),
                decodeAudioData: () => new Promise(resolve => resolveDecode = resolve),
            };
            const player = new AudioPlayer({contextFactory: () => context, maxAheadSeconds: 0.02});
            await player.unlock();
            const chunk = {format: 'pcm_s16le', channels: 1, sample_rate: 8000,
                data_base64: btoa('\0'.repeat(16000))};
            await player.enqueue(chunk);
            const waiting = player.enqueue(chunk);
            await new Promise(resolve => setTimeout(resolve, 30));
            const blocked = player.waiters.size === 1 && starts === 1;
            player.stop();
            const waitingAccepted = await waiting;
            const waitersAfter = player.waiters.size;
            const decode = player.enqueue({format: 'wav', data_base64: btoa('fake-wav-boundary')});
            await new Promise(resolve => setTimeout(resolve, 0));
            player.stop();
            resolveDecode({duration: 1});
            const decodeAccepted = await decode;
            return {blocked, waitingAccepted, waitersAfter, decodeAccepted,
                starts, sources: player.sources.size, bytes: player.queuedEncodedBytes};
        }""")
        self.assertTrue(result["blocked"], result)
        self.assertFalse(result["waitingAccepted"], result)
        self.assertEqual(result["waitersAfter"], 0, result)
        self.assertFalse(result["decodeAccepted"], result)
        self.assertEqual(result["starts"], 1, result)
        self.assertEqual(result["sources"], 0, result)
        self.assertEqual(result["bytes"], 0, result)

    async def test_panel_manual_capture_authentication_stream_and_late_events(self):
        panel = await self.open_panel()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        await panel.screenshot(path=str(ARTIFACTS / "panel.png"), full_page=True)
        await self.page.screenshot(path=str(ARTIFACTS / "reader.png"))
        # The first manual read must select its tab and submit, without a second click.
        await panel.locator("#readOnce").click()
        first = await self.request(panel)
        self.assertEqual(first["input"]["type"], "image")
        self.assertTrue(base64.b64decode(first["input"]["data_base64"]).startswith(b"\x89PNG"))
        auth = await panel.evaluate("__backend.frames[0]")
        self.assertEqual(auth, {"type": "auth", "token": "TEST-LOCAL-TOKEN-WITH-32-CHARACTERS"})
        self.assertEqual(await panel.evaluate("__backend.sockets[0].url"), "ws://127.0.0.1:8765/v1/realtime")
        await panel.evaluate("""id => {
            __backend.emit(id, 'transcript', {text: '読み取り結果です。'});
            __backend.emit(id, 'audio.chunk', {index: 0, format: 'pcm_s16le',
                sample_rate: 48000, channels: 1, data_base64: btoa('\0'.repeat(96000))});
        }""", first["id"])
        await panel.wait_for_function("__mock.sources.length === 1 && __mock.sources[0].starts === 1")
        await panel.wait_for_function("__backend.frames.some(frame => frame.type === 'audio.ack')")
        self.assertEqual(await panel.evaluate("__backend.frames.find(frame => frame.type === 'audio.ack')"),
                         {"type": "audio.ack", "id": first["id"], "index": 0})
        self.assertEqual(await panel.locator("#transcript").text_content(), "読み取り結果です。")
        await self.page.evaluate("readerFixture.goTo(2)")
        await panel.locator("#readOnce").click()
        second = await self.request(panel, 2)
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotEqual(second["input"]["data_base64"], first["input"]["data_base64"])
        self.assertTrue(await panel.evaluate("""id => __backend.frames.some(frame => frame.type === 'cancel' && frame.id === id)""", first["id"]))
        await panel.evaluate("""({oldId, currentId}) => {
            __backend.emit(currentId, 'transcript', {text: '次のページ'});
            __backend.emit(oldId, 'transcript', {text: '古いページ'});
            __backend.emit(oldId, 'audio.chunk', {index: 1, format: 'pcm_s16le',
                sample_rate: 48000, channels: 1, data_base64: btoa('\0'.repeat(96000))});
        }""", {"oldId": first["id"], "currentId": second["id"]})
        await panel.wait_for_timeout(50)
        self.assertEqual(await panel.locator("#transcript").text_content(), "次のページ")
        self.assertEqual(await panel.evaluate("__mock.sources.length"), 1)
        self.assertEqual(await panel.evaluate("__backend.frames.filter(frame => frame.type === 'audio.ack').length"), 1)
        self.assertGreaterEqual(await panel.evaluate("__mock.sources[0].stops"), 1)
        self.assertGreaterEqual(await panel.evaluate("__mock.sources[0].disconnects"), 1)
        await panel.locator("#stop").click()
        self.assertTrue(await panel.evaluate("""id => __backend.frames.some(frame => frame.type === 'cancel' && frame.id === id)""", second["id"]))
        await panel.evaluate("dispatchEvent(new PageTransitionEvent('pagehide'))")
        await panel.wait_for_function("__mock.contexts.every(context => context.state === 'closed')")
        self.assertEqual(await panel.locator("#transcript").text_content(), "")
        self.assertEqual(await panel.evaluate("__backend.sockets[0].readyState"), 3)

    async def test_panel_pauses_unfocused_window_and_rejects_capture_focus_race(self):
        panel = await self.open_panel()
        await panel.locator("#startMonitor").click()
        first = await self.request(panel)
        await panel.evaluate("__mock.focusWindow(3)")
        capture_count = await panel.evaluate("__mock.captures")
        await panel.wait_for_timeout(1400)
        self.assertEqual(await panel.evaluate("__mock.captures"), capture_count)
        self.assertTrue(await panel.evaluate("""id => __backend.frames.some(frame => frame.type === 'cancel' && frame.id === id)""", first["id"]))
        await panel.locator("#stop").click()
        await panel.evaluate("__mock.focusWindow(2)")
        await panel.evaluate("__mock.captureRace = () => { __mock.focusWindow(3); __mock.focusWindow(2); }")
        await panel.locator("#readOnce").click()
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('切り替わ')")
        self.assertEqual(await panel.evaluate("__backend.frames.filter(frame => frame.type === 'synthesize').length"), 1)

    async def test_panel_canvas_turn_replaces_pending_synthesis_and_pauses_other_tab(self):
        panel = await self.open_panel()
        await panel.locator("#startMonitor").click()
        first = await self.request(panel)
        # Backend intentionally leaves this request pending, so replacement must cancel it.
        await self.page.locator("#next").click()
        second = await self.request(panel, 2)
        self.assertNotEqual(first["input"]["data_base64"], second["input"]["data_base64"])
        frames = await panel.evaluate("__backend.frames")
        cancel_index = next(i for i, frame in enumerate(frames) if frame["type"] == "cancel" and frame["id"] == first["id"])
        second_index = next(i for i, frame in enumerate(frames) if frame["type"] == "synthesize" and frame["id"] == second["id"])
        self.assertLess(cancel_index, second_index)
        await panel.evaluate("__mock.activate(9)")
        captures_before = await panel.evaluate("__mock.captures")
        await self.page.evaluate("readerFixture.goTo(3)")
        await panel.wait_for_timeout(1600)
        self.assertEqual(await panel.evaluate("__mock.captures"), captures_before)
        self.assertEqual(await panel.evaluate("__backend.frames.filter(frame => frame.type === 'synthesize').length"), 2)
        self.assertIn("一時停止", await panel.locator("#status").text_content())
        await panel.locator("#stop").click()

    async def test_native_start_connects_and_ignores_duplicate_clicks(self):
        panel = await self.open_panel()
        await panel.locator("#token").fill("old-token")
        await panel.evaluate("__mock.nativePending = true")
        await panel.locator("#startServer").click()
        await panel.wait_for_function("__mock.nativeCalls.length === 1")
        self.assertTrue(await panel.locator("#startServer").is_disabled())
        await panel.locator("#startServer").evaluate("element => element.dispatchEvent(new Event('click'))")
        self.assertEqual(await panel.evaluate("__mock.nativeCalls.length"), 1)
        await panel.evaluate("__mock.resolveNative()")
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('起動して接続しました')")
        self.assertFalse(await panel.locator("#startServer").is_disabled())
        self.assertEqual(await panel.locator("#token").input_value(), "TEST-LOCAL-TOKEN-WITH-32-CHARACTERS")
        self.assertEqual(await panel.evaluate("__mock.nativeCalls"), [{"host": "com.myaudible.launcher", "message": {"type": "start"}}])
        saved = json.loads(await panel.evaluate("sessionStorage.getItem('test.chrome.storage')"))
        self.assertEqual(saved["token"], "TEST-LOCAL-TOKEN-WITH-32-CHARACTERS")
        await panel.locator("#textInput").fill("起動後の読み上げ")
        await panel.locator("#sendText").click()
        self.assertEqual((await self.request(panel))["input"]["text"], "起動後の読み上げ")

    async def test_native_start_failure_allows_retry_and_reuses_running_server(self):
        panel = await self.open_panel()
        await panel.evaluate("__mock.nativeError = 'Specified native messaging host not found.'")
        await panel.locator("#startServer").click()
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('install-native-host.ps1')")
        self.assertFalse(await panel.locator("#startServer").is_disabled())
        await panel.evaluate("__mock.nativeError = null; __mock.nativeResponse = {ok: false, error: '8765は使用中です。'}")
        await panel.locator("#startServer").click()
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('8765は使用中')")
        self.assertFalse(await panel.locator("#startServer").is_disabled())
        await panel.evaluate("__mock.nativeResponse = {ok: true, url: 'http://127.0.0.1:8765', token: 'TEST-LOCAL-TOKEN-WITH-32-CHARACTERS', already_running: true}")
        await panel.locator("#startServer").click()
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('起動済みのサーバー')")
        self.assertFalse(await panel.locator("#startServer").is_disabled())

    async def test_native_start_rejects_remote_connection_information(self):
        panel = await self.open_panel()
        previous = await panel.locator("#token").input_value()
        await panel.evaluate("__mock.nativeResponse = {ok: true, url: 'https://example.com', token: 'TEST-LOCAL-TOKEN-WITH-32-CHARACTERS'}")
        await panel.locator("#startServer").click()
        await panel.wait_for_function("document.querySelector('#status').textContent.includes('接続先は')")
        self.assertEqual(await panel.locator("#token").input_value(), previous)
        self.assertFalse(await panel.locator("#startServer").is_disabled())

    async def test_panel_text_reference_registration_reload_and_storage_boundaries(self):
        panel = await self.open_panel()
        await panel.locator("#textInput").fill("こんにちは。テキストから読み上げます。")
        await panel.locator("#sendText").click()
        request = await self.request(panel)
        self.assertEqual(request["input"], {"type": "text", "text": "こんにちは。テキストから読み上げます。"})
        self.assertNotIn("chunk_chars", request["options"])
        self.assertEqual(await panel.evaluate("__mock.captures"), 0)
        await panel.locator("#voiceName").evaluate("element => element.closest('details').open = true")
        await panel.locator("#voiceName").fill("テスト参照音声")
        await panel.locator("#voiceFile").set_input_files({"name": "reference.wav", "mimeType": "audio/wav", "buffer": b"test-reference-audio"})
        await panel.locator("#uploadVoice").click()
        await panel.wait_for_function("document.querySelector('#voice').value === 'voice-1'", timeout=5000)
        self.assertEqual(await panel.locator("#voice option:checked").text_content(), "テスト参照音声")
        await panel.reload()
        await panel.wait_for_function("document.querySelector('#voice').value === 'voice-1'", timeout=5000)
        self.assertEqual(await panel.locator("#textInput").input_value(), "")
        await panel.locator("#textInput").fill("登録済み参照音声を使用")
        await panel.locator("#sendText").click()
        request = await self.request(panel)
        self.assertEqual(request["options"]["voice_id"], "voice-1")
        stored = await panel.evaluate("sessionStorage.getItem('test.chrome.storage')")
        self.assertNotIn("登録済み参照音声を使用", stored)
        self.assertNotIn("data_base64", stored)
        self.assertNotIn("transcript", json.loads(stored))
        self.assertNotIn("textInput", json.loads(stored))


    async def test_speed_is_sent_to_synthesis_and_browser_plays_at_normal_rate(self):
        panel = await self.open_panel()
        await panel.locator("#rate").select_option("1.5")
        await panel.locator("#textInput").fill("速度の確認です。")
        await panel.locator("#sendText").click()
        request = await self.request(panel)
        self.assertEqual(request["options"]["speed"], 1.5)
        await panel.evaluate("""id => __backend.emit(id, 'audio.chunk', {
            index: 0, format: 'pcm_s16le', sample_rate: 48000,
            channels: 1, data_base64: btoa(String.fromCharCode(0).repeat(96000))
        })""", request["id"])
        await panel.wait_for_function("__mock.sources.length === 1 && __mock.sources[0].starts === 1")
        self.assertEqual(await panel.evaluate("__mock.sources[0].playbackRate.value"), 1)
        await panel.reload()
        await panel.wait_for_function("document.querySelector('#rate').value === '1.5'")

    async def test_actual_audio_completion_waits_for_playback(self):
        result = await self.page.evaluate("""async () => {
            const {AudioPlayer} = await import('/extension/lib/audio.mjs');
            const player = new AudioPlayer();
            await player.unlock();
            const chunk = {format: 'pcm_s16le', sample_rate: 48000, channels: 1,
                data_base64: btoa(String.fromCharCode(0).repeat(9600))};
            await player.enqueue(chunk); await player.enqueue(chunk);
            const start = performance.now();
            const finished = await player.waitUntilFinished();
            const result = {finished, elapsed: performance.now() - start, sources: player.sources.size};
            await player.close(); return result;
        }""")
        self.assertTrue(result["finished"], result)
        self.assertGreater(result["elapsed"], 150, result)
        self.assertEqual(result["sources"], 0, result)

    async def test_auto_click_waits_for_all_audio_then_reads_the_changed_page_once(self):
        panel = await self.open_panel()
        await self.configure_action(panel)
        await panel.locator("#startMonitor").click()
        await self.page.bring_to_front()
        request = await self.request(panel)
        await self.complete_audio(panel, request["id"], chunks=2)
        await panel.evaluate("id => __backend.emit(id, 'completed')", request["id"])
        await panel.wait_for_timeout(1400)
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 1)
        await panel.evaluate("__mock.sources[0].finish()")
        await panel.wait_for_timeout(1400)
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 1)
        await panel.evaluate("__mock.sources[1].finish()")
        await self.page.wait_for_function("readerFixture.page() === 2", timeout=6000)
        next_request = await self.request(panel, 2)
        self.assertNotEqual(next_request["id"], request["id"])
        await panel.evaluate("id => __backend.emit(id, 'completed')", request["id"])
        await panel.wait_for_timeout(1400)
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 2)
        self.assertEqual(await panel.evaluate("__mock.executions.filter(item => item.func === 'actAtPoint').length"), 1)
        await panel.locator("#stop").click()

    async def test_auto_actions_are_cancelled_by_stop_focus_page_error_and_settings_changes(self):
        for reason in ("stop", "tab", "window", "page", "error", "cancelled", "settings", "close"):
            with self.subTest(reason=reason):
                await self.page.evaluate("readerFixture.goTo(1)")
                panel = await self.open_panel()
                await self.configure_action(panel)
                await panel.locator("#startMonitor").click()
                await self.page.bring_to_front()
                request = await self.request(panel)
                await self.complete_audio(panel, request["id"])
                if reason == "stop":
                    await panel.locator("#stop").click()
                elif reason == "tab":
                    await panel.evaluate("__mock.activate(9)")
                elif reason == "window":
                    await panel.evaluate("__mock.focusWindow(3)")
                elif reason == "page":
                    await self.page.evaluate("readerFixture.goTo(2)")
                elif reason == "error":
                    await panel.evaluate("id => __backend.emit(id, 'error', {message: 'test error'})", request["id"])
                elif reason == "cancelled":
                    await panel.evaluate("id => __backend.emit(id, 'cancelled')", request["id"])
                elif reason == "settings":
                    await panel.locator("#afterReadAction").select_option("none")
                else:
                    await panel.evaluate("dispatchEvent(new PageTransitionEvent('pagehide'))")
                await panel.evaluate("__mock.sources[0].finish()")
                await panel.wait_for_timeout(1600)
                self.assertEqual(await panel.evaluate("__mock.executions.filter(item => item.func === 'actAtPoint').length"), 0, reason)
                self.assertEqual(await self.page.evaluate("readerFixture.page()"), 2 if reason == "page" else 1, reason)
                await panel.close()

    async def test_auto_scroll_moves_the_window_or_the_container_at_the_selected_point(self):
        for container in (False, True):
            with self.subTest(container=container):
                await self.page.evaluate("""container => {
                    document.querySelector('#scroll-test')?.remove(); window.scrollTo(0, 0);
                    const box = document.createElement('div'); box.id = 'scroll-test';
                    box.style.cssText = container ? 'position:fixed;left:400px;top:150px;width:400px;height:400px;overflow:auto;background:white'
                                                  : 'height:2400px';
                    box.innerHTML = '<div style="height:2400px;background:linear-gradient(white,black)">スクロール本文</div>';
                    document.body.append(box);
                }""", container)
                panel = await self.open_panel()
                await self.configure_action(panel, "scroll", x=50 if container else 95, y=50, scroll_y=50)
                await panel.locator("#startMonitor").click()
                await self.page.bring_to_front()
                request = await self.request(panel)
                await self.complete_audio(panel, request["id"])
                self.assertEqual(await self.page.evaluate("window.scrollY + document.querySelector('#scroll-test').scrollTop"), 0)
                await panel.evaluate("__mock.sources[0].finish()")
                await self.page.wait_for_function("window.scrollY + document.querySelector('#scroll-test').scrollTop > 100", timeout=6000)
                if container:
                    self.assertEqual(await self.page.evaluate("window.scrollY"), 0)
                else:
                    self.assertEqual(await self.page.evaluate("document.querySelector('#scroll-test').scrollTop"), 0)
                await panel.locator("#stop").click()
                await panel.close()

    async def test_actions_reach_cross_origin_frames_and_coordinates_are_picked_and_restored(self):
        await self.page.evaluate("""url => {
            const frame = document.createElement('iframe'); frame.id = 'action-frame';
            frame.src = url; frame.style.cssText = 'position:fixed;left:400px;top:100px;width:350px;height:250px;border:0';
            document.body.append(frame);
        }""", self.origin.replace("127.0.0.1", "localhost") + "/tests/browser/fixtures/action-frame.html")
        await self.page.locator("#action-frame").content_frame.locator("#next").wait_for()
        panel = await self.open_panel()
        await self.configure_action(panel)
        await panel.locator("#pickActionPoint").click()
        await panel.locator("#previewCanvas").wait_for(state="visible")
        canvas = await panel.locator("#previewCanvas").evaluate("node => ({width: node.clientWidth, height: node.clientHeight})")
        await panel.locator("#previewCanvas").click(position={"x": canvas["width"] * 575 / 1200, "y": canvas["height"] * 150 / 820})
        self.assertAlmostEqual(float(await panel.locator("#afterReadX").input_value()), 47.9, delta=.3)
        self.assertAlmostEqual(float(await panel.locator("#afterReadY").input_value()), 18.3, delta=.3)
        frame = self.page.frames[-1]
        self.assertEqual(await frame.evaluate("window.clicks"), 0)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        await panel.screenshot(path=str(ARTIFACTS / "after-read-action.png"), full_page=True)
        await panel.reload()
        await panel.wait_for_function("document.querySelector('#afterReadAction').value === 'click'")
        self.assertAlmostEqual(float(await panel.locator("#afterReadX").input_value()), 47.9, delta=.3)
        await self.page.bring_to_front()
        for action, y in (("click", 150), ("scroll", 250)):
            performed = await panel.evaluate("""async ({type, y}) => {
                const {executePageAction} = await import('/extension/lib/page-action.mjs');
                return executePageAction({tabId: 1, documentId: 'fixture-0', isCurrent: () => true,
                    action: {type, x: 575 / 1200 * 100, y: y / 820 * 100, scrollX: 0, scrollY: 50}});
            }""", {"type": action, "y": y})
            self.assertTrue(performed)
        self.assertEqual(await frame.evaluate("window.clicks"), 1)
        self.assertGreater(await frame.locator("#scroller").evaluate("node => node.scrollTop"), 100)

    async def test_manual_read_does_not_advance_even_when_the_action_is_enabled(self):
        panel = await self.open_panel()
        await self.configure_action(panel)
        await panel.locator("#readOnce").click()
        request = await self.request(panel)
        await self.complete_audio(panel, request["id"])
        await panel.evaluate("__mock.sources[0].finish()")
        await panel.wait_for_timeout(1400)
        self.assertEqual(await self.page.evaluate("readerFixture.page()"), 1)
        self.assertEqual(await panel.evaluate("__mock.executions.filter(item => item.func === 'actAtPoint').length"), 0)

    async def test_a_click_without_a_page_change_does_not_repeat_or_reread(self):
        await self.page.evaluate("""() => {
            const button = document.createElement('button'); button.id = 'no-change'; button.textContent = '次へ';
            button.style.cssText = 'position:fixed;left:80%;top:35%;width:15%;height:30%';
            window.actionClicks = 0; button.onclick = () => { window.actionClicks++; };
            document.body.append(button);
        }""")
        panel = await self.open_panel()
        await self.configure_action(panel)
        await panel.locator("#startMonitor").click()
        await self.page.bring_to_front()
        request = await self.request(panel)
        await self.complete_audio(panel, request["id"])
        await panel.evaluate("__mock.sources[0].finish()")
        await self.page.wait_for_function("window.actionClicks === 1", timeout=6000)
        await panel.wait_for_timeout(2800)
        self.assertEqual(await self.page.evaluate("window.actionClicks"), 1)
        self.assertEqual(await panel.evaluate("__backend.frames.filter(frame => frame.type === 'synthesize').length"), 1)
        await panel.locator("#stop").click()


if __name__ == "__main__":
    unittest.main(verbosity=2)
