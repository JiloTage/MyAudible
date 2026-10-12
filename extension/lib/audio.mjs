import {parsePCM16} from './protocol.mjs';

export class AudioPlayer {
  constructor({contextFactory = () => new AudioContext(), onState = () => {}, maxAheadSeconds = 20} = {}) {
    this.contextFactory = contextFactory;
    this.onState = onState;
    this.context = null;
    this.sources = new Set();
    this.nextAt = 0;
    this.generation = 0;
    this.decodeQueue = Promise.resolve();
    this.maxAheadSeconds = maxAheadSeconds;
    this.waiters = new Set();
    this.queuedEncodedBytes = 0;
  }
  async unlock() {
    this.context ||= this.contextFactory();
    await this.context.resume();
    if (this.context.state !== 'running') throw new Error('音声を有効にするには「今の画面を読む」または「自動読み上げ」を押してください。');
  }
  stop() {
    this.generation++;
    for (const cancel of this.waiters) cancel();
    this.waiters.clear();
    for (const source of this.sources) {
      try { source.stop(); } catch {}
      source.disconnect();
    }
    this.sources.clear();
    this.nextAt = this.context?.currentTime || 0;
    this.decodeQueue = Promise.resolve();
    this.queuedEncodedBytes = 0;
    this.onState('stopped');
  }
  waitForCapacity(generation) {
    if (generation !== this.generation) return Promise.resolve(false);
    if (this.nextAt - this.context.currentTime <= this.maxAheadSeconds) return Promise.resolve(true);
    return new Promise((resolve, reject) => {
      let timer;
      const finish = (result, error) => {
        clearTimeout(timer); this.waiters.delete(cancel);
        if (error) reject(error); else resolve(result);
      };
      const cancel = () => finish(false);
      const check = () => {
        if (generation !== this.generation) return finish(false);
        if (this.context.state !== 'running') return finish(false, new Error('音声再生が一時停止しています。停止してから読み上げを再開してください。'));
        if (this.nextAt - this.context.currentTime <= this.maxAheadSeconds) return finish(true);
        timer = setTimeout(check, 100);
      };
      this.waiters.add(cancel); check();
    });
  }
  enqueue(event) {
    const generation = this.generation;
    const encodedBytes = event.data_base64?.length || 0;
    if (encodedBytes > 8 * 1024 * 1024 || this.queuedEncodedBytes + encodedBytes > 8 * 1024 * 1024) return Promise.reject(new Error('音声バッファが上限を超えました。短いテキストに分けて再実行してください。'));
    this.queuedEncodedBytes += encodedBytes;
    this.decodeQueue = this.decodeQueue.then(async () => {
      if (generation !== this.generation) return false;
      if (!this.context || this.context.state !== 'running') throw new Error('ブラウザの音声再生が停止しています。読み上げボタンを押して再開してください。');
      let buffer;
      if (event.format === 'pcm_s16le') {
        const channels = Number(event.channels || 1);
        const rate = Number(event.sample_rate);
        if (!Number.isInteger(channels) || channels < 1 || channels > 2 || !Number.isFinite(rate) || rate < 8000 || rate > 192000) throw new Error('音声形式が不正です。');
        const samples = parsePCM16(event.data_base64, channels);
        if (!samples[0].length) return true;
        buffer = this.context.createBuffer(channels, samples[0].length, rate);
        samples.forEach((channel, i) => buffer.copyToChannel(channel, i));
      } else if (event.format === 'wav') {
        const bytes = Uint8Array.from(atob(event.data_base64), char => char.charCodeAt(0));
        buffer = await this.context.decodeAudioData(bytes.buffer);
      } else throw new Error('非対応の音声形式です。');
      if (buffer.duration > 30) throw new Error('音声チャンクが長すぎます。停止して、短いテキストで再実行してください。');
      if (!await this.waitForCapacity(generation)) return false;
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.context.destination);
      const when = Math.max(this.context.currentTime + 0.035, this.nextAt);
      this.nextAt = when + buffer.duration;
      this.sources.add(source);
      source.onended = () => {
        this.sources.delete(source);
        source.disconnect();
        if (!this.sources.size) this.onState('idle');
      };
      source.start(when);
      this.onState('playing');
      return true;
    }).finally(() => { if (generation === this.generation) this.queuedEncodedBytes -= encodedBytes; });
    return this.decodeQueue;
  }
  async close() { this.stop(); await this.context?.close(); }
}
