export function normalizeCrop(crop = {}) {
  const limit = (v, fallback, min, max) => Math.min(max, Math.max(min, Number.isFinite(Number(v)) ? Number(v) : fallback));
  const x = limit(crop.x, 0.05, 0, 0.95);
  const y = limit(crop.y, 0.08, 0, 0.95);
  return {x, y, width: limit(crop.width, 0.9, 0.05, 1 - x), height: limit(crop.height, 0.84, 0.05, 1 - y)};
}

export function signatureFromRGBA(rgba) {
  const result = new Uint8Array(rgba.length / 4);
  for (let pixel = 0; pixel < result.length; pixel++) {
    const i = pixel * 4;
    result[pixel] = Math.round(rgba[i] * 0.299 + rgba[i + 1] * 0.587 + rgba[i + 2] * 0.114);
  }
  return result;
}

// Keep individual pixels: a brightness average loses small changes in similar text pages.
export function visualDifference(a, b) {
  if (!a || !b || a.length !== b.length) return Infinity;
  let absolute = 0;
  let significant = 0;
  for (let i = 0; i < a.length; i++) {
    const delta = Math.abs(a[i] - b[i]);
    absolute += delta;
    if (delta > 16) significant++;
  }
  const global = (absolute / (a.length * 255) * 0.7 + significant / a.length * 0.3) * 100;
  // Similar reader pages may differ by one glyph. A global average dilutes it
  // beyond detection, so also compare fixed small regions of the actual image.
  const side = Math.sqrt(a.length);
  if (!Number.isInteger(side)) return global;
  const tile = Math.max(4, Math.round(side / 10));
  let peak = 0;
  for (let top = 0; top < side; top += tile) for (let left = 0; left < side; left += tile) {
    let delta = 0, changed = 0, pixels = 0;
    for (let y = top; y < Math.min(top + tile, side); y++) for (let x = left; x < Math.min(left + tile, side); x++) {
      const difference = Math.abs(a[y * side + x] - b[y * side + x]);
      delta += difference; changed += difference > 16 ? 1 : 0; pixels++;
    }
    peak = Math.max(peak, (delta / (pixels * 255) * 0.7 + changed / pixels * 0.3) * 100);
  }
  return Math.max(global, peak);
}

export class PageTurnDetector {
  constructor({threshold = 0.5, stability = 0.18, stableFrames = 2, stableMs = 600} = {}) {
    this.threshold = threshold;
    this.stability = Math.min(stability, threshold / 2);
    this.stableFrames = stableFrames;
    this.stableMs = stableMs;
    this.reset();
  }
  reset() { this.accepted = null; this.pending = null; }
  observe(signature, now) {
    if (this.accepted && visualDifference(signature, this.accepted) < this.threshold) {
      const reverted = Boolean(this.pending);
      this.pending = null;
      return {type: reverted ? 'reverted' : 'unchanged'};
    }
    const changing = !this.pending && Boolean(this.accepted);
    if (this.pending && visualDifference(signature, this.pending.signature) <= this.stability) {
      this.pending.frames++;
      this.pending.signature = signature;
    } else {
      this.pending = {signature, frames: 1, since: now};
    }
    if (this.pending.frames >= this.stableFrames && now - this.pending.since >= this.stableMs) {
      this.accepted = signature;
      this.pending = null;
      return {type: 'page'};
    }
    return {type: changing ? 'changing' : 'settling'};
  }
}
