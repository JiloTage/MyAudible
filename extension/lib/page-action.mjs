export function readActionOptions({type, x, y, scrollX, scrollY}) {
  if (type === 'none') return null;
  if (!['click', 'scroll'].includes(type)) throw new Error('読み上げ後の操作を選択してください。');
  const number = (value, min, max) => {
    const result = Number(value);
    if (!String(value).trim() || !Number.isFinite(result) || result < min || result > max) throw new Error('操作位置は0〜100%、スクロール量は−200〜200%で指定してください。');
    return result;
  };
  const action = {type, x: number(x, 0, 100), y: number(y, 0, 100)};
  if (type === 'scroll') {
    action.scrollX = number(scrollX, -200, 200);
    action.scrollY = number(scrollY, -200, 200);
    if (!action.scrollX && !action.scrollY) throw new Error('スクロール量を指定してください。');
  }
  return action;
}

// Parent window indexes can be read even for frames from another origin.
function actionFramePath() {
  const path = [];
  for (let child = window; child !== child.top; child = child.parent) {
    let index = 0;
    while (index < child.parent.length && child.parent.frames[index] !== child) index++;
    path.unshift(index);
  }
  return path;
}

function actAtPoint({action, point, delta}) {
  if (document.visibilityState !== 'visible') return {error: '対象タブが表示されていません。'};
  point ||= {x: Math.min(innerWidth - 1, innerWidth * action.x / 100), y: Math.min(innerHeight - 1, innerHeight * action.y / 100)};
  delta ||= {x: innerWidth * (action.scrollX || 0) / 100, y: innerHeight * (action.scrollY || 0) / 100};
  let element = document.elementFromPoint(point.x, point.y);
  while (element?.shadowRoot) {
    const inner = element.shadowRoot.elementFromPoint(point.x, point.y);
    if (!inner || inner === element) break;
    element = inner;
  }
  if (!element) return {error: '指定した位置に操作対象がありません。'};
  if (element.tagName === 'IFRAME' || element.tagName === 'FRAME') {
    const rect = element.getBoundingClientRect();
    let childIndex = 0;
    while (childIndex < window.length && window.frames[childIndex] !== element.contentWindow) childIndex++;
    return {childIndex, point: {
      x: (point.x - rect.left) * element.offsetWidth / rect.width - element.clientLeft,
      y: (point.y - rect.top) * element.offsetHeight / rect.height - element.clientTop,
    }, delta};
  }
  if (action.type === 'click') {
    if (element.closest(':disabled')) return {error: '指定した位置のボタンは無効です。'};
    const mouse = {bubbles: true, cancelable: true, composed: true, view: window, clientX: point.x, clientY: point.y, button: 0};
    // Moving focus draws a ring that can be mistaken for a new page.
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      const options = {...mouse, buttons: type.endsWith('down') ? 1 : 0};
      element.dispatchEvent(type.startsWith('pointer')
        ? new PointerEvent(type, {...options, pointerId: 1, pointerType: 'mouse', isPrimary: true})
        : new MouseEvent(type, options));
    }
    return {performed: true};
  }
  for (let node = element; node; node = node.parentElement || node.getRootNode().host) {
    const style = getComputedStyle(node);
    if ((delta.y && node.scrollHeight > node.clientHeight && /auto|scroll/.test(style.overflowY)) ||
        (delta.x && node.scrollWidth > node.clientWidth && /auto|scroll/.test(style.overflowX))) {
      const before = {x: node.scrollLeft, y: node.scrollTop};
      node.scrollBy({left: delta.x, top: delta.y, behavior: 'instant'});
      if (node.scrollLeft !== before.x || node.scrollTop !== before.y) return {performed: true};
    }
  }
  const before = {x: scrollX, y: scrollY};
  window.scrollBy({left: delta.x, top: delta.y, behavior: 'instant'});
  return scrollX !== before.x || scrollY !== before.y
    ? {performed: true} : {error: 'スクロールできません。操作位置や量、ページの終端を確認してください。'};
}

export async function executePageAction({tabId, documentId, action, isCurrent}) {
  if (!isCurrent()) return false;
  if (!documentId) throw new Error('対象ページを確認できません。自動読み上げを開始し直してください。');
  const frames = await chrome.scripting.executeScript({target: {tabId, allFrames: true}, func: actionFramePath});
  let frame = frames.find(item => item.frameId === 0);
  if (!isCurrent() || frame?.documentId !== documentId) return false;
  let point, delta;
  for (let depth = 0; depth < frames.length; depth++) {
    if (!isCurrent()) return false;
    const [response] = await chrome.scripting.executeScript({
      target: {tabId, documentIds: [frame.documentId]}, func: actAtPoint, args: [{action, point, delta}],
    });
    if (!isCurrent()) return false;
    const result = response?.result;
    if (result?.performed) return true;
    if (result?.error) throw new Error(result.error);
    if (!Number.isInteger(result?.childIndex)) throw new Error('操作結果を確認できませんでした。');
    const path = [...frame.result, result.childIndex];
    frame = frames.find(item => JSON.stringify(item.result) === JSON.stringify(path));
    if (!frame) throw new Error('指定した位置のフレームへアクセスできません。サイトへのアクセス許可を確認してください。');
    ({point, delta} = result);
  }
  throw new Error('操作対象のフレームを確認できませんでした。');
}
