# Local API contract

Backend: FastAPI, bound to `127.0.0.1:8765`, all data remains on user's PC.
`GET /health` is unauthenticated, contains readiness only: `{status:"ok"|"degraded", ocr_ready:boolean, tts_ready:boolean, message:string}`. `/v1/*` REST uses `Authorization: Bearer <token>`.
`GET /v1/voices` returns `{voices: [{id, name, created_at}]}`. `POST /v1/voices` multipart `name`, `audio` persists normalized reference audio and metadata, returns voice. `DELETE /v1/voices/{id}` removes voice.

WebSocket `/v1/realtime`: first frame `{type:"auth", token}`. Backend emits `{type:"ready", flow_control:"audio.ack"}` or error and closes. Requests:

```json
{"type":"synthesize", "id":"uuid", "input":{"type":"text","text":"こんにちは。"}, "options":{"voice_id":null,"steps":4,"instructions":"落ち着いた声", "seed":42,"chunk_chars":120}}
```

Image input instead: `{type:"image", data_base64:"...", mime_type:"image/png", reading_order:"vertical"}`. `reading_order` can be `auto`, `vertical`, `horizontal`.
Cancel: `{type:"cancel", id}`. New requests cancel previous job, latest wins.

Flow control: after scheduling each `audio.chunk`, send `{type:"audio.ack", id, index}`. The server holds at most one unacknowledged chunk and stops with an error after 60 seconds without an acknowledgement. The browser limits queued playback to about 20 seconds and waits for playback progress before acknowledging further chunks. Cancel and replacement requests interrupt this wait immediately; acknowledgements for stale requests are ignored.

Server JSON events all carry request `id` except ready/auth error:
- `{type:"status", id, stage:"ocr"|"tts", message}`
- `{type:"transcript", id, text}`
- `{type:"audio.chunk", id, index, format:"pcm_s16le", sample_rate:48000, channels:1, data_base64}` or format `wav` for completed sentence chunks fallback.
- `{type:"completed", id}` synthesis finished (queued browser audio may remain).
- `{type:"cancelled", id}`
- `{type:"error", id, message, code}`

Audio is streamed to browser and held only while playback. Playback speed is a browser parameter (`0.5`–`2`), independent of synthesis. No screenshot/text/generated audio persistence or content logging. Reference voices are deliberately persistent and removable. Token stored Chrome local storage, never in URL or source control. Upstream speech.cpp details are in upstream-notes.md.
