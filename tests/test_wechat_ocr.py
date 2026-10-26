from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_ocr  # noqa: E402
from wechat_ocr import (  # noqa: E402
    OcrResult,
    available_providers,
    is_likely_text,
    normalize_text,
    ocr_image,
)

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "ocr_sample.png"


class TestNormalizeText(unittest.TestCase):
    """Tests for OCR text cleaning and normalization rules."""

    def test_normalize_fullwidth_punctuation_and_digits(self):
        # Fullwidth digits & characters normalized to standard ASCII
        raw = "微信导出 ２０２６：包含全角字符！还有英文ＡＢＣ。"
        norm = normalize_text(raw)
        self.assertIn("2026:", norm)
        self.assertIn("ABC", norm)
        self.assertIn("包含全角字符!", norm)

    def test_normalize_consecutive_blank_lines(self):
        # Consecutive blank lines collapsed to single \n between paragraphs
        raw = "第一段文字\n\n\n\n第二段文字\n\n\n第三段文字"
        norm = normalize_text(raw)
        expected = "第一段文字\n第二段文字\n第三段文字"
        self.assertEqual(norm, expected)

    def test_normalize_broken_chinese_lines(self):
        # Broken lines within paragraph ending and starting with CJK merge
        raw = "这是微信导出的一段测试\n文本，在图片中被自动折断\n成了多行显示。"
        norm = normalize_text(raw)
        expected = "这是微信导出的一段测试文本,在图片中被自动折断成了多行显示。"
        self.assertEqual(norm, expected)

    def test_normalize_broken_english_lines_and_hyphenation(self):
        # English hyphenation across lines should merge; words get space
        raw = "This is a demon-\nstration of WeChat\nOCR Test."
        norm = normalize_text(raw)
        self.assertIn("demonstration", norm)
        self.assertIn("WeChat OCR Test.", norm)

    def test_normalize_preserve_urls_and_numbers(self):
        # URLs and query params must not be broken or stripped
        url = "https://github.com/steven/wx-linux-exporter?branch=main&v=2026"
        raw = f"请访问项目地址：\n{url}\n欢迎提交 PR！"
        norm = normalize_text(raw)
        self.assertIn(url, norm)

    def test_normalize_remove_symbol_noise_lines(self):
        # Isolated symbol/divider lines must be filtered out
        raw = "---\n正文标题\n=======\n这是正文内容。\n......\n- - -\n"
        norm = normalize_text(raw)
        self.assertNotIn("---", norm)
        self.assertNotIn("=======", norm)
        self.assertNotIn("......", norm)
        self.assertEqual(norm, "正文标题\n这是正文内容。")


class TestAvailableProviders(unittest.TestCase):
    """Tests for available_providers safe detection without exceptions."""

    def test_available_providers_no_exception(self):
        providers = available_providers()
        self.assertIsInstance(providers, list)
        for p in providers:
            self.assertIn(p, ("rapidocr", "tesseract"))

    @patch("wechat_ocr._is_rapidocr_available", return_value=False)
    @patch("wechat_ocr._is_tesseract_available", return_value=False)
    def test_available_providers_none(self, _m_tess, _m_rapid):
        self.assertEqual(available_providers(), [])

    @patch("wechat_ocr._is_rapidocr_available", return_value=True)
    @patch("wechat_ocr._is_tesseract_available", return_value=True)
    def test_available_providers_both(self, _m_tess, _m_rapid):
        self.assertEqual(available_providers(), ["rapidocr", "tesseract"])


class TestIsLikelyText(unittest.TestCase):
    """Tests for is_likely_text semantic classification."""

    def test_empty_or_failed_results(self):
        r1 = OcrResult("", "rapidocr", "chi_sim+eng", None, "empty")
        r2 = OcrResult("", "none", "chi_sim+eng", None, "failed")
        r3 = OcrResult("", "none", "chi_sim+eng", None, "unavailable")
        r4 = OcrResult("", "none", "chi_sim+eng", None, "timeout")
        self.assertFalse(is_likely_text(r1))
        self.assertFalse(is_likely_text(r2))
        self.assertFalse(is_likely_text(r3))
        self.assertFalse(is_likely_text(r4))

    def test_noise_or_symbol_only(self):
        r = OcrResult("--- ...", "rapidocr", "chi_sim+eng", 0.9, "ready")
        self.assertFalse(is_likely_text(r))

    def test_low_confidence(self):
        r = OcrResult("微信测试", "rapidocr", "chi_sim+eng", 0.05, "ready")
        self.assertFalse(is_likely_text(r))

    def test_valid_text(self):
        r1 = OcrResult(
            "微信导出 测试", "rapidocr", "chi_sim+eng", 0.85, "ready"
        )
        r2 = OcrResult(
            "WeChat OCR 2026", "tesseract", "chi_sim+eng", None, "ready"
        )
        self.assertTrue(is_likely_text(r1))
        self.assertTrue(is_likely_text(r2))


class TestOcrSafetyGuards(unittest.TestCase):
    """Tests for memory and decompression bomb protection."""

    def test_empty_data(self):
        res = ocr_image(b"")
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.provider, "none")

    def test_oversize_bytes(self):
        huge_data = b"X" * (31 * 1024 * 1024)
        res = ocr_image(huge_data)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.provider, "none")

    def test_oversize_dimensions_png(self):
        # PNG claiming 10000 x 6000 pixels (60 MP > 50 MP)
        import struct
        header = b"\x89PNG\r\n\x1a\n"
        ihdr_data = struct.pack(">IIBBBBB", 10000, 6000, 8, 2, 0, 0, 0)
        chunk = (
            struct.pack(">I", len(ihdr_data)) +
            b"IHDR" +
            ihdr_data +
            b"\x00\x00\x00\x00"
        )
        fake_png = header + chunk
        res = ocr_image(fake_png)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.provider, "none")


class TestOcrProviderFallbackAndStatus(unittest.TestCase):
    """Mock-based tests covering fallback order, errors, and timeouts."""

    fake_img = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    @patch("wechat_ocr.available_providers", return_value=[])
    def test_status_unavailable_when_no_engine(self, _m_avail):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "unavailable")
        self.assertEqual(res.provider, "none")
        self.assertEqual(res.text, "")

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           return_value=("微信导出 测试", 0.92))
    def test_rapidocr_success(self, _m_rapid, _m_avail):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.provider, "rapidocr")
        self.assertEqual(res.confidence, 0.92)
        self.assertIn("微信导出 测试", res.text)

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           return_value=("", None))
    def test_rapidocr_empty(self, _m_rapid, _m_avail):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.provider, "rapidocr")
        self.assertEqual(res.text, "")

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           side_effect=RuntimeError("RapidOCR crash"))
    @patch("wechat_ocr._run_tesseract_with_timeout",
           return_value=("Tesseract Fallback Text", 0.88))
    def test_rapidocr_fails_falls_back_to_tesseract(
        self, _m_tess, _m_rapid, _m_avail
    ):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.provider, "tesseract")
        self.assertIn("Tesseract Fallback Text", res.text)

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           side_effect=TimeoutError("Timeout"))
    @patch("wechat_ocr._run_tesseract_with_timeout",
           return_value=("Tesseract Text", 0.85))
    def test_rapidocr_times_out_falls_back_to_tesseract(
        self, _m_tess, _m_rapid, _m_avail
    ):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.provider, "tesseract")
        self.assertEqual(res.text, "Tesseract Text")

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           side_effect=RuntimeError("Err 1"))
    @patch("wechat_ocr._run_tesseract_with_timeout",
           side_effect=RuntimeError("Err 2"))
    def test_all_providers_error_returns_failed(
        self, _m_tess, _m_rapid, _m_avail
    ):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.provider, "none")

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_rapidocr_with_timeout",
           side_effect=TimeoutError("Timeout 1"))
    @patch("wechat_ocr._run_tesseract_with_timeout",
           side_effect=TimeoutError("Timeout 2"))
    def test_all_providers_timeout_returns_timeout(
        self, _m_tess, _m_rapid, _m_avail
    ):
        res = ocr_image(self.fake_img)
        self.assertEqual(res.status, "timeout")
        self.assertEqual(res.provider, "none")

    @patch("wechat_ocr.available_providers",
           return_value=["rapidocr", "tesseract"])
    @patch("wechat_ocr._run_tesseract_with_timeout",
           return_value=("Only Tesseract", 0.77))
    def test_custom_provider_sequence(self, _m_tess, _m_avail):
        res = ocr_image(self.fake_img, providers=["tesseract"])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.provider, "tesseract")


class TestOcrCache(unittest.TestCase):
    """Tests for atomic caching in state/ocr_cache/."""

    fake_img = b"\x89PNG\r\n\x1a\n" + b"\x11" * 32

    def test_cache_hit_avoids_re_ocr(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "ocr_cache"

            with patch(
                "wechat_ocr.available_providers", return_value=["rapidocr"]
            ), patch(
                "wechat_ocr._run_rapidocr_with_timeout",
                return_value=("缓存测试文本", 0.95),
            ) as m_run:
                # First run: writes to cache
                r1 = ocr_image(self.fake_img, cache_dir=cache_dir)
                self.assertEqual(r1.status, "ready")
                self.assertEqual(m_run.call_count, 1)

                # Second run: should hit cache directly
                r2 = ocr_image(self.fake_img, cache_dir=cache_dir)
                self.assertEqual(r2.status, "ready")
                self.assertEqual(r2.text, "缓存测试文本")
                self.assertEqual(m_run.call_count, 1)


class TestRealFixture(unittest.TestCase):
    """End-to-end integration test against generated fixture."""

    @unittest.skipIf(
        not (shutil.which("tesseract") or wechat_ocr._is_rapidocr_available()),
        "Neither tesseract nor rapidocr is installed on this system",
    )
    def test_real_fixture_ocr(self):
        self.assertTrue(FIXTURE_PATH.is_file(), f"Missing at {FIXTURE_PATH}")
        data = FIXTURE_PATH.read_bytes()
        result = ocr_image(data)
        self.assertEqual(result.status, "ready")
        self.assertIn(result.provider, ("rapidocr", "tesseract"))
        has_expected = "测试" in result.text or "WeChat" in result.text
        self.assertTrue(
            has_expected,
            f"Expected text missing in: {result.text}"
        )


if __name__ == "__main__":
    unittest.main()
