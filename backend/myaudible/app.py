from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import re
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .ocr import MAX_IMAGE_BYTES, InvalidImage, OCRService, OCRUnavailable
from .speech import SpeechError, SpeechService
from .voices import MAX_REFERENCE_BYTES, InvalidReference, VoiceStore

EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
MAX_WS_BYTES = 18 * 1024 * 1024


def allowed_origin(origin: str | None) -> bool:
    return origin is None or bool(EXTENSION_ORIGIN.fullmatch(origin))


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text", "image"]
    text: str | None = Field(default=None, max_length=10000)
    data_base64: str | None = Field(default=None, max_length=MAX_IMAGE_BYTES * 4 // 3 + 4)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"] | None = None
    reading_order: Literal["auto", "vertical", "horizontal"] = "auto"


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    steps: int = Field(default=4, ge=1, le=100, strict=True)
    seed: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    chunk_chars: int = Field(default=100, ge=20, le=300, strict=True)
    instructions: str | None = Field(default=None, max_length=1000)


class Synthesize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["synthesize"]
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    input: Input
    options: Options = Field(default_factory=Options)


async def worker_until_done(function, *args):
    """Cancellation never releases the shared GPU gate while a worker still runs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # Retrieve worker failure without logging OCR inputs.
        raise


def create_app(settings: Settings | None = None, *, ocr=None, client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings()
    voices = VoiceStore(settings.state_dir)
    ocr = ocr or OCRService(device=settings.ocr_device, lightweight=settings.ocr_lite)
    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=5), limits=httpx.Limits(max_connections=6, max_keepalive_connections=4), trust_env=False,
    )
    speech = SpeechService(settings.speech_url, voices, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="MyAudible local backend", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.voices = voices
    app.state.ocr = ocr
    app.state.speech = speech
    app.state.gpu_gate = asyncio.Semaphore(1)
    app.state.jobs = 0
    app.state.connections = 0
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://[a-p]{32}", allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def protect_browser_requests(request: Request, call_next):
        if not allowed_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "Untrusted browser origin"}, status_code=403)
        if request.method == "POST" and request.url.path == "/v1/voices":
            raw_length = request.headers.get("content-length")
            try:
                length = int(raw_length) if raw_length is not None else None
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            if length is None:
                return JSONResponse({"detail": "Content-Length is required for reference uploads"}, status_code=411)
            if length < 0 or length > MAX_REFERENCE_BYTES + 65536:
                return JSONResponse({"detail": "Reference upload is too large"}, status_code=413)
        return await call_next(request)

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header.encode("utf-8"), f"Bearer {settings.token}".encode("utf-8")):
            raise HTTPException(401, "Invalid local API token")

    auth = Depends(require_token)

    @app.get("/health")
    async def health():
        ocr_status = await asyncio.to_thread(ocr.readiness)
        tts_status = await speech.readiness()
        ready = bool(ocr_status.get("available")) and bool(tts_status.get("ready"))
        messages = []
        if not ocr_status.get("available"):
            messages.append("Install backend[ocr] to enable screenshot OCR")
        if not tts_status.get("ready"):
            messages.append(tts_status.get("message", "speech.cpp is unavailable"))
        return {"status": "ok" if ready else "degraded", "ocr_ready": bool(ocr_status.get("available")), "tts_ready": bool(tts_status.get("ready")), "message": "; ".join(messages), "ocr": ocr_status, "tts": tts_status, "version": __version__}

    @app.get("/v1/voices", dependencies=[auth])
    async def list_voices():
        return {"voices": await asyncio.to_thread(voices.list)}

    @app.post("/v1/voices", dependencies=[auth], status_code=201)
    async def add_voice(name: Annotated[str, Form(max_length=80)], audio: Annotated[UploadFile, File()]):
        try:
            data = await audio.read(MAX_REFERENCE_BYTES + 1)
            if len(data) > MAX_REFERENCE_BYTES:
                raise HTTPException(413, "Reference audio is too large")
            return await worker_until_done(voices.add, name, data)
        except InvalidReference as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await audio.close()

    @app.delete("/v1/voices/{voice_id}", dependencies=[auth])
    async def delete_voice(voice_id: str):
        if not await worker_until_done(voices.delete, voice_id):
            raise HTTPException(404, "Reference voice not found")
        speech.forget_voice(voice_id)
        return {"deleted": True}

    @app.websocket("/v1/realtime")
    async def realtime(ws: WebSocket):
        if not allowed_origin(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        if app.state.connections >= settings.max_connections:
            await ws.close(code=1013)
            return
        await ws.accept()
        app.state.connections += 1
        send_lock = asyncio.Lock()
        connected = True
        tasks: set[asyncio.Task] = set()
        active: asyncio.Task | None = None
        active_id: str | None = None
        pending_acks: dict[tuple[str, int], asyncio.Future] = {}

        async def send(event: dict):
            if not connected:
                return
            try:
                async with send_lock:
                    await ws.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                pass

        async def run_job(job: Synthesize):
            try:
                async with asyncio.timeout(settings.job_timeout):
                    async with app.state.gpu_gate:
                        if job.input.type == "image":
                            await send({"type": "status", "id": job.id, "stage": "ocr", "message": "画像を読み取っています"})
                            if not job.input.data_base64 or not job.input.mime_type:
                                raise InvalidImage("Image input needs data_base64 and mime_type")
                            try:
                                image = base64.b64decode(job.input.data_base64, validate=True)
                            except (ValueError, binascii.Error) as exc:
                                raise InvalidImage("Image is not valid base64") from exc
                            text = await worker_until_done(ocr.recognize, image, job.input.reading_order)
                            del image
                        else:
                            text = job.input.text or ""
                        text = text.strip()
                        if not text:
                            raise SpeechError("読み上げるテキストがありません", "empty_text")
                        if len(text) > 10000:
                            raise SpeechError("OCR output exceeds 10,000 characters; select a smaller reading area", "validation_error")
                        await send({"type": "transcript", "id": job.id, "text": text})
                        await send({"type": "status", "id": job.id, "stage": "tts", "message": "音声を生成しています"})
                        index = 0
                        async with aclosing(speech.synthesize(text, job.options.model_dump())) as stream:
                            async for audio, rate in stream:
                                key = (job.id, index)
                                ack = asyncio.get_running_loop().create_future()
                                pending_acks[key] = ack
                                try:
                                    await send({"type": "audio.chunk", "id": job.id, "index": index, "format": "pcm_s16le", "sample_rate": rate, "channels": 1, "data_base64": base64.b64encode(audio).decode("ascii")})
                                    try:
                                        await asyncio.wait_for(ack, timeout=60)
                                    except TimeoutError as exc:
                                        raise SpeechError("音声再生の応答がありません。サイドバーを開き直してください", "playback_timeout") from exc
                                finally:
                                    pending_acks.pop(key, None)
                                index += 1
                        await send({"type": "completed", "id": job.id})
            except asyncio.CancelledError:
                await send({"type": "cancelled", "id": job.id})
            except TimeoutError:
                await send({"type": "error", "id": job.id, "code": "timeout", "message": "処理が時間制限を超えました。短いテキストで再実行してください"})
            except (InvalidImage, ValueError) as exc:
                await send({"type": "error", "id": job.id, "code": "validation_error", "message": str(exc)[:500]})
            except OCRUnavailable as exc:
                await send({"type": "error", "id": job.id, "code": "ocr_unavailable", "message": str(exc)[:500]})
            except SpeechError as exc:
                await send({"type": "error", "id": job.id, "code": exc.code, "message": str(exc)[:500]})
            except Exception:
                # Do not log exceptions that may contain screenshot/transcript content.
                await send({"type": "error", "id": job.id, "code": "internal_error", "message": "ローカル処理に失敗しました。依存ソフトとモデルを確認してください"})
        try:
            try:
                first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            except (TimeoutError, ValueError):
                await send({"type": "error", "code": "auth_required", "message": "Authenticate in the first WebSocket frame"})
                await ws.close(code=1008)
                return
            if not isinstance(first, dict) or first.get("type") != "auth" or not isinstance(first.get("token"), str) or not hmac.compare_digest(first["token"].encode("utf-8"), settings.token.encode("utf-8")):
                await send({"type": "error", "code": "unauthorized", "message": "Invalid local API token"})
                await ws.close(code=1008)
                return
            await send({"type": "ready", "flow_control": "audio.ack"})
            while True:
                try:
                    message = await ws.receive_json()
                except ValueError:
                    await send({"type": "error", "code": "validation_error", "message": "Send a JSON object"})
                    continue
                if not isinstance(message, dict):
                    await send({"type": "error", "code": "validation_error", "message": "Send a JSON object"})
                    continue
                request_id = message.get("id")
                if message.get("type") == "audio.ack":
                    index = message.get("index")
                    if isinstance(request_id, str) and isinstance(index, int) and not isinstance(index, bool):
                        ack = pending_acks.get((request_id, index))
                        if ack and not ack.done():
                            ack.set_result(None)
                    continue
                if message.get("type") == "cancel":
                    if active and not active.done() and (request_id is None or request_id == active_id):
                        if not active.cancelling():
                            active.cancel()
                    continue
                try:
                    job = Synthesize.model_validate(message)
                except ValidationError:
                    safe_id = request_id if isinstance(request_id, str) and len(request_id) <= 80 else None
                    await send({"type": "error", "id": safe_id, "code": "validation_error", "message": "Invalid request or synthesis options"})
                    continue
                if active and not active.done() and not active.cancelling():
                    active.cancel()
                if app.state.jobs >= settings.max_jobs:
                    await send({"type": "error", "id": job.id, "code": "busy", "message": "ローカル処理が混み合っています。少し待って再実行してください"})
                    continue
                app.state.jobs += 1
                active_id = job.id
                active = asyncio.create_task(run_job(job))
                tasks.add(active)
                def job_done(task):
                    tasks.discard(task)
                    app.state.jobs -= 1
                active.add_done_callback(job_done)
        except WebSocketDisconnect:
            pass
        finally:
            connected = False
            app.state.connections -= 1
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    return app


app = create_app()
