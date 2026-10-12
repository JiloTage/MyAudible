from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx

from .voices import VoiceStore

MAX_BUFFERED_AUDIO_BYTES = 64 * 1024 * 1024


class SpeechError(RuntimeError):
    def __init__(self, message: str, code: str = "upstream_error", *, missing_voice: bool = False, duplicate_voice: bool = False):
        super().__init__(message)
        self.code = code
        self.missing_voice = missing_voice
        self.duplicate_voice = duplicate_voice


async def sse_events(response: httpx.Response) -> AsyncIterator[dict]:
    data: list[str] = []
    async for line in response.aiter_lines():
        if len(line) > 3 * 1024 * 1024:
            raise SpeechError("speech.cpp returned an oversized audio event")
        if line == "":
            if data:
                joined = "\n".join(data)
                data.clear()
                if joined == "[DONE]":
                    yield {"type": "speech.audio.done"}
                else:
                    try:
                        event = json.loads(joined)
                    except ValueError as exc:
                        raise SpeechError("speech.cpp returned invalid SSE JSON") from exc
                    if not isinstance(event, dict):
                        raise SpeechError("speech.cpp returned invalid SSE JSON")
                    yield event
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        try:
            event = json.loads("\n".join(data))
        except ValueError as exc:
            raise SpeechError("speech.cpp returned incomplete SSE JSON") from exc
        if not isinstance(event, dict):
            raise SpeechError("speech.cpp returned invalid SSE JSON")
        yield event


class SpeechService:
    def __init__(self, base_url: str, voices: VoiceStore, client: httpx.AsyncClient):
        self.base_url = base_url
        self.voices = voices
        self.client = client
        self._registered: set[str] = set()

    async def readiness(self) -> dict:
        try:
            response = await self.client.get(f"{self.base_url}/v1/models", timeout=2)
            response.raise_for_status()
            body = response.json()
            models = body.get("data", []) if isinstance(body, dict) else []
            model = next((m.get("id", "") for m in models if isinstance(m, dict) and "irodori" in m.get("id", "").lower()), None)
            if not model:
                self._registered.clear()
            return {"available": True, "ready": bool(model), "model": model, "message": "" if model else "Start speech serve with irodori-tts-mf"}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            self._registered.clear()
            return {"available": False, "ready": False, "model": None, "message": "speech.cpp is unavailable; start the local speech server"}

    @staticmethod
    async def _response_error(response: httpx.Response) -> SpeechError:
        # This response is local and returned only to the caller; it is never logged.
        try:
            body = json.loads(await response.aread())
            error = body.get("error", {})
            message = str(error.get("message", "speech.cpp rejected the request"))[:500]
            param = error.get("param")
        except (ValueError, TypeError, AttributeError):
            message, param = f"speech.cpp returned HTTP {response.status_code}", None
        return SpeechError(
            message, missing_voice=param == "voice" and response.status_code in {400, 404, 422},
            duplicate_voice=param == "name" and response.status_code == 400 and "is already a voice" in message,
        )

    async def register_voice(self, voice_id: str, *, force: bool = False) -> None:
        if not force and voice_id in self._registered:
            return
        try:
            recording = await asyncio.to_thread(self.voices.audio, voice_id)
        except KeyError as exc:
            raise SpeechError("Reference voice no longer exists; choose another voice", "voice_not_found") from exc
        try:
            response = await self.client.post(
                f"{self.base_url}/speech/voices", data={"name": voice_id},
                files={"file": (f"{voice_id}.wav", recording, "audio/wav")},
            )
            if response.is_error:
                error = await self._response_error(response)
                if not error.duplicate_voice:
                    raise error
            self._registered.add(voice_id)
        except httpx.HTTPError as exc:
            raise SpeechError("Cannot register the voice with local speech.cpp", "tts_unavailable") from exc

    def forget_voice(self, voice_id: str) -> None:
        self._registered.discard(voice_id)

    async def _stream_audio(self, text: str, options: dict) -> AsyncIterator[tuple[bytes, int]]:
        # Playback ACKs must not stall speech.cpp's HTTP sender and its write timeout.
        queue = asyncio.Queue()
        queued_bytes = 0

        async def receive():
            nonlocal queued_bytes
            try:
                async with aclosing(self._receive_audio(text, options)) as stream:
                    async for audio, rate in stream:
                        queued_bytes += len(audio)
                        if queued_bytes > MAX_BUFFERED_AUDIO_BYTES:
                            raise SpeechError("生成音声がメモリ上限を超えました。読み取り範囲を小さくするか、短いテキストで再実行してください。", "audio_too_large")
                        queue.put_nowait((audio, rate))
            except Exception as exc:
                queue.put_nowait(exc)
            finally:
                queue.put_nowait(None)

        receiver = asyncio.create_task(receive())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                queued_bytes -= len(item[0])
                yield item
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def _receive_audio(self, text: str, options: dict) -> AsyncIterator[tuple[bytes, int]]:
        body = {
            "input": text, "voice": options.get("voice_id") or "none", "response_format": "pcm",
            "stream_format": "sse", "steps": options.get("steps", 4), "speed": options.get("speed", 1.0),
        }
        for key in ("seed", "instructions"):
            if options.get(key) is not None:
                body[key] = options[key]
        try:
            async with self.client.stream("POST", f"{self.base_url}/v1/audio/speech", json=body) as response:
                if response.is_error:
                    raise await self._response_error(response)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise SpeechError("speech.cpp did not return an SSE audio stream; use v0.8.4 or later")
                try:
                    rate = int(response.headers.get("X-Sample-Rate", "48000"))
                except ValueError as exc:
                    raise SpeechError("speech.cpp returned an invalid sample rate") from exc
                if not 8000 <= rate <= 192000:
                    raise SpeechError("speech.cpp returned an unsupported sample rate")
                done = False
                async for event in sse_events(response):
                    kind = event.get("type")
                    if kind == "speech.audio.delta":
                        try:
                            audio = base64.b64decode(event["audio"], validate=True)
                        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
                            raise SpeechError("speech.cpp returned invalid audio data") from exc
                        if len(audio) % 2:
                            raise SpeechError("speech.cpp returned incomplete 16-bit PCM samples")
                        if audio:
                            yield audio, rate
                    elif kind == "speech.audio.done":
                        done = True
                        break
                    elif kind == "error":
                        error = event.get("error", {})
                        if not isinstance(error, dict):
                            error = {}
                        raise SpeechError(str(error.get("message", "speech.cpp synthesis failed"))[:500])
                if not done:
                    raise SpeechError("speech.cpp audio stream ended before completion")
        except httpx.HTTPError as exc:
            raise SpeechError("Local speech.cpp disconnected or timed out", "tts_unavailable") from exc

    async def synthesize(self, text: str, options: dict) -> AsyncIterator[tuple[bytes, int]]:
        text = "".join(text.split())
        if not text:
            raise SpeechError("読み上げるテキストがありません", "empty_text")
        voice_id = options.get("voice_id")
        if voice_id:
            await self.register_voice(voice_id)
        emitted = False
        try:
            async with aclosing(self._stream_audio(text, options)) as stream:
                async for audio, rate in stream:
                    emitted = True
                    yield audio, rate
        except SpeechError as exc:
            if not voice_id or not exc.missing_voice or emitted:
                raise
            # speech.cpp keeps voices in memory. Its restart invalidates our cache.
            await self.register_voice(voice_id, force=True)
            async with aclosing(self._stream_audio(text, options)) as stream:
                async for audio, rate in stream:
                    yield audio, rate
