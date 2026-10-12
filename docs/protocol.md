# Local API contract

Backend: FastAPI, bound to `127.0.0.1:8765`, all data remains on user's PC.
`GET /health` is unauthenticated, contains readiness only: `{status:"ok"|"degraded", ocr_ready:boolean, tts_ready:boolean, message:string}`. `/v1/*` REST uses `Authorization: Bearer <token>`.
`GET /v1/voices` returns `{voices: [{id, name, created_at}]}`. `POST /v1/voices` multipart `name`, `audio` persists normalized reference audio and metadata, returns voice. `DELETE /v1/voices/{id}` removes voice.

WebSocket `/v1/realtime`: first frame `{type:"auth", token}`. Backend emits `{type:"ready", flow_control:"audio.ack"}` or error and closes. Requests:

```json
{"type":"synthesize", "id":"uuid", "input":{"type":"text","text":"こんにちは。"}, "options":{"voice_id":null,"steps":4,"speed":1.5,"instructions":"落ち着いた声", "seed":42}}
```

Image input instead: `{type:"image", data_base64:"...", mime_type:"image/png", reading_order:"vertical"}`. `reading_order` can be `auto`, `vertical`, `horizontal`.
Cancel: `{type:"cancel", id}`. New requests cancel previous job, latest wins.

Flow control: after scheduling each `audio.chunk`, send `{type:"audio.ack", id, index}`. The server holds at most one unacknowledged chunk and stops with an error after 60 seconds without an acknowledgement. The browser limits queued playback to about 20 seconds and waits for playback progress before acknowledging further chunks. Cancel and replacement requests interrupt this wait immediately; acknowledgements for stale requests are ignored.

Before TTS, the backend removes all Unicode whitespace from both OCR and direct text input, including newlines, tabs, ASCII/full-width spaces and non-breaking spaces. Punctuation is retained; English word spaces are removed too. The `transcript` event retains internal whitespace for inspection. Input that becomes empty is rejected with `empty_text` before contacting speech.cpp.

The backend submits the cleaned full text in one HTTP request. speech.cpp handles sentence splitting and reuses the same voice, options and seed within that request. The obsolete `chunk_chars` option is accepted from older installed panels but ignored and not forwarded.

The backend reads speech.cpp's SSE response independently of playback acknowledgements, buffering at most 64 MiB of pending PCM for the request. This avoids speech.cpp's HTTP write timeout while the browser waits for playback. The larger bound allows a full page to be received in one response. Cancellation closes the upstream response and discards pending audio. Excess pending audio returns `audio_too_large`; select a smaller reading area or submit less text.

Server JSON events all carry request `id` except ready/auth error:
- `{type:"status", id, stage:"ocr"|"tts", message}`
- `{type:"transcript", id, text}`
- `{type:"audio.chunk", id, index, format:"pcm_s16le", sample_rate:48000, channels:1, data_base64}` or format `wav` for completed sentence chunks fallback.
- `{type:"completed", id}` synthesis finished (queued browser audio may remain).
- `{type:"cancelled", id}`
- `{type:"error", id, message, code}`

Audio is streamed to browser and held only while playback. `options.speed` is passed to speech.cpp as the synthesis speed (`0.25`–`4`, default `1`); the panel offers `0.5`–`2`. The browser plays the returned audio at its normal rate without applying a second speed change. Changing the setting affects the next synthesis request; queued audio retains its generated speed. No screenshot/text/generated audio persistence or content logging. Reference voices are deliberately persistent and removable. Token stored Chrome local storage, never in URL or source control. Upstream speech.cpp details are in upstream-notes.md.

The panel can optionally click or scroll after an automatically detected page has finished playback. This is a browser setting, not a backend option. It requires both `completed` for the current request and the last queued WebAudio source's `ended` event. A fresh monitored screenshot must still match the read page before the operation. Manual text/image requests never run an after-read action. Stop, cancellation, replacement, errors, settings changes, navigation and focus changes invalidate pending operations. The action runs once per successful page; a click that leaves the screenshot unchanged does not trigger retries or rereading.

Action positions are percentages of the visible page viewport (`0`–`100`), independent of the OCR crop. Scroll amounts use viewport percentages (`-200`–`200`, with at least one nonzero axis); the container under the point is preferred over the whole document. The extension uses `chrome.scripting` and document IDs to target the read document and route into permitted nested frames, including other origins. Clicks are JavaScript pointer/mouse events, so pages requiring trusted user input may reject them. Settings persist, but previews remain in memory and are discarded when closed.
