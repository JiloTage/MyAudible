import {PageTurnDetector, normalizeCrop} from './lib/detector.mjs';
import {captureLockedTab, CaptureSafetyError, prepareScreenshot} from './lib/capture.mjs';
import {backendURL, isCurrentEvent} from './lib/protocol.mjs';
import {AudioPlayer} from './lib/audio.mjs';

const $ = id => document.getElementById(id);
const storedKeys = ['backend', 'token', 'voice', 'steps', 'seed', 'rate', 'instructions', 'cropX', 'cropY', 'cropWidth', 'cropHeight', 'threshold', 'stableMs', 'readingOrder'];
let selectedTab = null;
let activationRevision = 0;
let monitoring = false;
let monitorTimer = null;
let pollBusy = false;
let monitoringRevision = 0;
let detector = createDetector();
let socket = null;
let socketReady = null;
let currentRequestId = null;
let requestRevision = 0;
let previewBitmap = null;
let previewDrag = null;
let closed = false;
let healthTimer = null;
let navigationLoading = false;
const player = new AudioPlayer({onState: state => {
  $('playback').textContent = state === 'playing' ? '音声を再生中' : '';
}});

function status(message) { $('status').textContent = message; }
function createDetector() { return new PageTurnDetector({threshold: Math.max(0.1, Number($('threshold')?.value || 0.5)), stableMs: Math.max(500, Math.min(5000, Number($('stableMs')?.value || 600)))}); }
function crop() { return normalizeCrop({x: Number($('cropX').value) / 100, y: Number($('cropY').value) / 100, width: Number($('cropWidth').value) / 100, height: Number($('cropHeight').value) / 100}); }
function applyCrop(value) {
  const normalized = normalizeCrop(value);
  for (const [key, id] of [['x', 'cropX'], ['y', 'cropY'], ['width', 'cropWidth'], ['height', 'cropHeight']]) $(''+id).value = (normalized[key] * 100).toFixed(1);
}
async function saveSettings() {
  await chrome.storage.local.set(Object.fromEntries(storedKeys.map(key => [key, $(key).value])));
}
function config() {
  const base = backendURL($('backend').value);
  const token = $('token').value.trim();
  if (!token) throw new Error('バックエンドのトークンを入力してください。');
  return {base, token};
}
function options() {
  const speed = Number($('rate').value);
  if (!Number.isFinite(speed) || speed < 0.25 || speed > 4) throw new Error('読み上げ速度は0.25〜4の範囲で指定してください。');
  const bounded = (id, min, max) => {
    const value = Number($(id).value);
    if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${$(id).closest('label').firstChild.textContent.trim()}の範囲が不正です。`);
    return value;
  };
  return {
    voice_id: $('voice').value || null,
    steps: bounded('steps', 1, 100),
    speed,
    ...($('seed').value.trim() ? {seed: bounded('seed', 0, 2147483647)} : {}),
    ...($('instructions').value.trim() ? {instructions: $('instructions').value.trim()} : {})
  };
}
async function authenticatedFetch(path, init = {}) {
  const {base, token} = config();
  const response = await fetch(base + path, {...init, headers: {...init.headers, Authorization: `Bearer ${token}`}});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.message || `接続エラー (${response.status})`);
  return result;
}
async function checkHealth() {
  try {
    const base = backendURL($('backend').value);
    const response = await fetch(base + '/health', {signal: AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error('health');
    const result = await response.json();
    const tts = result.tts?.ready ?? result.tts_ready ?? false;
    const ocr = result.ocr?.available ?? result.ocr_ready ?? false;
    $('health').textContent = `OCR ${ocr ? '準備OK' : '未準備'} / 音声 ${tts ? '準備OK' : '未準備'}`;
    $('health').title = result.message || result.tts?.error || result.ocr?.error || '';
  } catch {
    $('health').textContent = 'バックエンド未接続';
    $('health').title = '「サーバーを起動して接続」を押すか、ローカルPCでバックエンドを起動してください。';
  }
}
async function loadVoices(preferred = $('voice').value) {
  const result = await authenticatedFetch('/v1/voices');
  $('voice').replaceChildren(new Option('参照音声なし', ''));
  for (const voice of result.voices || []) $('voice').add(new Option(voice.name, voice.id));
  if ([...$('voice').options].some(item => item.value === preferred)) $('voice').value = preferred;
}
async function startServer() {
  if ($('startServer').disabled) return;
  $('startServer').disabled = true;
  $('startServer').textContent = 'サーバーを起動中…';
  stopMonitor(); disconnectSocket();
  status('ローカルサーバーを起動しています。初回はモデルの準備に時間がかかる場合があります。');
  try {
    let result;
    try {
      result = await chrome.runtime.sendNativeMessage('com.myaudible.launcher', {type: 'start'});
    } catch (error) {
      throw new Error(`起動用ランチャーに接続できません。PCで scripts\\install-native-host.ps1 を実行してから再試行してください。\n${error.message}`);
    }
    if (!result?.ok) throw new Error(result?.error || 'サーバーの起動に失敗しました。');
    const base = backendURL(result.url);
    if (typeof result.token !== 'string' || result.token.length < 24) throw new Error('起動用ランチャーからの接続情報が不正です。');
    if (closed) return;
    $('backend').value = base;
    $('token').value = result.token;
    await saveSettings();
    await checkHealth();
    await loadVoices();
    status(result.already_running ? '起動済みのサーバーに接続しました。' : 'サーバーを起動して接続しました。');
  } finally {
    $('startServer').disabled = false;
    $('startServer').textContent = 'サーバーを起動して接続';
  }
}

function disconnectSocket() {
  currentRequestId = null;
  requestRevision++;
  player.stop();
  socketReady = null;
  if (socket) { socket.close(1000, 'panel reset'); socket = null; }
}
async function ensureSocket() {
  if (socketReady) return socketReady;
  const {base, token} = config();
  const ws = new WebSocket(base.replace('http:', 'ws:') + '/v1/realtime');
  socket = ws;
  socketReady = new Promise((resolve, reject) => {
    let ready = false;
    const timeout = setTimeout(() => { reject(new Error('バックエンド認証がタイムアウトしました。')); ws.close(); }, 8000);
    ws.addEventListener('open', () => ws.send(JSON.stringify({type: 'auth', token})));
    ws.addEventListener('message', event => {
      if (socket !== ws) return;
      let message;
      try { message = JSON.parse(event.data); } catch { status('バックエンドの応答が不正です。'); return; }
      if (message.type === 'ready') { ready = true; clearTimeout(timeout); resolve(ws); return; }
      if (!ready && message.type === 'error') { clearTimeout(timeout); reject(new Error(message.message || '認証に失敗しました。')); return; }
      if (!isCurrentEvent(message, currentRequestId)) return;
      if (message.type === 'status') status(message.message || (message.stage === 'ocr' ? 'OCR処理中…' : '音声生成中…'));
      if (message.type === 'transcript') $('transcript').textContent = message.text;
      if (message.type === 'audio.chunk') {
        player.enqueue(message).then(accepted => {
          if (accepted && isCurrentEvent(message, currentRequestId) && socket === ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'audio.ack', id: message.id, index: message.index}));
        }).catch(error => {
          if (isCurrentEvent(message, currentRequestId)) { cancelCurrent(); status(error.message); }
        });
      }
      if (message.type === 'completed') status(monitoring ? '生成完了。次のページを監視中。' : '生成完了。');
      if (message.type === 'cancelled') status('読み上げを停止しました。');
      if (message.type === 'error') { player.stop(); status(message.message || '読み上げに失敗しました。'); }
    });
    ws.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('音声サーバーに接続できません。')); });
    ws.addEventListener('close', () => {
      clearTimeout(timeout);
      if (!ready) reject(new Error('音声サーバーとの接続が終了しました。'));
      if (socket === ws) {
        socket = null; socketReady = null;
        currentRequestId = null; requestRevision++; player.stop();
        if (!closed) status(monitoring ? '接続が切れました。停止してから自動読み上げを再開してください。' : '音声サーバーとの接続が終了しました。');
        stopMonitor();
      }
    });
  });
  try { return await socketReady; } catch (error) {
    if (socket === ws) { socket = null; socketReady = null; }
    ws.close();
    throw error;
  }
}
function cancelCurrent() {
  const id = currentRequestId;
  currentRequestId = null;
  requestRevision++;
  if (id && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: 'cancel', id}));
  player.stop();
}
async function synthesize(input) {
  cancelCurrent();
  const revision = requestRevision;
  const params = options();
  const ws = await ensureSocket();
  if (revision !== requestRevision || closed) return;
  currentRequestId = crypto.randomUUID();
  $('transcript').textContent = '';
  status(input.type === 'image' ? '画面を送信。OCR処理を開始…' : 'テキストを送信。音声生成を開始…');
  ws.send(JSON.stringify({type: 'synthesize', id: currentRequestId, input, options: params}));
}

async function activeInWindow(windowId) {
  const window = await chrome.windows.get(windowId);
  if (!window.focused) return null;
  const [tab] = await chrome.tabs.query({active: true, windowId});
  return tab;
}
async function selectTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab?.id || !tab.windowId) throw new Error('対象タブが見つかりません。');
  if (/^(chrome|chrome-extension|edge|about):/.test(tab.url || '')) throw new Error('通常のウェブページで拡張機能のアイコンを押してください。');
  stopMonitor(); cancelCurrent();
  selectedTab = {id: tab.id, windowId: tab.windowId};
  navigationLoading = false;
  $('target').textContent = `対象: ${tab.title || '現在のタブ'} (#${tab.id})`;
  detector.reset();
  await injectHints();
  return selectedTab;
}
async function injectHints() {
  if (!selectedTab) return;
  try { await chrome.scripting.executeScript({target: {tabId: selectedTab.id, allFrames: true}, files: ['page-hints.js']}); }
  catch {
    try { await chrome.scripting.executeScript({target: {tabId: selectedTab.id}, files: ['page-hints.js']}); } catch {}
  }
}
async function captureFrame() {
  if (!selectedTab) await selectTab();
  const {id: tabId, windowId} = selectedTab;
  const dataURL = await captureLockedTab({
    tabId, windowId, queryActive: activeInWindow,
    capture: window => chrome.tabs.captureVisibleTab(window, {format: 'png'}),
    revision: () => activationRevision
  });
  const frame = await prepareScreenshot(dataURL, crop());
  return {...frame, image: () => ({type: 'image', data_base64: frame.canvas.toDataURL('image/png').split(',')[1], mime_type: 'image/png', reading_order: $('readingOrder').value})};
}
function schedulePoll(delay = 1200) {
  clearTimeout(monitorTimer);
  if (monitoring && !closed) monitorTimer = setTimeout(pollPage, Math.max(1000, delay));
}
async function pollPage() {
  if (!monitoring || closed || pollBusy) return;
  pollBusy = true;
  const generation = monitoringRevision;
  let frame = null;
  try {
    if (navigationLoading) { status('対象ページの読み込みを待っています…'); return; }
    const tab = await activeInWindow(selectedTab.windowId);
    if (tab?.id !== selectedTab.id) {
      status('対象タブが前面ではないため、自動読み上げを一時停止中。');
      return;
    }
    frame = await captureFrame();
    if (!monitoring || generation !== monitoringRevision) return;
    const event = detector.observe(frame.signature, Date.now());
    if (event.type === 'changing') { cancelCurrent(); status('画面の変化を検出。ページの静止を待っています…'); }
    if (event.type === 'page') await synthesize(frame.image());
  } catch (error) {
    if (error instanceof CaptureSafetyError) status('対象タブが前面ではないため、自動読み上げを一時停止中。');
    else { stopMonitor(); cancelCurrent(); status(`画面の取得に失敗: ${error.message}`); }
  } finally { frame?.release(); pollBusy = false; schedulePoll(); }
}
function stopMonitor() {
  monitoring = false;
  monitoringRevision++;
  clearTimeout(monitorTimer); monitorTimer = null;
  $('startMonitor').disabled = false;
  if (selectedTab) chrome.tabs.sendMessage(selectedTab.id, {type: 'myaudible.stop-hints'}).catch(() => {});
}
async function startMonitor() {
  await player.unlock();
  options(); config();
  if (!selectedTab) await selectTab();
  stopMonitor(); cancelCurrent();
  const generation = monitoringRevision;
  $('startMonitor').disabled = true;
  try {
    await ensureSocket();
    if (generation !== monitoringRevision || closed) return;
    await injectHints();
    if (generation !== monitoringRevision || closed) return;
    detector = createDetector();
    monitoring = true;
    status('自動読み上げを開始。画面が静止するまで待っています…');
    pollPage();
  } catch (error) {
    if (generation === monitoringRevision) $('startMonitor').disabled = false;
    throw error;
  }
}
async function readOnce() {
  await player.unlock();
  if (!selectedTab) await selectTab();
  cancelCurrent();
  const revision = ++requestRevision;
  const frame = await captureFrame();
  try {
    if (revision !== requestRevision) return;
    // Manual reading also establishes the monitored page, avoiding a duplicate first read.
    detector.reset();
    detector.observe(frame.signature, Date.now() - Number($('stableMs').value) - 100);
    detector.observe(frame.signature, Date.now());
    await synthesize(frame.image());
  } finally { frame.release(); }
}

function clearPreview() {
  previewBitmap?.close(); previewBitmap = null; previewDrag = null;
  $('previewCanvas').width = $('previewCanvas').height = 0;
  $('previewBox').hidden = true;
}
function drawPreview() {
  if (!previewBitmap) return;
  const canvas = $('previewCanvas');
  const ctx = canvas.getContext('2d');
  ctx.drawImage(previewBitmap, 0, 0, canvas.width, canvas.height);
  const region = crop();
  const x = region.x * canvas.width, y = region.y * canvas.height;
  const width = region.width * canvas.width, height = region.height * canvas.height;
  ctx.fillStyle = '#0008';
  ctx.fillRect(0, 0, canvas.width, y);
  ctx.fillRect(0, y, x, height);
  ctx.fillRect(x + width, y, canvas.width - x - width, height);
  ctx.fillRect(0, y + height, canvas.width, canvas.height - y - height);
  ctx.strokeStyle = '#ff3b30'; ctx.lineWidth = Math.max(2, canvas.width / 250);
  ctx.strokeRect(x, y, width, height);
}
async function showPreview() {
  if (!selectedTab) await selectTab();
  const {id: tabId, windowId} = selectedTab;
  const dataURL = await captureLockedTab({tabId, windowId, queryActive: activeInWindow, capture: window => chrome.tabs.captureVisibleTab(window, {format: 'png'}), revision: () => activationRevision});
  clearPreview();
  previewBitmap = await createImageBitmap(await (await fetch(dataURL)).blob());
  $('previewCanvas').width = previewBitmap.width;
  $('previewCanvas').height = previewBitmap.height;
  $('previewBox').hidden = false;
  drawPreview();
}
function pointerPosition(event) {
  const rect = $('previewCanvas').getBoundingClientRect();
  return {x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))};
}
$('previewCanvas').addEventListener('pointerdown', event => {
  previewDrag = pointerPosition(event);
  $('previewCanvas').setPointerCapture(event.pointerId);
});
$('previewCanvas').addEventListener('pointermove', event => {
  if (!previewDrag) return;
  const current = pointerPosition(event);
  applyCrop({x: Math.min(previewDrag.x, current.x), y: Math.min(previewDrag.y, current.y), width: Math.abs(current.x - previewDrag.x), height: Math.abs(current.y - previewDrag.y)});
  drawPreview();
});
$('previewCanvas').addEventListener('pointerup', () => {
  if (!previewDrag) return;
  previewDrag = null; detector = createDetector(); cancelCurrent();
  saveSettings().catch(error => status(error.message));
});
$('previewCanvas').addEventListener('pointercancel', () => { previewDrag = null; });

function run(task) { Promise.resolve().then(task).catch(error => status(error.message || String(error))); }
$('startServer').addEventListener('click', () => run(startServer));
$('connect').addEventListener('click', () => run(async () => { stopMonitor(); disconnectSocket(); await saveSettings(); await checkHealth(); await loadVoices(); status('接続しました。'); }));
$('selectTab').addEventListener('click', () => run(async () => { await selectTab(); status('対象タブを選択しました。'); }));
$('readOnce').addEventListener('click', () => run(readOnce));
$('startMonitor').addEventListener('click', () => run(startMonitor));
$('stop').addEventListener('click', () => { stopMonitor(); cancelCurrent(); status('停止しました。'); });
$('sendText').addEventListener('click', () => run(async () => {
  await player.unlock();
  const text = $('textInput').value.trim();
  if (!text) throw new Error('読み上げるテキストを入力してください。');
  stopMonitor();
  await synthesize({type: 'text', text});
}));
$('preview').addEventListener('click', () => run(showPreview));
$('closePreview').addEventListener('click', clearPreview);
$('uploadVoice').addEventListener('click', () => run(async () => {
  const name = $('voiceName').value.trim();
  const audio = $('voiceFile').files[0];
  if (!name || !audio) throw new Error('参照音声の名前と音声ファイルを指定してください。');
  if (audio.size > 12 * 1024 * 1024) throw new Error('参照音声は12MiB以下のPCM WAVを指定してください。');
  const form = new FormData(); form.set('name', name); form.set('audio', audio);
  status('参照音声を登録中…');
  const voice = await authenticatedFetch('/v1/voices', {method: 'POST', body: form});
  await loadVoices(); $('voice').value = voice.id;
  $('voiceFile').value = ''; $('voiceName').value = '';
  await saveSettings(); status('参照音声を登録しました。');
}));
$('deleteVoice').addEventListener('click', () => run(async () => {
  const id = $('voice').value;
  if (!id) throw new Error('削除する参照音声を選択してください。');
  await authenticatedFetch(`/v1/voices/${encodeURIComponent(id)}`, {method: 'DELETE'});
  await loadVoices(); await saveSettings(); status('参照音声を削除しました。');
}));
for (const id of storedKeys) $(id).addEventListener('change', () => {
  if (id.startsWith('crop') || id === 'threshold' || id === 'stableMs') { applyCrop(crop()); detector = createDetector(); cancelCurrent(); drawPreview(); }
  if (id === 'token' || id === 'backend') { stopMonitor(); disconnectSocket(); }
  saveSettings().catch(error => status(error.message));
});

chrome.tabs.onActivated.addListener(info => {
  activationRevision++;
  if (monitoring && selectedTab && info.windowId === selectedTab.windowId && info.tabId !== selectedTab.id) {
    cancelCurrent();
    status('対象タブが前面ではないため、自動読み上げを一時停止中。');
  }
});
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId !== selectedTab?.id) return;
  if (changeInfo.status === 'loading') {
    navigationLoading = true;
    activationRevision++; cancelCurrent(); detector.reset();
    if (monitoring) status('ページ移動を検出。読み込みを待っています…');
  }
  if (changeInfo.status === 'complete') { navigationLoading = false; if (monitoring) injectHints(); }
});
chrome.windows.onFocusChanged.addListener(windowId => {
  activationRevision++;
  if (monitoring && selectedTab && windowId !== selectedTab.windowId) {
    cancelCurrent(); status('対象ウィンドウが前面ではないため、自動読み上げを一時停止中。');
  }
});
chrome.tabs.onRemoved.addListener(tabId => {
  if (tabId !== selectedTab?.id) return;
  activationRevision++; stopMonitor(); cancelCurrent(); selectedTab = null;
  $('target').textContent = '対象タブは閉じられました。';
  status('対象タブを選択し直してください。');
});
chrome.runtime.onMessage.addListener((message, sender) => {
  // Never postpone the continuous visual timer: animated DOM can emit hints forever.
  if (message.type === 'myaudible.page-hint' && sender.tab?.id === selectedTab?.id && monitoring) return;
});
function dispose() {
  if (closed) return;
  closed = true; stopMonitor(); cancelCurrent(); disconnectSocket();
  clearInterval(healthTimer); clearPreview();
  $('transcript').textContent = ''; $('textInput').value = '';
  player.close().catch(() => {});
}
window.addEventListener('pagehide', dispose);
window.addEventListener('unload', dispose);

async function initialize() {
  const saved = await chrome.storage.local.get(storedKeys);
  for (const id of storedKeys) if (id !== 'voice' && saved[id] !== undefined) $(id).value = saved[id];
  applyCrop(crop()); detector = createDetector();
  await checkHealth();
  if ($('token').value) await loadVoices(saved.voice || '').catch(error => status(error.message));
  healthTimer = setInterval(checkHealth, 15000);
}
run(initialize);
