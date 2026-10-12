"""In-memory screenshot OCR using a lazily loaded local YomiToku model."""

from __future__ import annotations

import importlib
import importlib.util
from io import BytesIO
import threading
from typing import Any
import warnings

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MIN_IMAGE_DIMENSION = 16
ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
READING_ORDERS = {"auto": "auto", "vertical": "right2left", "horizontal": "top2bottom"}


class InvalidImage(ValueError):
    """The screenshot cannot safely be decoded as a supported still image."""


class OCRUnavailable(RuntimeError):
    """YomiToku or its local model dependencies are unavailable."""


def _decode_image(image_bytes: bytes) -> Any:
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise InvalidImage("画像データが空です。")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise InvalidImage("画像は 12 MiB 以下にしてください。")

    try:
        # Keep dimensions bounded before allocating a decoded pixel buffer.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format not in ALLOWED_IMAGE_FORMATS:
                    raise InvalidImage("PNG、JPEG、WebP の画像を指定してください。")
                width, height = image.size
                if min(width, height) < MIN_IMAGE_DIMENSION:
                    raise InvalidImage("画像の幅と高さは 16 ピクセル以上必要です。")
                if width * height > MAX_IMAGE_PIXELS:
                    raise InvalidImage("画像は 2,000 万ピクセル以下にしてください。")
                if getattr(image, "is_animated", False):
                    raise InvalidImage("静止画像を指定してください。")
                image.verify()
            # verify() invalidates the decoder; reopen the same memory buffer.
            with Image.open(BytesIO(image_bytes)) as image:
                import numpy as np

                rgb = image.convert("RGB")
                # DocumentAnalyzer consumes an OpenCV-compatible BGR ndarray.
                return np.asarray(rgb)[:, :, ::-1].copy()
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage("画像データが破損しているか、対応していない形式です。") from exc


def _get(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _contents(item: Any) -> str:
    value = _get(item, "contents", _get(item, "content", ""))
    if not isinstance(value, str):
        return ""
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _box(item: Any) -> tuple[float, float, float, float]:
    box = _get(item, "box")
    if box is not None and len(box) == 4:
        return tuple(float(n) for n in box)
    points = _get(item, "points", [])
    if points:
        xs, ys = zip(*points)
        return float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))
    return 0.0, 0.0, 0.0, 0.0


def _word_fallback(words: list[Any], reading_order: str) -> str:
    """Handle word-only output, preserving Japanese vertical column order."""
    words = [word for word in words if _contents(word)]
    if not words:
        return ""
    vertical = reading_order == "vertical"
    if reading_order == "auto":
        vertical_area = horizontal_area = 0.0
        for word in words:
            x1, y1, x2, y2 = _box(word)
            area = max(1.0, (x2 - x1) * (y2 - y1))
            if _get(word, "direction") == "vertical":
                vertical_area += area
            else:
                horizontal_area += area
        vertical = vertical_area > horizontal_area

    # Group words whose intervals overlap on the column (or line) axis.
    # Sorting by the axis center first makes grouping deterministic.
    axis = 0 if vertical else 1
    words.sort(key=lambda word: (_box(word)[axis] + _box(word)[axis + 2]) / 2,
               reverse=vertical)
    groups: list[list[Any]] = []
    for word in words:
        box = _box(word)
        low, high = box[axis], box[axis + 2]
        matching = None
        for group in groups:
            first = _box(group[0])
            group_low, group_high = first[axis], first[axis + 2]
            overlap = min(high, group_high) - max(low, group_low)
            if overlap > 0 and overlap >= min(high - low, group_high - group_low) / 2:
                matching = group
                break
        if matching is None:
            groups.append([word])
        else:
            matching.append(word)

    lines = []
    for group in groups:
        group.sort(key=lambda word: (_box(word)[1], _box(word)[0]) if vertical
                   else (_box(word)[0], _box(word)[1]))
        separator = "" if vertical else " "
        lines.append(separator.join(_contents(word) for word in group))
    return "\n".join(lines)


def _extract_text(results: Any, reading_order: str) -> str:
    # DocumentAnalyzer computes layout-aware orders across paragraphs, figures
    # and tables. Figure paragraphs matter for text inside manga images.
    paragraphs = list(_get(results, "paragraphs", []) or [])
    figures = list(_get(results, "figures", []) or [])
    tables = list(_get(results, "tables", []) or [])
    elements = paragraphs + figures + tables

    def order(item: Any) -> tuple[float, float, float]:
        value = _get(item, "order")
        x1, y1, _, _ = _box(item)
        return float(value) if value is not None else float("inf"), y1, x1

    blocks = []
    for element in sorted(elements, key=order):
        if _get(element, "role") in {"page_header", "page_footer"}:
            continue
        if _get(element, "paragraphs") is not None:
            text = "\n".join(_contents(p) for p in sorted(_get(element, "paragraphs"), key=order)
                             if _contents(p))
        elif _get(element, "cells") is not None:
            cells = sorted(_get(element, "cells"),
                           key=lambda cell: (_get(cell, "row", 0), _get(cell, "col", 0)))
            text = "\n".join(_contents(cell) for cell in cells if _contents(cell))
        else:
            text = _contents(element)
        if text:
            blocks.append(text)
    if elements:
        return "\n\n".join(blocks)
    return _word_fallback(list(_get(results, "words", []) or []), reading_order)


class OCRService:
    """Blocking worker service. Call recognize() outside the asyncio thread.

    The lock covers loading and inference because DocumentAnalyzer mutates its
    input image and reading order. Cancelling an awaiting coroutine does not
    cancel this worker: the caller must retain its semaphore until it exits.
    """

    def __init__(self, device: str = "cuda", lightweight: bool = True):
        if device not in {"cuda", "cpu", "mps"}:
            raise ValueError("OCR device must be cuda, cpu, or mps")
        self.device = device
        self.lightweight = lightweight
        self._analyzer: Any = None
        self._lock = threading.Lock()

    def readiness(self) -> dict[str, Any]:
        missing = []
        try:
            missing = [name for name in ("yomitoku", "torch", "cv2")
                       if importlib.util.find_spec(name) is None]
            available = not missing
        except (ImportError, ValueError):
            available = False
        status = {"available": available, "models_loaded": self._analyzer is not None,
                  "device": self.device}
        if missing:
            status["missing_dependencies"] = missing
        if self.device == "cuda" and available:
            try:
                torch = importlib.import_module("torch")
                if not torch.cuda.is_available():
                    status["available"] = False
                    status["error"] = "CUDA is unavailable; install CUDA PyTorch or choose MYAUDIBLE_OCR_DEVICE=cpu"
            except Exception:
                status["available"] = False
                status["error"] = "PyTorch cannot initialize CUDA"
        return status

    def _load_analyzer(self) -> Any:
        if self._analyzer is None:
            try:
                yomitoku = importlib.import_module("yomitoku")
                configs = {}
                if self.lightweight:
                    configs = {"ocr": {"text_recognizer": {
                        "model_name": "parseq-tiny-dynw-v4",
                        "dynamic_width": True,
                        "batch_bucketing": True,
                    }}}
                self._analyzer = yomitoku.DocumentAnalyzer(
                    configs=configs, device=self.device, visualize=False,
                    ignore_meta=True, ignore_ruby=True, reading_order="auto",
                )
            except Exception as exc:
                raise OCRUnavailable(
                    "YomiToku を初期化できません。OCR 依存パッケージ、モデル、GPU 設定を確認してください。"
                ) from exc
        return self._analyzer

    def recognize(self, image_bytes: bytes, reading_order: str = "auto") -> str:
        if reading_order not in READING_ORDERS:
            raise ValueError("reading_order must be auto, vertical, or horizontal")
        image = _decode_image(image_bytes)
        with self._lock:
            analyzer = self._load_analyzer()
            analyzer.reading_order = READING_ORDERS[reading_order]
            try:
                results, _ocr_visualization, _layout_visualization = analyzer(image)
                return _extract_text(results, reading_order)
            finally:
                # Upstream keeps the last screenshot on the analyzer instance.
                # Release that reference after every request, including errors.
                if hasattr(analyzer, "img"):
                    analyzer.img = None
