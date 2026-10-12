from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def default_state_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "MyAudible"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "MyAudible"


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("MYAUDIBLE_STATE_DIR", default_state_dir())))
    speech_url: str = field(default_factory=lambda: os.environ.get("MYAUDIBLE_SPEECH_URL", "http://127.0.0.1:8081"))
    ocr_device: str = field(default_factory=lambda: os.environ.get("MYAUDIBLE_OCR_DEVICE", "cpu"))
    ocr_lite: bool = field(default_factory=lambda: os.environ.get("MYAUDIBLE_OCR_LITE", "1") != "0")
    token: str = ""
    max_jobs: int = 8
    max_connections: int = 16
    job_timeout: float = 600

    def __post_init__(self) -> None:
        parsed = urlparse(self.speech_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("MYAUDIBLE_SPEECH_URL must be an HTTP loopback URL, for example http://127.0.0.1:8081")
        self.speech_url = self.speech_url.rstrip("/")
        if self.ocr_device not in {"cpu", "cuda"}:
            raise ValueError("MYAUDIBLE_OCR_DEVICE must be cpu or cuda")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.token:
            return
        explicit = os.environ.get("MYAUDIBLE_TOKEN")
        if explicit:
            if len(explicit) < 24:
                raise ValueError("MYAUDIBLE_TOKEN must have at least 24 characters")
            self.token = explicit
            return
        token_file = self.state_dir / "token"
        try:
            self.token = token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self.token = secrets.token_urlsafe(32)
            try:
                with token_file.open("x", encoding="utf-8") as stream:
                    stream.write(self.token + "\n")
                token_file.chmod(0o600)
            except FileExistsError:
                self.token = token_file.read_text(encoding="utf-8").strip()
        if len(self.token) < 24:
            raise ValueError("The stored token is invalid; remove the state directory token file and restart")
