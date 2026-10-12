from __future__ import annotations

import os

import uvicorn

from .app import MAX_WS_BYTES, app


def main() -> None:
    settings = app.state.settings
    port = int(os.environ.get("MYAUDIBLE_PORT", "8765"))
    if not 1024 <= port <= 65535:
        raise ValueError("MYAUDIBLE_PORT must be between 1024 and 65535")
    print(f"MyAudible: http://127.0.0.1:{port}", flush=True)
    print(f"Sidebar token: {settings.token}", flush=True)
    print(f"State directory (reference voices and token only): {settings.state_dir}", flush=True)
    print(f"OCR device: {settings.ocr_device}; speech.cpp: {settings.speech_url}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning", ws_max_size=MAX_WS_BYTES, ws_max_queue=2)


if __name__ == "__main__":
    main()
