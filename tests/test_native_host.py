import contextlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import native_host


TOKEN = "test-token-with-at-least-32-characters"
READY = {"ok": True, "url": native_host.BACKEND_URL, "token": TOKEN}


def frame(value):
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(body)) + body


@pytest.mark.parametrize("data", [
    b"", b"\0", struct.pack("=I", 0), struct.pack("=I", 4097),
    struct.pack("=I", 10) + b"{}", frame({"type": "stop"}),
    frame({"type": "start", "command": "calc.exe"}), frame([]),
])
def test_rejects_malformed_or_arbitrary_commands_before_starting(data):
    with pytest.raises(ValueError):
        native_host.read_message(io.BytesIO(data))


def test_native_message_utf8_byte_lengths_and_real_stdio():
    assert native_host.read_message(io.BytesIO(frame({"type": "start"}))) == {"type": "start"}
    result = subprocess.run(
        [sys.executable, str(native_host.ROOT / "scripts/native_host.py")],
        input=frame({"type": "unsupported"}), capture_output=True, timeout=10, check=True,
    )
    size = struct.unpack("=I", result.stdout[:4])[0]
    assert size == len(result.stdout[4:])
    response = json.loads(result.stdout[4:])
    assert response["ok"] is False
    assert "対応していない" in response["error"]
    assert not result.stderr


def test_connection_rejects_redirects_without_sending_token_elsewhere(monkeypatch):
    reached = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            reached.append((self.path, self.headers.get("Authorization")))
            if self.path == "/v1/voices":
                self.send_response(302)
                self.send_header("Location", "/outside")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(native_host, "BACKEND_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        assert native_host.connection(SimpleNamespace(token=TOKEN)) is None
        assert reached == [("/v1/voices", f"Bearer {TOKEN}")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    speech = tmp_path / "Programs/speech.cpp/current/speech.exe"
    speech.parent.mkdir(parents=True)
    speech.touch()
    monkeypatch.setattr(native_host, "Settings", lambda **_kwargs: SimpleNamespace(state_dir=state_dir, token=TOKEN))
    monkeypatch.setattr(native_host, "startup_lock", lambda _state: contextlib.nullcontext())
    monkeypatch.setattr(native_host, "os", SimpleNamespace(name="nt", environ={"LOCALAPPDATA": str(tmp_path), "MYAUDIBLE_NATIVE_MODEL": "irodori-tts-mf:q8_0"}))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    sockets = MagicMock()
    monkeypatch.setattr(native_host.socket, "socket", sockets)
    runner = MagicMock(pid=12345)
    runner.poll.return_value = None
    launch = MagicMock(return_value=runner)
    monkeypatch.setattr(native_host.subprocess, "Popen", launch)
    monkeypatch.setattr(native_host.time, "sleep", lambda _seconds: None)
    stop = MagicMock()
    monkeypatch.setattr(native_host, "stop_runner", stop)
    return SimpleNamespace(launch=launch, runner=runner, sockets=sockets, stop=stop, state_dir=state_dir, speech=speech)


def test_existing_authenticated_backend_does_not_launch_duplicates(launcher, monkeypatch):
    monkeypatch.setattr(native_host, "connection", lambda _settings: READY)
    assert native_host.start_servers() == {**READY, "already_running": True}
    launcher.launch.assert_not_called()
    launcher.sockets.assert_not_called()


def test_start_waits_for_authentication_and_detaches_without_pipe_inheritance(launcher, monkeypatch):
    responses = iter([None, None, READY])
    monkeypatch.setattr(native_host, "connection", lambda _settings: next(responses))
    assert native_host.start_servers() == {**READY, "already_running": False}
    arguments, options = launcher.launch.call_args
    assert "--quiet" in arguments[0]
    assert "irodori-tts-mf:q8_0" in arguments[0]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"].name == str(launcher.state_dir / "server-startup.log")
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    launcher.stop.assert_not_called()
    assert TOKEN not in (launcher.state_dir / "server-startup.log").read_text()


def test_busy_port_never_launches_or_stops_an_existing_process(launcher, monkeypatch):
    monkeypatch.setattr(native_host, "connection", lambda _settings: None)
    launcher.sockets.return_value.__enter__.return_value.bind.side_effect = OSError("in use")
    with pytest.raises(RuntimeError, match="8765.*使用中"):
        native_host.start_servers()
    launcher.launch.assert_not_called()
    launcher.stop.assert_not_called()


def test_missing_speech_executable_returns_setup_instructions(launcher, monkeypatch):
    monkeypatch.setattr(native_host, "connection", lambda _settings: None)
    launcher.speech.unlink()
    with pytest.raises(RuntimeError, match="setup-windows"):
        native_host.start_servers()
    launcher.launch.assert_not_called()


@pytest.mark.parametrize("exited", [True, False])
def test_failed_start_or_timeout_cleans_up_only_its_runner(launcher, monkeypatch, exited):
    monkeypatch.setattr(native_host, "connection", lambda _settings: None)
    if exited:
        launcher.runner.poll.return_value = 1
    else:
        monkeypatch.setattr(native_host, "STARTUP_SECONDS", 0)
    with pytest.raises(RuntimeError, match="失敗|タイムアウト"):
        native_host.start_servers()
    launcher.stop.assert_called_once_with(launcher.runner)


@pytest.mark.skipif(os.name != "nt", reason="Windows file locking")
def test_two_native_hosts_cannot_start_servers_at_the_same_time(tmp_path):
    with native_host.startup_lock(tmp_path):
        with pytest.raises(RuntimeError, match="起動中"):
            with native_host.startup_lock(tmp_path):
                pytest.fail("Second startup acquired the lock")
    with native_host.startup_lock(tmp_path):
        pass
