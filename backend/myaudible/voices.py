from __future__ import annotations

import io
import json
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MAX_REFERENCE_BYTES = 12 * 1024 * 1024
MAX_VOICES = 64


class InvalidReference(ValueError):
    pass


def normalize_reference(data: bytes) -> bytes:
    """Validate PCM WAV and convert it to mono 24 kHz signed 16-bit WAV in memory."""
    if not data or len(data) > MAX_REFERENCE_BYTES:
        raise InvalidReference("Reference audio must be a WAV file no larger than 12 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            rate, channels, width, frames = source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getnframes()
            if source.getcomptype() != "NONE" or width not in {1, 2, 3, 4} or channels not in {1, 2} or not 8000 <= rate <= 192000:
                raise InvalidReference("Use an uncompressed PCM WAV with one or two channels, at 8–192 kHz")
            duration = frames / rate
            if not 3 <= duration <= 30:
                raise InvalidReference("Reference audio must be between 3 and 30 seconds")
            raw = source.readframes(frames)
            if len(raw) != frames * channels * width:
                raise InvalidReference("The WAV file is truncated")
    except (wave.Error, EOFError, OSError, ZeroDivisionError) as exc:
        raise InvalidReference("Use an uncompressed PCM WAV file") from exc
    if width == 1:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        samples = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        octets = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        samples = ((values ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
    else:
        samples = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    samples = samples.reshape(-1, channels).mean(axis=1)
    if not len(samples) or np.max(np.abs(samples)) < 0.0001:
        raise InvalidReference("The reference recording is silent")
    # Linear resampling keeps the lightweight bridge independent of ffmpeg/audioop.
    if rate != 24000:
        positions = np.arange(round(len(samples) * 24000 / rate), dtype=np.float64) * rate / 24000
        samples = np.interp(positions, np.arange(len(samples)), samples)
    pcm = np.rint(np.clip(samples, -1, 32767 / 32768) * 32768).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as dest:
        dest.setnchannels(1)
        dest.setsampwidth(2)
        dest.setframerate(24000)
        dest.writeframes(pcm)
    return output.getvalue()


class VoiceStore:
    """Persist only explicitly registered reference audio and its display name."""

    def __init__(self, state_dir: Path):
        self.root = state_dir / "voices"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()

    @staticmethod
    def _valid_id(voice_id: str) -> bool:
        return len(voice_id) == 32 and all(c in "0123456789abcdef" for c in voice_id)

    def list(self) -> list[dict]:
        with self._lock:
            voices = []
            for path in self.root.glob("*.json"):
                if not self._valid_id(path.stem) or not path.with_suffix(".wav").is_file():
                    continue
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                    if info.get("id") == path.stem:
                        voices.append({key: info[key] for key in ("id", "name", "created_at")})
                except (ValueError, KeyError, OSError):
                    continue
            return sorted(voices, key=lambda voice: voice["created_at"])

    def add(self, name: str, data: bytes) -> dict:
        name = name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise InvalidReference("Reference name must be 1–80 characters without control characters")
        normalized = normalize_reference(data)
        with self._lock:
            if len(self.list()) >= MAX_VOICES:
                raise InvalidReference("At most 64 reference voices can be saved; remove an unused voice first")
            voice_id = uuid.uuid4().hex
            info = {"id": voice_id, "name": name, "created_at": datetime.now(timezone.utc).isoformat()}
            wav_path = self.root / f"{voice_id}.wav"
            meta_path = wav_path.with_suffix(".json")
            try:
                with wav_path.open("xb") as stream:
                    stream.write(normalized)
                wav_path.chmod(0o600)
                with meta_path.open("x", encoding="utf-8") as stream:
                    json.dump(info, stream, ensure_ascii=False)
                meta_path.chmod(0o600)
            except BaseException:
                wav_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise
            return info

    def audio(self, voice_id: str) -> bytes:
        with self._lock:
            if not self._valid_id(voice_id):
                raise KeyError("Reference voice not found")
            path = self.root / f"{voice_id}.wav"
            if not path.with_suffix(".json").is_file():
                raise KeyError("Reference voice not found")
            try:
                return path.read_bytes()
            except FileNotFoundError as exc:
                raise KeyError("Reference voice not found") from exc

    def delete(self, voice_id: str) -> bool:
        with self._lock:
            if not self._valid_id(voice_id):
                return False
            path = self.root / f"{voice_id}.wav"
            present = path.exists() or path.with_suffix(".json").exists()
            path.with_suffix(".json").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return present
