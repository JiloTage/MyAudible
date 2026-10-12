import test from 'node:test';
import assert from 'node:assert/strict';
import {PageTurnDetector, visualDifference, normalizeCrop} from '../lib/detector.mjs';
import {captureLockedTab, CaptureSafetyError} from '../lib/capture.mjs';
import {parsePCM16, isCurrentEvent, backendURL} from '../lib/protocol.mjs';
import {AudioPlayer} from '../lib/audio.mjs';

function page(glyphPosition) {
  const pixels = new Uint8Array(160 * 160).fill(255);
  for (let y = 30; y < 36; y++) for (let x = glyphPosition; x < glyphPosition + 4; x++) pixels[y * 160 + x] = 20;
  return pixels;
}

test('single similar-text page turn stays detectable and waits for animation to settle', () => {
  const before = page(30), after = page(40), transition = page(35);
  assert.ok(visualDifference(before, after) >= 0.5);
  const detector = new PageTurnDetector();
  assert.equal(detector.observe(before, 0).type, 'settling');
  assert.equal(detector.observe(before, 1200).type, 'page');
  assert.equal(detector.observe(before, 2400).type, 'unchanged');
  assert.equal(detector.observe(transition, 3600).type, 'changing');
  assert.equal(detector.observe(after, 4800).type, 'settling');
  assert.equal(detector.observe(after, 6000).type, 'page');
  assert.equal(detector.observe(after, 7200).type, 'unchanged');
});

test('transient changes are discarded and longer settling time is respected', () => {
  const detector = new PageTurnDetector({stableMs: 2500});
  const before = page(30), after = page(40);
  detector.observe(before, 0);
  assert.equal(detector.observe(before, 1200).type, 'settling');
  assert.equal(detector.observe(before, 2600).type, 'page');
  assert.equal(detector.observe(after, 3800).type, 'changing');
  assert.equal(detector.observe(before, 5000).type, 'reverted');
  assert.equal(detector.observe(before, 6200).type, 'unchanged');
});

test('crop stays within screen boundaries when its origin moves', () => {
  const region = normalizeCrop({x: .9, y: .8, width: .9, height: .84});
  assert.ok(region.x + region.width <= 1);
  assert.ok(region.y + region.height <= 1);
});

test('capture refuses another active tab before exposing any image', async () => {
  let captures = 0;
  await assert.rejects(captureLockedTab({tabId: 1, windowId: 2, queryActive: async () => ({id: 3, windowId: 2}), capture: async () => { captures++; return 'private'; }, revision: () => 0}), CaptureSafetyError);
  assert.equal(captures, 0);
});

test('capture permission failure explains how to grant access', async () => {
  await assert.rejects(captureLockedTab({
    tabId: 1, windowId: 2, queryActive: async () => ({id: 1, windowId: 2}),
    capture: async () => { throw new Error("Either the '<all_urls>' or 'activeTab' permission is required."); },
    revision: () => 0
  }), /再読み込み.*サイトへのアクセス/);
});

test('capture discards tab switch, switch-return, and window switch races', async () => {
  for (const race of ['tab', 'return', 'window']) {
    let revision = 0, calls = 0;
    await assert.rejects(captureLockedTab({
      tabId: 1, windowId: 2,
      queryActive: async () => { calls++; return calls === 1 || race === 'return' ? {id: 1, windowId: 2} : race === 'tab' ? {id: 3, windowId: 2} : null; },
      capture: async () => { revision++; return 'private'; }, revision: () => revision
    }), CaptureSafetyError);
  }
});

test('PCM decoding preserves little-endian signed stereo samples', () => {
  const encoded = btoa(String.fromCharCode(0, 128, 255, 127, 0, 0, 0, 64));
  const channels = parsePCM16(encoded, 2);
  assert.deepEqual([...channels[0]], [-1, 0]);
  assert.deepEqual([...channels[1]], [32767 / 32768, .5]);
  assert.throws(() => parsePCM16(btoa('123'), 1));
});

test('stale synthesis IDs and non-loopback backend URLs are rejected', () => {
  assert.equal(isCurrentEvent({id: 'old'}, 'current'), false);
  assert.equal(isCurrentEvent({id: null}, null), false);
  assert.equal(isCurrentEvent({id: 'current'}, 'current'), true);
  assert.equal(backendURL('http://127.0.0.1:8765'), 'http://127.0.0.1:8765');
  assert.equal(backendURL('http://localhost:9999'), 'http://localhost:9999');
  for (const url of ['http://example.com:8765', 'http://localhost', 'http://localhost:1023', 'http://secret@localhost:8765', 'http://localhost:8765/?token=private']) assert.throws(() => backendURL(url));
});

function fakeContext({canResume = true} = {}) {
  const sources = [];
  return {
    currentTime: 0, state: 'suspended', destination: {}, sources,
    async resume() { if (canResume) this.state = 'running'; },
    async close() { this.state = 'closed'; },
    createBuffer(channels, frames, rate) { return {duration: frames / rate, copyToChannel() {}}; },
    createBufferSource() {
      const source = {playbackRate: {value: 1}, connect() {}, disconnect() {}, start(time) { this.time = time; }, stop() { this.stopped = true; }};
      sources.push(source); return source;
    }
  };
}
function audioChunk() { return {format: 'pcm_s16le', sample_rate: 8000, channels: 1, data_base64: btoa('\0'.repeat(16000))}; }

test('audio needs a successful gesture unlock before receiving chunks', async () => {
  const context = fakeContext({canResume: false});
  const player = new AudioPlayer({contextFactory: () => context});
  await assert.rejects(player.unlock(), /音声を有効/);
  await assert.rejects(player.enqueue(audioChunk()), /音声再生が停止/);
  assert.equal(context.sources.length, 0);
  await player.close();
});

test('cancel releases pending audio credit and prevents a queued stale chunk from playing', async () => {
  const context = fakeContext();
  const player = new AudioPlayer({contextFactory: () => context, maxAheadSeconds: .05});
  await player.unlock();
  assert.equal(await player.enqueue(audioChunk()), true);
  const pending = player.enqueue(audioChunk());
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(context.sources.length, 1);
  player.stop();
  assert.equal(await pending, false);
  assert.ok(context.sources.every(source => source.stopped));
  assert.equal(player.sources.size, 0);
  await player.close();
  assert.equal(context.state, 'closed');
});
