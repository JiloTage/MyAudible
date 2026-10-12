// Hints only. Canvas rendering and inaccessible frames are always checked by capture polling.
(() => {
  if (globalThis.__myAudibleHints) return;
  let lastSent = 0;
  function hint() {
    const now = Date.now();
    if (now - lastSent < 350) return;
    lastSent = now;
    chrome.runtime.sendMessage({type: 'myaudible.page-hint'}).catch(() => {});
  }
  const observer = new MutationObserver(hint);
  observer.observe(document, {subtree: true, childList: true, attributes: true});
  const controller = new AbortController();
  for (const name of ['popstate', 'hashchange', 'pageshow', 'scroll', 'keyup', 'pointerup']) {
    addEventListener(name, hint, {passive: true, signal: controller.signal});
  }
  const dispose = () => { observer.disconnect(); controller.abort(); delete globalThis.__myAudibleHints; };
  chrome.runtime.onMessage.addListener(function listener(message) {
    if (message.type !== 'myaudible.stop-hints') return;
    dispose();
    chrome.runtime.onMessage.removeListener(listener);
  });
  globalThis.__myAudibleHints = {dispose};
})();
