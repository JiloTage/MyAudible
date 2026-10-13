"""Adapter tests use small in-memory images and mocked local model output."""

from io import BytesIO
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from myaudible.ocr import (
    InvalidImage, OCRService, OCRUnavailable, _decode_image, _extract_text,
    _guard_analyzer, _has_region_contrast, _is_reliable_word,
)


def image_bytes(format="PNG", size=(32, 24), color=(11, 22, 33), blank=False):
    output = BytesIO()
    image = Image.new("RGB", size, color)
    if not blank:
        ImageDraw.Draw(image).rectangle((8, 8, 12, 12), fill="white")
    image.save(output, format=format)
    return output.getvalue()


def paragraph(text, order, role=None):
    return SimpleNamespace(contents=text, order=order, role=role, box=[0, 0, 10, 20])


def word(text, box, direction="vertical"):
    x1, y1, x2, y2 = box
    return SimpleNamespace(content=text, direction=direction,
                           points=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]])


class OCRTests(unittest.TestCase):
    def test_blank_images_return_no_text_without_loading_models(self):
        with patch("myaudible.ocr.importlib.import_module") as importer:
            for color in ("white", "black", (11, 22, 33)):
                with self.subTest(color=color):
                    self.assertEqual(OCRService().recognize(image_bytes(color=color, blank=True)), "")
        importer.assert_not_called()

    def test_blank_detections_are_removed_before_recognition_with_scores_aligned(self):
        image = _decode_image(image_bytes())
        empty = [[0, 0], [4, 0], [4, 24], [0, 24]]
        printed = [[7, 7], [14, 7], [14, 14], [7, 14]]
        detector = MagicMock(return_value=(SimpleNamespace(points=[empty, printed], scores=[0.99, 0.8]), None))
        analyzer = SimpleNamespace(text_detector=detector, aggregate=lambda ocr, _layout: ocr)
        _guard_analyzer(analyzer)
        result, visualization = analyzer.text_detector(image)
        self.assertEqual(result.points, [printed])
        self.assertEqual(result.scores, [0.8])
        self.assertIsNone(visualization)
        detector.assert_called_once_with(image)

    def test_outside_image_regions_do_not_wrap_around_to_visible_text(self):
        image = _decode_image(image_bytes())
        for points in (
            [[-20, -20], [-2, -20], [-2, -2], [-20, -2]],
            [[35, 0], [50, 0], [50, 24], [35, 24]],
        ):
            with self.subTest(points=points):
                self.assertFalse(_has_region_contrast(image, points))
        self.assertTrue(_has_region_contrast(image, [[-2, -2], [14, -2], [14, 14], [-2, 14]]))

    def test_confidence_uses_per_character_average_and_preserves_genuine_repetitions(self):
        for text in ("短文", "長い文章" * 20, "2018年12月" * 4, "ことが、ことが、ことが"):
            with self.subTest(text=text):
                self.assertTrue(_is_reliable_word(SimpleNamespace(content=text, rec_score=0.9 ** (len(text) + 1))))
        self.assertFalse(_is_reliable_word(SimpleNamespace(
            content="こから、ことが、ことが、ことに", rec_score=7.161755455169228e-13)))

    def test_low_or_invalid_confidence_is_removed_before_layout_assembly(self):
        valid = SimpleNamespace(content="本文", rec_score=0.9)
        invalid = [SimpleNamespace(content="誤生成", rec_score=score)
                   for score in (0, 1e-10, float("nan"), float("inf"), -1, 2, None)]
        aggregate = MagicMock(side_effect=lambda ocr, _layout: ocr)
        analyzer = SimpleNamespace(text_detector=MagicMock(), aggregate=aggregate)
        _guard_analyzer(analyzer)
        output = analyzer.aggregate(SimpleNamespace(words=[valid, *invalid]), object())
        self.assertEqual(output.words, [valid])
        self.assertEqual(aggregate.call_args.args[0].words, [valid])

    def test_supported_image_decode_and_real_analyzer_contract(self):
        for format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format):
                analyzer = MagicMock()
                analyzer.return_value = (SimpleNamespace(
                    paragraphs=[paragraph("二番目", 2), paragraph("一番目", 1)],
                    figures=[], tables=[], words=[]), None, None)
                analyzer.img = object()
                module = SimpleNamespace(DocumentAnalyzer=MagicMock(return_value=analyzer))
                service = OCRService()
                with patch("myaudible.ocr.importlib.import_module", return_value=module):
                    self.assertEqual(service.recognize(image_bytes(format), "vertical"),
                                     "一番目\n\n二番目")
                self.assertEqual(analyzer.reading_order, "right2left")
                pixels = analyzer.call_args.args[0]
                self.assertEqual(pixels.shape, (24, 32, 3))
                if format == "PNG":
                    self.assertEqual(pixels[0, 0].tolist(), [33, 22, 11])
                self.assertIsNone(analyzer.img)
                module.DocumentAnalyzer.assert_called_once()
                self.assertTrue(module.DocumentAnalyzer.call_args.kwargs["ignore_ruby"])
                self.assertFalse(module.DocumentAnalyzer.call_args.kwargs["visualize"])

    def test_reuses_model_and_switches_reading_order_without_reload(self):
        analyzer = MagicMock(return_value=(SimpleNamespace(paragraphs=[], words=[]), None, None))
        module = SimpleNamespace(DocumentAnalyzer=MagicMock(return_value=analyzer))
        service = OCRService(device="cpu", lightweight=False)
        with patch("myaudible.ocr.importlib.import_module", return_value=module):
            service.recognize(image_bytes(), "horizontal")
            self.assertEqual(analyzer.reading_order, "top2bottom")
            service.recognize(image_bytes(), "auto")
            self.assertEqual(analyzer.reading_order, "auto")
        module.DocumentAnalyzer.assert_called_once()
        self.assertEqual(module.DocumentAnalyzer.call_args.kwargs["configs"], {})

    def test_rejects_invalid_images_before_loading_models(self):
        invalid = [b"", b"not an image", image_bytes("GIF"), image_bytes(size=(15, 40))]
        with patch("myaudible.ocr.importlib.import_module") as importer:
            for payload in invalid:
                with self.subTest(payload_length=len(payload)), self.assertRaises(InvalidImage):
                    OCRService().recognize(payload)
        importer.assert_not_called()

    def test_byte_and_pixel_limits_checked_before_inference(self):
        with patch("myaudible.ocr.MAX_IMAGE_BYTES", 10):
            with self.assertRaises(InvalidImage):
                OCRService().recognize(image_bytes())
        with patch("myaudible.ocr.MAX_IMAGE_PIXELS", 700):
            with self.assertRaises(InvalidImage):
                OCRService().recognize(image_bytes())

    def test_rejects_animated_images(self):
        output = BytesIO()
        Image.new("RGB", (32, 32), "red").save(
            output, "PNG", save_all=True,
            append_images=[Image.new("RGB", (32, 32), "blue")], duration=100, loop=0)
        with self.assertRaises(InvalidImage):
            OCRService().recognize(output.getvalue())

    def test_readiness_does_not_import_or_initialize_models(self):
        with patch("myaudible.ocr.importlib.util.find_spec", return_value=object()), \
             patch("myaudible.ocr.importlib.import_module") as importer:
            service = OCRService("cpu")
            self.assertEqual(service.readiness(), {
                "available": True, "models_loaded": False, "device": "cpu"})
        importer.assert_not_called()

    def test_readiness_cuda_reports_missing_cuda_without_loading_ocr(self):
        with patch("myaudible.ocr.importlib.util.find_spec", return_value=object()), \
             patch("myaudible.ocr.importlib.import_module") as importer:
            importer.return_value.cuda.is_available.return_value = False
            status = OCRService("cuda").readiness()
            self.assertFalse(status["available"])
            self.assertIn("CUDA is unavailable", status["error"])
            self.assertFalse(status["models_loaded"])
            importer.assert_called_once_with("torch")

    def test_missing_dependencies_produce_actionable_error(self):
        with patch("myaudible.ocr.importlib.import_module", side_effect=ImportError("missing")):
            with self.assertRaises(OCRUnavailable):
                OCRService().recognize(image_bytes())

    def test_guard_install_failure_does_not_cache_an_unguarded_model(self):
        module = SimpleNamespace(DocumentAnalyzer=MagicMock())
        service = OCRService()
        with patch("myaudible.ocr._guard_analyzer", side_effect=RuntimeError("unsupported analyzer")), \
             patch("myaudible.ocr.importlib.import_module", return_value=module):
            for _ in range(2):
                with self.assertRaises(OCRUnavailable):
                    service.recognize(image_bytes())
                self.assertIsNone(service._analyzer)
        self.assertEqual(module.DocumentAnalyzer.call_count, 2)

    def test_releases_last_image_even_if_inference_fails(self):
        analyzer = MagicMock(side_effect=RuntimeError("inference failed"))
        analyzer.img = object()
        service = OCRService()
        service._analyzer = analyzer
        with self.assertRaisesRegex(RuntimeError, "inference failed"):
            service.recognize(image_bytes())
        self.assertIsNone(analyzer.img)

    def test_inference_lock_preserves_reading_order_between_workers(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        class Analyzer:
            img = None
            reading_order = "auto"

            def __call__(self, image):
                observed = self.reading_order
                if not calls:
                    calls.append(observed)
                    first_started.set()
                    self.assert_released = release_first.wait(timeout=5)
                    calls.append(self.reading_order)
                else:
                    calls.append(observed)
                return SimpleNamespace(paragraphs=[], words=[]), None, None

        analyzer = Analyzer()
        service = OCRService()
        service._analyzer = analyzer
        workers = [threading.Thread(target=service.recognize, args=(image_bytes(), direction))
                   for direction in ("vertical", "horizontal")]
        workers[0].start()
        self.assertTrue(first_started.wait(timeout=5))
        workers[1].start()
        release_first.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(analyzer.assert_released)
        self.assertEqual(calls, ["right2left", "right2left", "top2bottom"])

    def test_figure_text_and_table_cells_follow_document_order(self):
        results = SimpleNamespace(
            paragraphs=[paragraph("本文", 3), paragraph("ヘッダー", 0, "page_header")],
            figures=[SimpleNamespace(order=1, paragraphs=[paragraph("台詞二", 2), paragraph("台詞一", 1)])],
            tables=[SimpleNamespace(order=2, cells=[
                SimpleNamespace(contents="右", row=1, col=2),
                SimpleNamespace(contents="左", row=1, col=1)])], words=[])
        self.assertEqual(_extract_text(results, "auto"), "台詞一\n台詞二\n\n左\n右\n\n本文")

    def test_vertical_word_only_output_joins_column_fragments(self):
        results = SimpleNamespace(words=[
            word("左上", [10, 0, 20, 30]), word("右下", [100, 45, 111, 75]),
            word("右上", [101, 0, 111, 30]), word("左下", [11, 45, 21, 75])])
        self.assertEqual(_extract_text(results, "auto"), "右上右下\n左上左下")

    def test_horizontal_word_only_output_reads_rows_left_to_right(self):
        results = SimpleNamespace(words=[
            word("下", [0, 40, 30, 50], "horizontal"),
            word("右", [40, 0, 70, 10], "horizontal"),
            word("左", [0, 1, 30, 11], "horizontal")])
        self.assertEqual(_extract_text(results, "auto"), "左 右\n下")

    def test_unsupported_reading_order(self):
        with self.assertRaises(ValueError):
            OCRService().recognize(image_bytes(), "unknown")


if __name__ == "__main__":
    unittest.main()
