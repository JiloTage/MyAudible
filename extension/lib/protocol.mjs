export function backendURL(value) {
  const parsed = new URL(value || 'http://127.0.0.1:8765');
  if (parsed.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(parsed.hostname) || Number(parsed.port) < 1024 || Number(parsed.port) > 65535 || parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname !== '/') {
    throw new Error('接続先は http://127.0.0.1:ポート または http://localhost:ポート（1024〜65535）を指定してください。');
  }
  return parsed.origin;
}

export function isCurrentEvent(event, id) {
  return Boolean(id && event.id === id);
}

export function parsePCM16(base64, channels = 1) {
  const raw = atob(base64);
  if (raw.length % (2 * channels) !== 0) throw new Error('PCMチャンクの長さが不正です。');
  const view = new DataView(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) view.setUint8(i, raw.charCodeAt(i));
  const frames = raw.length / 2 / channels;
  const audio = Array.from({length: channels}, () => new Float32Array(frames));
  for (let frame = 0; frame < frames; frame++) {
    for (let channel = 0; channel < channels; channel++) {
      audio[channel][frame] = view.getInt16((frame * channels + channel) * 2, true) / 32768;
    }
  }
  return audio;
}
