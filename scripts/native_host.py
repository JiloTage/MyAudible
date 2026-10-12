"""Chrome Native Messaging host for starting the fixed local MyAudible servers."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

from myaudible.config import Settings

ROOT = Path(__file__).resolve().parents[1]
BACKEND_URL = "http://127.0.0.1:8765"
MAX_MESSAGE_BYTES = 4096
STARTUP_SECONDS = 330


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def connection(settings: Settings) -> dict | None:
    # A different service on this port must not be mistaken for MyAudible.
    request = urllib.request.Request(BACKEND_URL + "/v1/voices", headers={"Authorization": f"Bearer {settings.token}"})
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with http.open(request, timeout=2) as response:
            data = json.loads(response.read(1024 * 1024))
        if isinstance(data, dict) and isinstance(data.get("voices"), list):
            return {"ok": True, "url": BACKEND_URL, "token": settings.token}
    except (OSError, urllib.error.URLError, ValueError):
        pass
    return None


@contextmanager
def startup_lock(state_dir: Path):
    import msvcrt

    # Keep the file in place: deleting a lock file would let another host bypass it.
    with (state_dir / "server-start.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("別のパネルでサーバーを起動中です。しばらく待ってから再試行してください。") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def stop_runner(runner: subprocess.Popen) -> None:
    if runner.poll() is not None:
        return
    # Only the process tree launched by this request may be terminated.
    subprocess.run(["taskkill", "/PID", str(runner.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    runner.wait(timeout=10)


def start_servers() -> dict:
    if os.name != "nt":
        raise RuntimeError("拡張機能からのサーバー起動はWindowsに対応しています。")
    settings = Settings(speech_url="http://127.0.0.1:8081", ocr_device="cpu")
    with startup_lock(settings.state_dir):
        existing = connection(settings)
        if existing:
            return {**existing, "already_running": True}
        for port in (8765, 8081):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as error:
                    raise RuntimeError(f"127.0.0.1:{port}は使用中です。既存サーバーの接続設定を確認するか、終了してから再試行してください。") from error
        speech = Path(os.environ["LOCALAPPDATA"]) / "Programs/speech.cpp/current/speech.exe"
        if not speech.is_file():
            raise RuntimeError("speech.cppが見つかりません。scripts\\setup-windows.ps1を実行してください。")
        log_path = settings.state_dir / "server-startup.log"
        with log_path.open("w", encoding="utf-8") as log:
            runner = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "scripts/run-local.py"), "--speech", str(speech),
                 "--model", os.environ.get("MYAUDIBLE_NATIVE_MODEL", "irodori-tts-mf"), "--quiet"],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1"},
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        try:
            deadline = time.monotonic() + STARTUP_SECONDS
            while time.monotonic() < deadline:
                if runner.poll() is not None:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
                    raise RuntimeError(f"サーバーの起動に失敗しました。詳細はscripts\\run-windows.ps1で確認できます。\n{detail}")
                ready = connection(settings)
                if ready:
                    # Detached servers keep running after the side panel is closed.
                    return {**ready, "already_running": False}
                time.sleep(0.3)
            raise RuntimeError("サーバーの起動がタイムアウトしました。scripts\\run-windows.ps1でモデルとGPUを確認してください。")
        except BaseException:
            stop_runner(runner)
            raise


def read_message(stream) -> dict:
    header = stream.read(4)
    if len(header) != 4:
        raise ValueError("起動要求のヘッダーが不正です。")
    size = struct.unpack("=I", header)[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ValueError("起動要求のサイズが不正です。")
    body = stream.read(size)
    if len(body) != size:
        raise ValueError("起動要求が途中で終了しました。")
    message = json.loads(body)
    # No executable paths, shell commands, ports or environment from the browser.
    if message != {"type": "start"}:
        raise ValueError("対応していない起動要求です。")
    return message


def write_message(stream, message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("=I", len(body)) + body)
    stream.flush()


def main() -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        read_message(sys.stdin.buffer)
        result = start_servers()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result = {"ok": False, "error": str(error)}
    write_message(sys.stdout.buffer, result)


if __name__ == "__main__":
    main()
