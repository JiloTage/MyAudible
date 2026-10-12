import asyncio
import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest

from myaudible.speech import SpeechError, SpeechService, split_text
from myaudible.voices import VoiceStore


def recording(seconds=4, rate=16000, channels=1, width=2):
    pcm = (np.sin(np.arange(seconds * rate) * 2 * np.pi * 220 / rate) * 5000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def event_bytes(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


def success_response(audio=b"\x01\x00\x02\x00"):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream", "X-Sample-Rate": "48000"}, content=event_bytes(
        {"type": "speech.audio.delta", "audio": base64.b64encode(audio).decode()}, {"type": "speech.audio.done", "samples": 2},
    ))


async def test_real_upstream_sse_contract_and_sentence_chunking(tmp_path):
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = SpeechService("http://127.0.0.1:8081", VoiceStore(tmp_path), client)
        audio = [chunk async for chunk in service.synthesize("あ" * 25 + "。" + "い" * 25 + "。", {"chunk_chars": 30, "steps": 4, "seed": 9, "instructions": "calm"})]
    assert len(audio) == 2
    assert all(chunk == (b"\x01\x00\x02\x00", 48000) for chunk in audio)
    assert requests[0] == {"input": "あ" * 25 + "。", "voice": "none", "response_format": "pcm", "stream_format": "sse", "steps": 4, "speed": 1, "seed": 9, "instructions": "calm"}


async def test_sse_missing_done_is_error_without_fake_audio(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b""))) as client:
        service = SpeechService("http://127.0.0.1:8081", VoiceStore(tmp_path), client)
        with pytest.raises(SpeechError, match="before completion"):
            [audio async for audio in service.synthesize("test", {})]


async def test_instream_error_and_malformed_pcm_are_rejected(tmp_path):
    for events, match in [
        ([{"type": "error", "error": {"message": "out of memory"}}], "out of memory"),
        ([{"type": "speech.audio.delta", "audio": "AQ=="}], "16-bit"),
        ([{"type": "speech.audio.delta", "audio": "!!!"}], "invalid audio"),
    ]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=event_bytes(*events)))) as client:
            service = SpeechService("http://127.0.0.1:8081", VoiceStore(tmp_path), client)
            with pytest.raises(SpeechError, match=match):
                [audio async for audio in service.synthesize("test", {})]


async def test_reference_lazy_registration_and_upstream_restart(tmp_path):
    store = VoiceStore(tmp_path)
    voice = store.add("voice", recording())
    registered = False
    uploads = []

    def upstream(request):
        nonlocal registered
        if request.url.path == "/speech/voices":
            assert b'name="name"' in request.content and voice["id"].encode() in request.content
            assert b'name="file"' in request.content and b"RIFF" in request.content
            uploads.append(request.content)
            registered = True
            return httpx.Response(200, json={"held": {}})
        if not registered:
            return httpx.Response(400, json={"error": {"message": "voice not found", "param": "voice"}})
        return success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = SpeechService("http://127.0.0.1:8081", store, client)
        options = {"voice_id": voice["id"]}
        [chunk async for chunk in service.synthesize("one", options)]
        [chunk async for chunk in service.synthesize("two", options)]
        assert len(uploads) == 1
        registered = False  # Simulate upstream restart without restarting our backend.
        [chunk async for chunk in service.synthesize("three", options)]
        assert len(uploads) == 2


async def test_cancellation_closes_upstream_stream(tmp_path):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.sleep(60)
            yield b""

        async def aclose(self):
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=SlowStream()))) as client:
        service = SpeechService("http://127.0.0.1:8081", VoiceStore(tmp_path), client)

        async def consume():
            return [chunk async for chunk in service.synthesize("test", {})]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


async def test_backend_restart_accepts_voice_still_registered_upstream(tmp_path):
    store = VoiceStore(tmp_path)
    voice = store.add("voice", recording())

    def upstream(request):
        if request.url.path == "/speech/voices":
            return httpx.Response(400, json={"error": {"message": voice["id"] + " is already a voice of Irodori; give the voice another name", "param": "name"}})
        return success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = SpeechService("http://127.0.0.1:8081", store, client)
        audio = [chunk async for chunk in service.synthesize("after restart", {"voice_id": voice["id"]})]
        assert len(audio) == 1
        assert voice["id"] in service._registered


def test_split_japanese_and_long_unpunctuated_text():
    original = "今日は晴れです。明日は雨です。" + "長" * 101
    chunks = split_text(original, 30)
    assert "".join(chunks) == original
    assert all(0 < len(chunk) <= 30 for chunk in chunks)
