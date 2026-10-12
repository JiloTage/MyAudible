#!/usr/bin/env python3
"""Start the local servers and stop both when Ctrl+C is pressed."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def port_number(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def require_free_port(port: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"127.0.0.1:{port} is already in use; stop that server or choose another port.") from error


def launch(arguments: list[str], *, environment: dict[str, str] | None = None) -> subprocess.Popen:
    options = {"env": environment}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(arguments, **options)


def stop_child(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        if os.name == "nt":
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=5)


def wait_for_speech(child: subprocess.Popen, url: str, seconds: float = 300) -> None:
    # A loopback request must never go through HTTP_PROXY/HTTPS_PROXY.
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"speech.cpp stopped during startup (exit {child.returncode}); see its output above.")
        try:
            with http.open(url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.3)
    raise RuntimeError("speech.cpp did not become ready within five minutes; check its model, GPU and output above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speech", default="speech", help="speech.cpp v0.8.4 executable")
    parser.add_argument("--model", default="irodori-tts-mf")
    parser.add_argument("--speech-device", choices=("auto", "gpu", "cpu"), default="gpu")
    parser.add_argument("--ocr-device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--speech-port", type=port_number, default=8081)
    parser.add_argument("--backend-port", type=port_number, default=8765)
    args = parser.parse_args()
    speech_child = backend_child = None
    try:
        if args.speech_port == args.backend_port:
            raise RuntimeError("Speech and backend ports must differ.")
        if args.backend_port < 1024:
            raise RuntimeError("MyAudible backend port must be between 1024 and 65535.")
        if args.model.startswith("-"):
            raise RuntimeError("Model must be a speech.cpp model name or a GGUF path.")
        require_free_port(args.speech_port)
        require_free_port(args.backend_port)
        version = subprocess.run([args.speech, "--version"], capture_output=True, text=True, check=True).stdout.strip()
        if not version.startswith("speech.cpp 0.8.4,"):
            raise RuntimeError(f"Expected speech.cpp 0.8.4; executable reports {version!r}. Run setup again.")
        if args.ocr_device == "cuda":
            try:
                import torch
            except ImportError as error:
                raise RuntimeError("PyTorch is missing. Run the Windows setup with the OCR dependencies first.") from error
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA OCR was selected but PyTorch cannot access CUDA. Install compatible CUDA wheels or select CPU OCR.")
        environment = os.environ.copy()
        environment["MYAUDIBLE_SPEECH_URL"] = f"http://127.0.0.1:{args.speech_port}"
        environment["MYAUDIBLE_OCR_DEVICE"] = args.ocr_device
        environment.setdefault("MYAUDIBLE_OCR_LITE", "1")
        environment["MYAUDIBLE_PORT"] = str(args.backend_port)
        print(f"Starting {version}; {args.model}, device {args.speech_device}.", flush=True)
        print("The first GPU run can take a while to compile shaders. Press Ctrl+C to stop both servers.", flush=True)
        speech_child = launch([args.speech, "serve", args.model, "--host", "127.0.0.1", "--port", str(args.speech_port), "--device", args.speech_device])
        wait_for_speech(speech_child, environment["MYAUDIBLE_SPEECH_URL"])
        backend_child = launch([sys.executable, "-m", "myaudible"], environment=environment)
        print(f"MyAudible backend starting at http://127.0.0.1:{args.backend_port}; use the connection token printed below.", flush=True)
        while True:
            if speech_child.poll() is not None:
                raise RuntimeError(f"speech.cpp exited with code {speech_child.returncode}; stopping the backend.")
            if backend_child.poll() is not None:
                if backend_child.returncode:
                    raise RuntimeError(f"MyAudible backend exited with code {backend_child.returncode}.")
                return 0
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nStopping MyAudible local servers.", flush=True)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"MyAudible: {error}", file=sys.stderr)
        return 1
    finally:
        stop_child(backend_child)
        stop_child(speech_child)


if __name__ == "__main__":
    raise SystemExit(main())
