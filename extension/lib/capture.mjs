import {normalizeCrop, signatureFromRGBA} from './detector.mjs';

export class CaptureSafetyError extends Error {}

export async function prepareScreenshot(dataURL, crop = {}) {
  const bitmap = await createImageBitmap(await (await fetch(dataURL)).blob());
  try {
    const region = normalizeCrop(crop);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(bitmap.width * region.width));
    canvas.height = Math.max(1, Math.round(bitmap.height * region.height));
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, bitmap.width * region.x, bitmap.height * region.y, bitmap.width * region.width, bitmap.height * region.height, 0, 0, canvas.width, canvas.height);
    const sample = document.createElement('canvas');
    sample.width = sample.height = 160;
    const sampled = sample.getContext('2d', {willReadFrequently: true});
    sampled.drawImage(canvas, 0, 0, 160, 160);
    const signature = signatureFromRGBA(sampled.getImageData(0, 0, 160, 160).data);
    sample.width = sample.height = 0;
    return {signature, canvas, release: () => { canvas.width = canvas.height = 0; }};
  } finally { bitmap.close(); }
}

export async function screenshotSignature(dataURL, crop = {}) {
  const frame = await prepareScreenshot(dataURL, crop);
  const signature = frame.signature;
  frame.release();
  return signature;
}

// Both active-tab checks and the activation revision are essential. Never return a
// screenshot captured while another tab was active, including a switch-and-return race.
export async function captureLockedTab({tabId, windowId, queryActive, capture, revision}) {
  const startRevision = revision();
  const before = await queryActive(windowId);
  if (!before || before.id !== tabId || before.windowId !== windowId) {
    throw new CaptureSafetyError('対象タブが前面ではありません。');
  }
  let dataURL;
  try { dataURL = await capture(windowId); }
  catch (error) {
    if (/activeTab|<all_urls>/.test(error.message || '')) {
      throw new Error('画面の取得が許可されていません。chrome://extensionsでMyAudibleを再読み込みし、サイトへのアクセスを許可してください。');
    }
    throw error;
  }
  const after = await queryActive(windowId);
  if (!after || after.id !== tabId || after.windowId !== windowId || revision() !== startRevision) {
    throw new CaptureSafetyError('キャプチャ中にタブが切り替わりました。');
  }
  return dataURL;
}
