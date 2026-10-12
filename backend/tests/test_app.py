import asyncio
import base64
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from myaudible.app import Options, create_app
from myaudible.config import Settings
from myaudible.voices import MAX_REFERENCE_BYTES
from test_speech import recording, success_response


TOKEN = "test-token-with-at-least-32-characters"
ORIGIN = "chrome-extension://" + "a" * 32


def test_speed_accepts_cpp_range_and_rejects_invalid_values():
    assert Options().speed == 1
    for value in (0.25, 1, 1.5, 4):
        assert Options(speed=value).speed == value
    for value in (0.24, 4.01, float('nan'), float('inf'), True, '1.5'):
        with pytest.raises(ValueError):
            Options(speed=value)


class FakeOCR:
    def readiness(self):
        return {"available": True, "models_loaded": True, "device": "cpu"}

    def recognize(self, image, reading_order):
        return "縦書きの文章。"


def make_app(tmp_path, handler=None, ocr=None):
    handler = handler or (lambda req: httpx.Response(200, json={"data": [{"id": "irodori-tts-mf"}]}) if req.url.path == "/v1/models" else success_response())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(Settings(state_dir=tmp_path, token=TOKEN), client=client, ocr=ocr or FakeOCR())


def authenticate(ws):
    ws.send_json({"type": "auth", "token": TOKEN})
    assert ws.receive_json() == {"type": "ready", "flow_control": "audio.ack"}


def until_completed(ws, request_id):
    events = []
    for _ in range(30):
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "audio.chunk":
            ws.send_json({"type": "audio.ack", "id": event["id"], "index": event["index"]})
        if event["type"] in {"completed", "error"} and event.get("id") == request_id:
            return events
    raise AssertionError("No terminal event")


def test_health_auth_origin_and_dns_rebinding_protection(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        health = client.get("/health").json()
        assert health["status"] == "ok" and health["ocr_ready"] and health["tts_ready"]
        assert TOKEN not in str(health)
        assert client.get("/v1/voices").status_code == 401
        auth = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}
        assert client.get("/v1/voices", headers=auth).json() == {"voices": []}
        assert client.get("/v1/voices", headers={**auth, "Origin": "https://evil.example"}).status_code == 403
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/v1/voices", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_health_reports_upstream_failure_without_ready_claim(tmp_path):
    app = make_app(tmp_path, handler=lambda req: httpx.Response(200, json={"data": [{"id": "unrelated"}]}))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        health = client.get("/health").json()
        assert health["status"] == "degraded" and not health["tts_ready"]


def test_reference_persistence_validation_normalization_delete(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        result = client.post("/v1/voices", headers=headers, data={"name": "参照音声"}, files={"audio": ("source.wav", recording(), "audio/wav")})
        assert result.status_code == 201
        voice = result.json()
        assert client.get("/v1/voices", headers=headers).json()["voices"] == [voice]
        assert client.post("/v1/voices", headers=headers, data={"name": "bad"}, files={"audio": ("fake.wav", b"not audio")}).status_code == 422
        assert client.post("/v1/voices", headers=headers, data={"name": "short"}, files={"audio": ("short.wav", recording(seconds=1))}).status_code == 422
        assert client.post("/v1/voices", headers={**headers, "content-length": str(MAX_REFERENCE_BYTES + 100000)}, content=b"").status_code == 413
        assert client.delete(f"/v1/voices/{voice['id']}", headers=headers).status_code == 200
        assert list((tmp_path / "voices").iterdir()) == []


def test_ws_text_image_error_and_no_input_or_audio_persistence(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime", headers={"Origin": ORIGIN}) as ws:
            authenticate(ws)
            ws.send_json({"type": "synthesize", "id": "text-1", "input": {"type": "text", "text": "こんにちは。"}})
            events = until_completed(ws, "text-1")
            assert [e["type"] for e in events] == ["transcript", "status", "audio.chunk", "completed"]
            audio = events[2]
            assert base64.b64decode(audio["data_base64"]) == b"\x01\x00\x02\x00"
            assert audio["sample_rate"] == 48000 and audio["format"] == "pcm_s16le"
            ws.send_json({"type": "synthesize", "id": "image-1", "input": {"type": "image", "mime_type": "image/png", "reading_order": "vertical", "data_base64": base64.b64encode(b"fake screenshot").decode()}})
            events = until_completed(ws, "image-1")
            assert any(e["type"] == "transcript" and e["text"] == "縦書きの文章。" for e in events)
            ws.send_json({"type": "synthesize", "id": "invalid", "input": {"type": "image", "mime_type": "image/png", "data_base64": "!!!"}})
            assert until_completed(ws, "invalid")[-1]["code"] == "validation_error"
            ws.send_json({"type": "synthesize", "id": "bad-option", "input": {"type": "text", "text": "hi"}, "options": {"steps": 0}})
            assert ws.receive_json()["code"] == "validation_error"
    assert list((tmp_path / "voices").iterdir()) == []
    assert app.state.jobs == 0 and app.state.connections == 0


def test_ws_auth_unicode_and_untrusted_origin(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime") as ws:
            ws.send_json({"type": "auth", "token": "日本語"})
            assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(Exception):
            with client.websocket_connect("ws://127.0.0.1/v1/realtime", headers={"Origin": "https://evil.example"}):
                pass


@pytest.mark.parametrize("input_type", ["text", "image"])
def test_whitespace_is_removed_for_tts_but_kept_in_transcript(tmp_path, input_type):
    import json
    text = "こん にちは。\n世界　へ。"
    requests = []

    class WhitespaceOCR(FakeOCR):
        def recognize(self, image, reading_order):
            return text

    def upstream(request):
        requests.append(json.loads(request.content))
        return success_response()

    app = make_app(tmp_path, handler=upstream, ocr=WhitespaceOCR())
    input_data = {"type": "text", "text": text} if input_type == "text" else {
        "type": "image", "mime_type": "image/png",
        "data_base64": base64.b64encode(b"fake screenshot").decode(),
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime") as ws:
            authenticate(ws)
            ws.send_json({"type": "synthesize", "id": "whitespace", "input": input_data, "options": {"speed": 1.5}})
            events = until_completed(ws, "whitespace")
            assert events[-1]["type"] == "completed"
            assert next(e["text"] for e in events if e["type"] == "transcript") == text
    assert len(requests) == 1
    assert requests[0]["input"] == "こんにちは。世界へ。"
    assert requests[0]["speed"] == 1.5


def test_old_panel_chunk_setting_is_ignored_and_full_text_is_forwarded(tmp_path):
    import json
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return success_response()

    app = make_app(tmp_path, handler=upstream)
    text = "古いパネルから送った文章も全文を一度に渡します。\n" * 10
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime") as ws:
            authenticate(ws)
            ws.send_json({"type": "synthesize", "id": "legacy", "input": {"type": "text", "text": text}, "options": {"chunk_chars": 20}})
            assert until_completed(ws, "legacy")[-1]["type"] == "completed"
    assert len(requests) == 1
    assert requests[0]["input"] == text.replace("\n", "")
    assert "chunk_chars" not in requests[0]
    assert requests[0]["speed"] == 1


def test_latest_cancel_waits_for_blocking_ocr_before_reusing_gpu(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    tts_started = threading.Event()

    class BlockingOCR(FakeOCR):
        def recognize(self, image, reading_order):
            entered.set()
            assert release.wait(4)
            return "cancelled image transcript"

    def upstream(req):
        tts_started.set()
        return success_response()

    app = make_app(tmp_path, handler=upstream, ocr=BlockingOCR())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime") as ws:
            authenticate(ws)
            ws.send_json({"type": "synthesize", "id": "old", "input": {"type": "image", "mime_type": "image/png", "data_base64": "aGVsbG8="}})
            assert entered.wait(2)
            ws.send_json({"type": "synthesize", "id": "new", "input": {"type": "text", "text": "new"}})
            assert not tts_started.wait(0.1)
            release.set()
            events = until_completed(ws, "new")
            assert any(e["type"] == "cancelled" and e["id"] == "old" for e in events)
            assert not any(e["type"] in {"transcript", "audio.chunk"} and e["id"] == "old" for e in events)
    assert app.state.jobs == 0


def test_cancel_pending_playback_credit_releases_upstream_and_latest(tmp_path):
    closed = threading.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            from test_speech import event_bytes
            yield event_bytes({"type": "speech.audio.delta", "audio": "AQACAA=="})
            await asyncio.sleep(30)

        async def aclose(self):
            closed.set()

    def upstream(req):
        import json
        if json.loads(req.content)["input"] == "old":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Stream())
        return success_response()

    app = make_app(tmp_path, handler=upstream)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("ws://127.0.0.1/v1/realtime") as ws:
            authenticate(ws)
            ws.send_json({"type": "synthesize", "id": "old", "input": {"type": "text", "text": "old"}})
            for _ in range(5):
                if ws.receive_json()["type"] == "audio.chunk":
                    break
            ws.send_json({"type": "synthesize", "id": "new", "input": {"type": "text", "text": "new"}})
            events = until_completed(ws, "new")
            assert events[-1]["type"] == "completed"
            assert closed.wait(2)
    assert app.state.jobs == 0
