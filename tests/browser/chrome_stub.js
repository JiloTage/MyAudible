// Test-only boundaries. Production panel.js, screenshot processing, detector,
// protocol parsing, and AudioPlayer all run unchanged in a real Chromium page.
(() => {
  const nativeFetch = window.fetch.bind(window);
  const persisted = JSON.parse(sessionStorage.getItem('test.chrome.storage') || '{}');
  const settings = {token: 'TEST-LOCAL-TOKEN', ...persisted};
  const event = () => ({listeners: new Set(), addListener(fn) { this.listeners.add(fn); },
    removeListener(fn) { this.listeners.delete(fn); }, dispatch(...args) { for (const fn of this.listeners) fn(...args); }});
  window.__mock = {
    activeTab: {id: 1, windowId: 2, title: '読書テスト', url: 'https://video.unext.jp/book/fixture'},
    saved: [], captures: 0, fetches: [], executions: [], sources: [], contexts: [],
    voices: JSON.parse(sessionStorage.getItem('test.voices') || '[]'),
    captureRace: null, focusedWindow: 2,
  };
  const events = {activated: event(), updated: event(), removed: event(), message: event(), focused: event()};
  const mock = window.__mock;
  window.chrome = {
    storage: {local: {
      get: async keys => Object.fromEntries(keys.filter(key => key in settings).map(key => [key, settings[key]])),
      set: async values => {Object.assign(settings, values); mock.saved.push(values);
        sessionStorage.setItem('test.chrome.storage', JSON.stringify(settings));},
    }},
    tabs: {
      query: async () => [mock.activeTab],
      captureVisibleTab: async () => {
        mock.captures++;
        const result = await window.__captureVisibleFixture();
        if (mock.captureRace) { mock.captureRace(); mock.captureRace = null; }
        return result;
      },
      sendMessage: async () => {},
      onActivated: events.activated, onUpdated: events.updated, onRemoved: events.removed,
    },
    scripting: {executeScript: async options => {mock.executions.push(options); return [{result: undefined}];}},
    runtime: {onMessage: events.message},
    windows: {get: async id => ({id, focused: id === mock.focusedWindow}), onFocusChanged: events.focused},
  };
  mock.activate = (id, windowId = 2) => {
    mock.activeTab = {...mock.activeTab, id, windowId};
    events.activated.dispatch({tabId: id, windowId});
  };
  mock.update = info => events.updated.dispatch(1, info);
  mock.focusWindow = id => {mock.focusedWindow = id; events.focused.dispatch(id);};
  mock.hint = () => events.message.dispatch({type: 'myaudible.page-hint'}, {tab: {id: 1}});
  window.fetch = async (url, init = {}) => {
    const address = String(url);
    if (!address.startsWith('http://127.0.0.1:8765')) return nativeFetch(url, init);
    const path = new URL(address).pathname;
    mock.fetches.push({path, method: init.method || 'GET', authorization: init.headers?.Authorization});
    const respond = (value, status = 200) => Promise.resolve(new Response(JSON.stringify(value),
      {status, headers: {'Content-Type': 'application/json'}}));
    if (path === '/health') return respond({status: 'ok', ocr_ready: true, tts_ready: true});
    if (init.headers?.Authorization !== 'Bearer TEST-LOCAL-TOKEN') return respond({detail: 'unauthorized'}, 401);
    if (path === '/v1/voices' && init.method === 'POST') {
      const voice = {id: `voice-${mock.voices.length + 1}`, name: init.body.get('name')};
      mock.voices.push(voice);
      sessionStorage.setItem('test.voices', JSON.stringify(mock.voices));
      return respond(voice);
    }
    if (path === '/v1/voices') return respond({voices: mock.voices});
    if (path.startsWith('/v1/voices/') && init.method === 'DELETE') {
      mock.voices = mock.voices.filter(voice => voice.id !== path.split('/').pop());
      sessionStorage.setItem('test.voices', JSON.stringify(mock.voices));
      return respond({deleted: true});
    }
    return respond({detail: 'unknown path'}, 404);
  };
  const frames = [];
  window.__backend = {frames, sockets: [], emit(id, type, fields = {}) {
    const socket = this.sockets.at(-1);
    socket.emit({type, id, ...fields});
  }};
  class FakeWebSocket extends EventTarget {
    static OPEN = 1;
    static CLOSED = 3;
    constructor(url) {
      super(); this.url = url; this.readyState = 0;
      window.__backend.sockets.push(this);
      setTimeout(() => { this.readyState = 1; this.dispatchEvent(new Event('open')); }, 0);
    }
    send(raw) {
      const message = JSON.parse(raw); frames.push(message);
      if (message.type === 'auth') queueMicrotask(() => this.emit(
        message.token === 'TEST-LOCAL-TOKEN' ? {type: 'ready'} : {type: 'error', message: 'unauthorized'}));
    }
    emit(message) { this.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(message)})); }
    close() { if (this.readyState === 3) return; this.readyState = 3; this.dispatchEvent(new Event('close')); }
  }
  window.WebSocket = FakeWebSocket;
  class InstrumentedAudioContext {
    constructor() { this.state = 'suspended'; this.destination = {}; mock.contexts.push(this); }
    get currentTime() { return performance.now() / 1000; }
    async resume() { this.state = 'running'; }
    async close() { this.state = 'closed'; }
    createBuffer(channels, count, rate) { return {duration: count / rate, copyToChannel() {}}; }
    createBufferSource() {
      const source = {playbackRate: {value: 1}, starts: 0, stops: 0, disconnects: 0,
        connect() {}, start(when) { this.starts++; this.when = when; },
        stop() { this.stops++; }, disconnect() { this.disconnects++; }};
      mock.sources.push(source); return source;
    }
  }
  window.AudioContext = InstrumentedAudioContext;
})();
