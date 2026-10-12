"""Opt-in Windows test using the registered launcher and real local servers.

NATIVE_CHROMIUM_PATH must point to Chrome for Testing or Chromium that accepts
--load-extension. This test uses a fresh profile and does not open the user's Chrome.
"""

import json
import functools
import os
from pathlib import Path
import re
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import unittest

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[2]


def fresh_windows_environment():
    """Read Windows' new user environment without the old Codex process values."""
    import ctypes
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(wintypes.HANDLE(-1), 0x000A, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        block = ctypes.c_void_p()
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            environment = {}
            cursor = block.value
            while entry := ctypes.wstring_at(cursor):
                name, _, value = entry.partition("=")
                if name:
                    environment[name] = value
                cursor += len(entry.encode("utf-16-le")) + 2
            return environment
        finally:
            userenv.DestroyEnvironmentBlock(block)
    finally:
        kernel.CloseHandle(token)


@unittest.skipUnless(os.name == "nt" and os.environ.get("NATIVE_CHROMIUM_PATH"),
                     "Requires an installed Windows launcher and NATIVE_CHROMIUM_PATH")
class NativeMessagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_host_connects_panel_and_generates_real_audio(self):
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Google\Chrome\NativeMessagingHosts\com.myaudible.launcher") as key:
            manifest_path = Path(winreg.QueryValueEx(key, "")[0])
        # A packaged installer can see redirected AppData files that Chrome cannot.
        self.assertEqual(manifest_path, manifest_path.resolve(strict=True))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        host_path = Path(manifest["path"])
        self.assertEqual(host_path, host_path.resolve(strict=True))
        environment = fresh_windows_environment()
        command_shell = next((value for key, value in environment.items() if key.upper() == "COMSPEC"), "")
        self.assertTrue(command_shell, "Windows ComSpec is missing; rerun install-native-host.ps1 and restart Chrome")
        self.assertTrue(Path(command_shell).is_file())
        extension_id = manifest["allowed_origins"][0].split("//")[1].rstrip("/")
        artifacts = ROOT / "test-results/native-messaging"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="profile-", dir=artifacts) as profile:
            self.assertTrue(Path(profile).resolve().is_relative_to(artifacts.resolve()))
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    profile, executable_path=os.environ["NATIVE_CHROMIUM_PATH"], headless=True,
                    env=environment,
                    args=[f"--load-extension={ROOT / 'extension'}",
                          f"--disable-extensions-except={ROOT / 'extension'}",
                          "--autoplay-policy=no-user-gesture-required"],
                    ignore_default_args=["--disable-extensions"],
                )
                page = None
                token = ''
                try:
                    if not context.service_workers:
                        await context.wait_for_event("serviceworker", timeout=15000)
                    self.assertIn(extension_id, context.service_workers[0].url)
                    page = await context.new_page()
                    await page.goto(f"chrome-extension://{extension_id}/panel.html")
                    response = await page.evaluate("""async () => {
                        try {
                            const result = await chrome.runtime.sendNativeMessage('com.myaudible.launcher', {type: 'start'});
                            return {ok: result.ok, error: result.error, token_length: result.token?.length, url: result.url};
                        } catch (error) { return {ok: false, error: error.message}; }
                    }""")
                    self.assertTrue(response["ok"], response.get("error"))
                    self.assertEqual(response["url"], "http://127.0.0.1:8765")
                    self.assertGreaterEqual(response["token_length"], 24)

                    await page.locator("#startServer").click()
                    await expect(page.locator("#startServer")).to_be_enabled(timeout=360000)
                    self.assertIn("接続しました", await page.locator("#status").inner_text())
                    token = await page.locator("#token").input_value()
                    self.assertGreaterEqual(len(token), 24)
                    await page.reload()
                    await expect(page.locator("#token")).to_have_value(re.compile(r".{24,}"))
                    text = "こん にちは。\n拡張機能からの　接続を確認しました。"
                    await page.locator("#textInput").fill(text)
                    await page.locator("#steps").fill("16")
                    await page.locator("#rate").select_option("1.5")
                    await page.locator("#sendText").click()
                    await expect(page.locator("#status")).to_have_text("生成完了。", timeout=120000)
                    self.assertEqual(await page.locator("#transcript").text_content(), text)

                    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT / "tests/browser/fixtures"))
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        reader = await context.new_page()
                        await reader.goto(f"http://127.0.0.1:{server.server_port}/permission-reader.html")
                        await reader.bring_to_front()
                        await reader.screenshot()
                        capture = await page.evaluate("""async () => {
                            try {
                                const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
                                const data = await chrome.tabs.captureVisibleTab(tab.windowId, {format: 'png'});
                                return {ok: data.startsWith('data:image/png;base64,'), length: data.length};
                            } catch (error) { return {ok: false, error: error.message}; }
                        }""")
                        self.assertTrue(capture["ok"], capture.get("error"))
                        self.assertGreater(capture["length"], 1000)
                        await reader.wait_for_timeout(600)
                        # A panel button click must work without a preceding toolbar action.
                        await page.evaluate("""() => {
                            document.querySelector('#readingOrder').value = 'horizontal';
                            document.querySelector('#transcript').textContent = '';
                            document.querySelector('#readOnce').click();
                        }""")
                        await expect(page.locator("#transcript")).to_contain_text("読み取り", timeout=120000)
                        await expect(page.locator("#status")).to_have_text("生成完了。", timeout=120000)

                        await page.locator("#stop").click()
                        await reader.evaluate("""() => {
                            document.body.innerHTML = '<p id="text">今日は晴れです。</p><button id="next">次へ</button>';
                            document.querySelector('#next').style.cssText = 'position:fixed;left:80%;top:30%;width:15%;height:40%';
                            window.nextClicks = 0;
                            document.querySelector('#next').onclick = () => {
                                window.nextClicks++;
                                document.querySelector('#text').textContent = '明日は雨です。';
                            };
                        }""")
                        await page.locator("#afterReadSettings > summary").click()
                        await page.locator("#afterReadAction").select_option("click")
                        await page.locator("#afterReadX").fill("90")
                        await page.locator("#afterReadY").fill("50")
                        await page.locator("#steps").fill("4")
                        await page.locator("#startMonitor").click()
                        await reader.bring_to_front()
                        await reader.wait_for_function("window.nextClicks === 1", timeout=120000)
                        self.assertEqual(await page.locator("#playback").text_content(), '')
                        await expect(page.locator("#transcript")).to_contain_text("明日は", timeout=120000)
                        await page.locator("#stop").click()

                        await reader.evaluate("""url => {
                            document.body.innerHTML = '';
                            const frame = document.createElement('iframe'); frame.id = 'action-frame'; frame.src = url;
                            frame.style.cssText = 'position:fixed;left:20%;top:20%;width:60%;height:50vh;border:0';
                            document.body.append(frame);
                        }""", f"http://localhost:{server.server_port}/action-frame.html")
                        child = reader.locator("#action-frame").content_frame
                        await child.locator("#next").wait_for()
                        await reader.bring_to_front()
                        results = await page.evaluate("""async () => {
                            const {executePageAction} = await import('./lib/page-action.mjs');
                            const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
                            const [document] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => true});
                            const result = [];
                            for (const [type, y] of [['click', 24], ['scroll', 43]]) result.push(await executePageAction({
                                tabId: tab.id, documentId: document.documentId, isCurrent: () => true,
                                action: {type, x: 50, y, scrollX: 0, scrollY: 50},
                            }));
                            return result;
                        }""")
                        self.assertEqual(results, [True, True])
                        self.assertEqual(await child.locator("#next").evaluate("node => node.ownerDocument.defaultView.clicks"), 1)
                        self.assertGreater(await child.locator("#scroller").evaluate("node => node.scrollTop"), 100)
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)
                except Exception as error:
                    # Playwright assertion snapshots can include password field values.
                    if page:
                        try:
                            token = await page.locator("#token").input_value(timeout=1000) or token
                        except Exception:
                            pass
                    message = str(error).replace(token, '[redacted]') if token else str(error)
                    raise AssertionError(message) from None
                finally:
                    await context.close()
