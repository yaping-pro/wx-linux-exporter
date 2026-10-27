#!/usr/bin/env python3
"""WeChat image OCR module (pure local, zero network).

Provides multi-engine OCR fallback (RapidOCR ONNXRuntime CPU -> Tesseract CLI),
robust text normalization (unifying full/half-width, noise filtering, joining
broken Chinese/English lines, preserving URLs/numbers), and safety guards
against memory/decompression bombs.

Zero required external dependencies: works out-of-the-box in environments
without any OCR engine installed, returning graceful 'unavailable' status.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
from typing import Any, Sequence
import unicodedata

logger = logging.getLogger(__name__)

# Safety limits: do not attempt OCR on files or pixels exceeding bounds
MAX_IMAGE_BYTES = 30 * 1024 * 1024       # 30 MB
MAX_PIXELS = 50_000_000                  # 50 Megapixels

DEFAULT_TIMEOUT = 60
DEFAULT_MIN_CHARS = 1
DEFAULT_LANG = "chi_sim+eng"
DEFAULT_PROVIDERS: tuple[str, ...] = ("rapidocr", "tesseract")

# Global singleton lock and instance for RapidOCR (lazy-loaded)
_RAPID_OCR_LOCK = threading.Lock()
_RAPID_OCR_INSTANCE: Any = None
_RAPID_OCR_VERSION: str | None = None
_TESSERACT_VERSION: str | None = None


@dataclass(frozen=True)
class OcrResult:
    text: str                 # 规整后的文本，失败/无文字时为空串
    provider: str             # "rapidocr" | "tesseract" | "none"
    lang: str                 # 实际使用的语言串，如 "chi_sim+eng"
    confidence: float | None  # 0..1，取不到给 None
    status: str               # "ready"|"empty"|"unavailable"|"failed"|...


def _is_cjk(ch: str) -> bool:
    """Return True if character is a CJK unified ideograph or CJK symbol."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF or
        0x3400 <= cp <= 0x4DBF or
        0x20000 <= cp <= 0x2A6DF or
        0xF900 <= cp <= 0xFAFF or
        0x3000 <= cp <= 0x303F  # CJK symbols and punctuation
    )


def _is_symbol_only(line: str) -> bool:
    """Return True if line has no CJK characters and no alphanumerics."""
    return not bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", line))


def _merge_paragraph_lines(lines: list[str]) -> str:
    """Merge lines within a single paragraph broken across lines by OCR."""
    if not lines:
        return ""
    merged = lines[0]
    for nxt in lines[1:]:
        if not nxt:
            continue
        # Case 1: English hyphenation across lines (e.g. "para-" + "graph")
        if merged.endswith("-") and re.match(r"^[A-Za-z]", nxt):
            merged = merged[:-1] + nxt
        # Case 2: CJK to CJK (e.g. "中文" + "测试") -> no space
        elif _is_cjk(merged[-1]) and _is_cjk(nxt[0]):
            merged = merged + nxt
        # Case 3: CJK boundary with punctuation or ASCII
        elif _is_cjk(merged[-1]) or _is_cjk(nxt[0]):
            merged = merged + nxt
        # Case 4: Alphanumeric to alphanumeric (e.g. "WeChat" + "OCR")
        elif (
            re.search(r"[A-Za-z0-9]$", merged) and
            re.match(r"^[A-Za-z0-9]", nxt)
        ):
            merged = merged + " " + nxt
        # Case 5: Default whitespace separation
        else:
            merged = merged + " " + nxt
    return merged


def normalize_text(raw: str) -> str:
    """Normalize raw OCR text output.

    - Unifies full-width and half-width characters (NFKC).
    - Removes isolated symbol-only noise lines (e.g. '---', '......').
    - Merges broken lines within the same paragraph.
    - Preserves single newline between paragraphs.
    - Preserves numbers, English words, URLs, and punctuation.
    """
    if not raw:
        return ""

    # 1. Unicode NFKC normalization
    norm = unicodedata.normalize("NFKC", raw)

    # 2. Split lines and strip whitespace per line
    raw_lines = [line.strip() for line in norm.splitlines()]

    # 3. Group into paragraphs divided by empty lines or symbol-only lines
    paragraphs: list[list[str]] = []
    current_p: list[str] = []

    for line in raw_lines:
        if not line or _is_symbol_only(line):
            if current_p:
                paragraphs.append(current_p)
                current_p = []
        else:
            current_p.append(line)
    if current_p:
        paragraphs.append(current_p)

    # 4. Merge lines within each paragraph and separate paragraphs with \n
    merged_paragraphs = [_merge_paragraph_lines(p) for p in paragraphs if p]
    result = "\n".join(mp for mp in merged_paragraphs if mp).strip()
    return result


def is_likely_text(result: OcrResult) -> bool:
    """Determine whether OCR result contains meaningful readable text."""
    if result.status != "ready" or not result.text:
        return False
    # Must contain at least one CJK or alphanumeric character
    meaningful = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", result.text)
    if not meaningful:
        return False
    # If confidence is available and too low, discard as noise
    if result.confidence is not None and result.confidence < 0.15:
        return False
    return True


def _get_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Extract width and height from raw bytes without full pixel decode."""
    l_data = len(data)
    if l_data < 10:
        return None

    # PNG
    if (
        l_data >= 24 and
        data.startswith(b"\x89PNG\r\n\x1a\n") and
        data[12:16] == b"IHDR"
    ):
        w, h = struct.unpack(">II", data[16:24])
        return w, h

    # GIF
    if l_data >= 10 and (
        data.startswith(b"GIF87a") or data.startswith(b"GIF89a")
    ):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h

    # BMP
    if l_data >= 26 and data.startswith(b"BM"):
        try:
            header_size = struct.unpack("<I", data[14:18])[0]
            if header_size == 12:
                w, h = struct.unpack("<HH", data[18:22])
            else:
                w = abs(struct.unpack("<i", data[18:22])[0])
                h = abs(struct.unpack("<i", data[22:26])[0])
            return w, h
        except struct.error:
            pass

    # JPEG
    if l_data >= 4 and data.startswith(b"\xff\xd8"):
        idx = 2
        sof_markers = (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        )
        while idx < l_data:
            if data[idx] != 0xFF:
                idx += 1
                continue
            while idx < l_data and data[idx] == 0xFF:
                idx += 1
            if idx >= l_data:
                break
            marker = data[idx]
            idx += 1
            if marker in (0xD8, 0xD9, 0x00) or (0xD0 <= marker <= 0xD7):
                continue
            if idx + 2 > l_data:
                break
            seg_len = struct.unpack(">H", data[idx:idx + 2])[0]
            if seg_len < 2:
                break
            if marker in sof_markers:
                if idx + 7 <= l_data:
                    h, w = struct.unpack(">HH", data[idx + 3:idx + 7])
                    return w, h
            idx += seg_len

    # WEBP
    if l_data >= 30 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 ":
            if l_data >= 26 and data[20:23] == b"\x9d\x01\x2a":
                w = struct.unpack("<H", data[23:25])[0] & 0x3FFF
                h = struct.unpack("<H", data[25:27])[0] & 0x3FFF
                return w, h
        elif chunk == b"VP8L":
            if l_data >= 25 and data[20] == 0x2F:
                b0, b1, b2, b3 = data[21:25]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return w, h
        elif chunk == b"VP8X":
            if l_data >= 30:
                w = 1 + struct.unpack("<I", data[24:27] + b"\x00")[0]
                h = 1 + struct.unpack("<I", data[27:30] + b"\x00")[0]
                return w, h

    # Fallback to Pillow if installed
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        pass

    return None


def _is_rapidocr_available() -> bool:
    """Check if rapidocr_onnxruntime is importable."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _get_rapidocr():
    """Get or initialize singleton RapidOCR instance."""
    global _RAPID_OCR_INSTANCE, _RAPID_OCR_VERSION
    with _RAPID_OCR_LOCK:
        if _RAPID_OCR_INSTANCE is None:
            from rapidocr_onnxruntime import RapidOCR
            import rapidocr_onnxruntime
            _RAPID_OCR_INSTANCE = RapidOCR()
            _RAPID_OCR_VERSION = getattr(
                rapidocr_onnxruntime, "__version__", "rapidocr"
            )
        return _RAPID_OCR_INSTANCE


def _run_rapidocr_with_timeout(
    data: bytes,
    timeout: int,
) -> tuple[str, float | None]:
    """Execute RapidOCR on raw bytes with a hard thread-join timeout."""
    ocr = _get_rapidocr()
    result_box: list[tuple[str, float | None]] = []
    error_box: list[Exception] = []

    def target():
        try:
            res, _ = ocr(data)
            if not res:
                result_box.append(("", None))
                return
            texts: list[str] = []
            scores: list[float] = []
            for item in res:
                if len(item) >= 2 and item[1]:
                    t = str(item[1]).strip()
                    if t:
                        texts.append(t)
                if len(item) >= 3 and item[2] is not None:
                    try:
                        scores.append(float(item[2]))
                    except (ValueError, TypeError):
                        pass
            raw_text = "\n".join(texts)
            avg_score = (sum(scores) / len(scores)) if scores else None
            result_box.append((raw_text, avg_score))
        except Exception as exc:
            error_box.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=float(timeout))

    if thread.is_alive():
        raise TimeoutError(f"rapidocr timed out after {timeout}s")
    if error_box:
        raise error_box[0]
    if result_box:
        return result_box[0]
    return "", None


def _is_tesseract_available() -> bool:
    """Check if tesseract CLI is installed and in PATH."""
    return shutil.which("tesseract") is not None


def _get_tesseract_version() -> str:
    global _TESSERACT_VERSION
    if _TESSERACT_VERSION is None:
        tess = shutil.which("tesseract")
        if not tess:
            return "none"
        try:
            res = subprocess.run(
                [tess, "--version"], capture_output=True, text=True, timeout=5
            )
            first_line = (
                res.stdout.splitlines()[0] if res.stdout else "tesseract"
            )
            _TESSERACT_VERSION = first_line.strip()
        except Exception:
            _TESSERACT_VERSION = "tesseract"
    return _TESSERACT_VERSION


def _parse_tesseract_tsv(tsv_output: str) -> tuple[str, float | None]:
    """Parse Tesseract TSV output to extract lines and mean confidence."""
    lines_dict: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []

    for raw_line in tsv_output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        try:
            block_num = int(parts[2])
            par_num = int(parts[3])
            line_num = int(parts[4])
        except ValueError:
            block_num, par_num, line_num = 0, 0, 0

        conf_str = parts[10]
        word_text = parts[11].strip()
        if not word_text:
            continue

        try:
            c = float(conf_str)
            if c >= 0:
                confs.append(c / 100.0)
        except ValueError:
            pass

        key = (block_num, par_num, line_num)
        if key not in lines_dict:
            lines_dict[key] = []
        lines_dict[key].append(word_text)

    lines = [" ".join(words) for words in lines_dict.values()]
    full_text = "\n".join(lines)
    avg_conf = (sum(confs) / len(confs)) if confs else None
    return full_text, avg_conf


def _run_tesseract_with_timeout(
    data: bytes,
    timeout: int,
    lang: str = DEFAULT_LANG,
) -> tuple[str, float | None]:
    """Run Tesseract CLI via subprocess with timeout protection."""
    tess_bin = shutil.which("tesseract")
    if not tess_bin:
        raise RuntimeError("tesseract executable not found")

    cmd = [tess_bin, "stdin", "stdout", "-l", lang, "tsv"]
    try:
        proc = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"tesseract timed out after {timeout}s")

    if proc.returncode != 0:
        cmd_plain = [tess_bin, "stdin", "stdout", "-l", lang]
        try:
            proc = subprocess.run(
                cmd_plain,
                input=data,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"tesseract timed out after {timeout}s")

        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"tesseract exited {proc.returncode}: {err[:150]}"
            )
        raw_text = proc.stdout.decode("utf-8", errors="replace")
        return raw_text, None

    tsv_text = proc.stdout.decode("utf-8", errors="replace")
    return _parse_tesseract_tsv(tsv_text)


def _read_cache(
    cache_dir: Path,
    sha256: str,
    provider_key: str,
) -> OcrResult | None:
    """Read cached OCR result if present and provider key matches."""
    cache_file = cache_dir / f"{sha256}.json"
    if not cache_file.is_file():
        return None
    try:
        content = json.loads(cache_file.read_text(encoding="utf-8"))
        if (
            content.get("sha256") == sha256 and
            content.get("provider_key") == provider_key
        ):
            return OcrResult(
                text=content.get("text", ""),
                provider=content.get("provider", "none"),
                lang=content.get("lang", DEFAULT_LANG),
                confidence=content.get("confidence"),
                status=content.get("status", "ready"),
            )
    except Exception:
        pass
    return None


def _write_cache(
    cache_dir: Path,
    sha256: str,
    provider_key: str,
    result: OcrResult,
) -> None:
    """Write OCR result atomically to state/ocr_cache/<sha256>.json."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{sha256}.json"
        tmp_file = cache_dir / f".{sha256}.tmp.{os.getpid()}"
        payload = {
            "sha256": sha256,
            "provider_key": provider_key,
            "provider": result.provider,
            "lang": result.lang,
            "confidence": result.confidence,
            "status": result.status,
            "text": result.text,
        }
        tmp_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_file.replace(cache_file)
    except Exception:
        pass


def available_providers() -> list[str]:
    """Return ordered list of OCR providers installed on this machine."""
    providers: list[str] = []
    if _is_rapidocr_available():
        providers.append("rapidocr")
    if _is_tesseract_available():
        providers.append("tesseract")
    return providers


def ocr_image(
    data: bytes,
    *,
    providers: Sequence[str] = DEFAULT_PROVIDERS,
    timeout: int = DEFAULT_TIMEOUT,
    min_chars: int = DEFAULT_MIN_CHARS,
    cache_dir: Path | str | None = None,
) -> OcrResult:
    """Perform OCR on image bytes with sequential provider fallback.

    Args:
        data: Raw image bytes.
        providers: Sequence of providers ("rapidocr", "tesseract").
        timeout: Timeout in seconds for each provider.
        min_chars: Minimum character threshold for 'ready' status vs 'empty'.
        cache_dir: Optional cache directory path (e.g. 'state/ocr_cache').

    Returns:
        OcrResult with text, provider, lang, confidence, and status.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        return OcrResult(
            text="",
            provider="none",
            lang=DEFAULT_LANG,
            confidence=None,
            status="empty",
        )

    # Memory / CPU Protection: reject files > 30MB
    if len(data) > MAX_IMAGE_BYTES:
        logger.warning(
            "Image size %d bytes exceeds limit %d", len(data), MAX_IMAGE_BYTES
        )
        return OcrResult(
            text="",
            provider="none",
            lang=DEFAULT_LANG,
            confidence=None,
            status="failed",
        )

    # Decompression bomb protection: check image dimensions
    dims = _get_image_dimensions(data)
    if dims:
        w, h = dims
        if w * h > MAX_PIXELS:
            logger.warning(
                "Image pixel count %d (%dx%d) exceeds limit %d",
                w * h, w, h, MAX_PIXELS,
            )
            return OcrResult(
                text="",
                provider="none",
                lang=DEFAULT_LANG,
                confidence=None,
                status="failed",
            )

    # Cache handling
    target_cache_dir: Path | None = None
    if cache_dir is not None:
        target_cache_dir = Path(cache_dir)
    elif (
        os.environ.get("WECHAT_OCR_CACHE") == "1" or
        Path("state/ocr_cache").is_dir()
    ):
        target_cache_dir = Path("state/ocr_cache")

    sha256 = hashlib.sha256(data).hexdigest()
    provider_key = f"{','.join(providers)}"

    if target_cache_dir:
        cached = _read_cache(target_cache_dir, sha256, provider_key)
        if cached is not None:
            return cached

    avail = available_providers()
    valid_providers = [p for p in providers if p in ("rapidocr", "tesseract")]

    # If none of the requested providers are available on this machine
    if not any(p in avail for p in valid_providers):
        return OcrResult(
            text="",
            provider="none",
            lang=DEFAULT_LANG,
            confidence=None,
            status="unavailable",
        )

    had_timeout = False
    had_error = False

    for p in valid_providers:
        if p not in avail:
            continue
        try:
            if p == "rapidocr":
                raw_text, conf = _run_rapidocr_with_timeout(
                    data, timeout=timeout
                )
                lang = DEFAULT_LANG
            elif p == "tesseract":
                raw_text, conf = _run_tesseract_with_timeout(
                    data, timeout=timeout, lang=DEFAULT_LANG
                )
                lang = DEFAULT_LANG
            else:
                continue

            norm_text = normalize_text(raw_text)
            if not norm_text or len(norm_text) < min_chars:
                res = OcrResult(
                    text="",
                    provider=p,
                    lang=lang,
                    confidence=conf,
                    status="empty",
                )
            else:
                res = OcrResult(
                    text=norm_text,
                    provider=p,
                    lang=lang,
                    confidence=conf,
                    status="ready",
                )

            if target_cache_dir:
                _write_cache(target_cache_dir, sha256, provider_key, res)
            return res

        except TimeoutError:
            logger.warning("Provider %s timed out after %ds", p, timeout)
            had_timeout = True
            continue
        except Exception as exc:
            logger.warning("Provider %s failed: %s", p, type(exc).__name__)
            had_error = True
            continue

    # Fallback status if all available providers were attempted and failed
    final_status = "timeout" if (had_timeout and not had_error) else "failed"
    return OcrResult(
        text="",
        provider="none",
        lang=DEFAULT_LANG,
        confidence=None,
        status=final_status,
    )
