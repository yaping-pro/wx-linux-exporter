"""Tests for wechat_image module (WeChat 4.x & 3.x image decryption)."""

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "bin"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from Crypto.Cipher import AES

from bin.wechat_image import (
    ImageKeys,
    V1_AES_KEY,
    V1_MAGIC,
    V2_MAGIC,
    clean_wxid,
    convert_wxgf,
    decrypt_dat,
    derive_keys_from_kvcomm,
    detect_format,
    extract_hevc_from_wxgf,
    image_size,
    parse_dat_header,
    quality_of,
    rank_variants,
    resolve_keys,
)


def _pad_pkcs7(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def make_synthetic_v2(
    plaintext: bytes,
    aes_key: bytes,
    xor_key: int,
    aes_size: int = 1024,
    version: str = "v2",
) -> bytes:
    """Create a synthetic V2 or V1 .dat container wrapping plaintext."""
    magic = V2_MAGIC if version == "v2" else V1_MAGIC
    eff_aes_key = V1_AES_KEY if version == "v1" else aes_key
    eff_xor_key = xor_key

    eff_aes_size = min(len(plaintext), aes_size)
    aligned = eff_aes_size - (~(~eff_aes_size % 16))
    aes_plain = plaintext[:eff_aes_size]
    padded = _pad_pkcs7(aes_plain, 16)
    if len(padded) < aligned:
        padded = padded + bytes(
            [aligned - len(padded)] * (aligned - len(padded))
        )
    cipher = AES.new(eff_aes_key, AES.MODE_ECB)
    enc_aes = cipher.encrypt(padded[:aligned])

    remaining = plaintext[len(aes_plain):]
    if remaining:
        # xor_size min(1MB, len(remaining))
        xor_size = min(1024 * 1024, len(remaining))
        raw_size = len(remaining) - xor_size
        raw_part = remaining[:raw_size]
        tail = remaining[raw_size:]
        trans = bytes.maketrans(
            bytes(range(256)),
            bytes(b ^ eff_xor_key for b in range(256)),
        )
        enc_tail = tail.translate(trans)
    else:
        xor_size = 0
        raw_part = b""
        enc_tail = b""

    header = (
        magic
        + struct.pack("<I", eff_aes_size)
        + struct.pack("<I", xor_size)
        + b"\x01"
    )
    return header + enc_aes + raw_part + enc_tail


class TestCleanWxid(unittest.TestCase):
    def test_clean_wxid_cases(self):
        # Two underscores: keep up to second underscore
        self.assertEqual(
            clean_wxid("wxid_abcdef123456_1a2b"), "wxid_abcdef123456"
        )
        self.assertEqual(clean_wxid("user_abc_1234"), "user_abc")
        self.assertEqual(clean_wxid("user_abc_1234_extra"), "user_abc")
        # One underscore
        self.assertEqual(
            clean_wxid("wxid_abcdef123456"), "wxid_abcdef123456"
        )
        # No underscore
        self.assertEqual(clean_wxid("plainuser"), "plainuser")
        self.assertEqual(clean_wxid(""), "")


class TestQualityAndRanking(unittest.TestCase):
    def test_quality_of(self):
        self.assertEqual(quality_of(Path("a_h.dat")), "hd")
        self.assertEqual(quality_of(Path("a_t.dat")), "thumb")
        self.assertEqual(quality_of(Path("a_b.dat")), "bubble")
        self.assertEqual(quality_of(Path("a.dat")), "full")
        self.assertEqual(quality_of(Path("foo/bar/xyz_h.jpg")), "hd")

    def test_rank_variants(self):
        paths = [
            Path("img_b.dat"),
            Path("img_t.dat"),
            Path("img_h.dat"),
            Path("img.dat"),
        ]
        ranked = rank_variants(paths)
        expected = [
            Path("img_h.dat"),
            Path("img.dat"),
            Path("img_t.dat"),
            Path("img_b.dat"),
        ]
        self.assertEqual(ranked, expected)


class TestHeaderAndDetection(unittest.TestCase):
    def test_parse_v2_header(self):
        # 6 magic + 4 aes_size + 4 xor_size + 1 flag
        head = V2_MAGIC + struct.pack("<II", 1024, 2048) + b"\x01"
        meta = parse_dat_header(head)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.version, "v2")
        self.assertEqual(meta.aes_size, 1024)
        self.assertEqual(meta.xor_size, 2048)
        self.assertEqual(meta.aligned, 1040)

    def test_parse_v1_header(self):
        head = V1_MAGIC + struct.pack("<II", 512, 100) + b"\x01"
        meta = parse_dat_header(head)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.version, "v1")
        self.assertEqual(meta.aes_size, 512)
        self.assertEqual(meta.aligned, 528)

    def test_corrupt_headers(self):
        # Truncated
        self.assertIsNone(parse_dat_header(b"\x00" * 10))
        # Wrong magic & not xor3
        self.assertIsNone(parse_dat_header(b"\x00" * 20))
        # Invalid flag
        bad_flag = V2_MAGIC + struct.pack("<II", 1024, 2048) + b"\x02"
        self.assertIsNone(parse_dat_header(bad_flag))

    def test_detect_format(self):
        self.assertEqual(detect_format(b"\xff\xd8\xff\xe0"), "jpg")
        self.assertEqual(detect_format(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(detect_format(b"GIF87a"), "gif")
        self.assertEqual(detect_format(b"GIF89a"), "gif")
        self.assertEqual(detect_format(b"BM\x00\x00"), "bmp")
        self.assertEqual(
            detect_format(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp"
        )
        self.assertEqual(detect_format(b"wxgf\x12\x00"), "wxgf")
        self.assertEqual(detect_format(b"random bytes"), "bin")


class TestImageSize(unittest.TestCase):
    def test_png_size(self):
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + struct.pack(">II", 1920, 1080)
            + b"\x08\x06\x00\x00\x00"
        )
        self.assertEqual(image_size(png, "png"), (1920, 1080))

    def test_gif_size(self):
        gif = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00"
        self.assertEqual(image_size(gif, "gif"), (320, 240))

    def test_bmp_size(self):
        # DIB BITMAPINFOHEADER (header_sz = 40)
        bmp = (
            b"BM\x00\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00"
            + struct.pack("<Iii", 40, 640, -480)
        )
        self.assertEqual(image_size(bmp, "bmp"), (640, 480))

    def test_webp_size_vp8(self):
        # VP8 key frame
        webp = (
            b"RIFF\x00\x00\x00\x00WEBPVP8 \x00\x00\x00\x00\x00\x00\x00"
            + b"\x9d\x01\x2a"
            + struct.pack("<HH", 800, 600)
        )
        self.assertEqual(image_size(webp, "webp"), (800, 600))

    def test_webp_size_vp8x(self):
        # VP8X canvas
        w_b = (800 - 1).to_bytes(3, "little")
        h_b = (600 - 1).to_bytes(3, "little")
        webp = (
            b"RIFF\x00\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00"
            + w_b
            + h_b
        )
        self.assertEqual(image_size(webp, "webp"), (800, 600))

    def test_jpg_size(self):
        # Minimal JPEG with SOF0 marker
        # Marker 0xC0 length 11 (2 bytes len + 1 prec + 2 h + 2 w + 1 comp)
        sof0 = (
            b"\xff\xc0"
            + struct.pack(">H", 11)
            + b"\x08"
            + struct.pack(">HH", 720, 1280)
            + b"\x03"
        )
        head_jfif = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF"
            b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        )
        jpg = head_jfif + sof0 + b"\xff\xd9"
        self.assertEqual(image_size(jpg, "jpg"), (1280, 720))

    def test_invalid_or_unknown_size(self):
        self.assertEqual(image_size(b"", "jpg"), (None, None))
        self.assertEqual(image_size(b"garbage", "png"), (None, None))
        self.assertEqual(image_size(b"garbage", "bin"), (None, None))


class TestSyntheticDecryption(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.aes_key = b"1234567890abcdef"
        self.xor_key = 0x5A
        self.keys = ImageKeys(
            aes_key=self.aes_key,
            xor_key=self.xor_key,
            code=999999,
            source="kvcomm",
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_v2_small_file_no_raw(self):
        # Plaintext smaller than aes_size
        head_jpg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF"
            b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07"
        )
        plain = head_jpg + b"payload_bytes_for_small_image"
        dat = make_synthetic_v2(
            plain, self.aes_key, self.xor_key, aes_size=1024
        )
        p = self.root / "small_t.dat"
        p.write_bytes(dat)

        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(res.data, plain)
        self.assertEqual(res.fmt, "jpg")
        self.assertEqual(res.quality, "thumb")
        self.assertEqual(res.meta.version, "v2")

    def test_v2_large_file_with_raw_and_xor_suffix(self):
        # 1.5 MB plaintext to test AES + raw plaintext + 1MB XOR suffix
        header_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + struct.pack(">II", 100, 100)
            + b"\x08\x06\x00\x00\x00"
        )
        plain = (
            header_png
            + b"A" * 2000
            + b"B" * (1024 * 1024)
            + b"C" * (200 * 1024)
        )
        dat = make_synthetic_v2(
            plain, self.aes_key, self.xor_key, aes_size=1024
        )
        p = self.root / "large_h.dat"
        p.write_bytes(dat)

        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(len(res.data), len(plain))
        self.assertEqual(res.data, plain)
        self.assertEqual(res.fmt, "png")
        self.assertEqual(res.quality, "hd")
        self.assertEqual(res.width, 100)
        self.assertEqual(res.height, 100)

    def test_v2_exact_16_byte_boundary(self):
        # aes_size is exactly 16 bytes -> aligned is 32 (PKCS7 adds full block)
        plain = b"GIF89a" + struct.pack("<HH", 50, 50) + b"\x00" * 200
        dat = make_synthetic_v2(plain, self.aes_key, self.xor_key, aes_size=16)
        p = self.root / "exact16.dat"
        p.write_bytes(dat)

        meta = parse_dat_header(dat)
        self.assertEqual(meta.aligned, 32)

        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(res.data, plain)
        self.assertEqual(res.fmt, "gif")
        self.assertEqual(res.quality, "full")

    def test_v2_micro_file_aes_size_less_than_16(self):
        # Small file with aes_size = 8
        plain = b"BM" + b"\x00" * 100
        dat = make_synthetic_v2(plain, self.aes_key, self.xor_key, aes_size=8)
        p = self.root / "micro_b.dat"
        p.write_bytes(dat)

        meta = parse_dat_header(dat)
        self.assertEqual(meta.aligned, 16)

        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(res.data, plain)
        self.assertEqual(res.fmt, "bmp")
        self.assertEqual(res.quality, "bubble")

    def test_v1_fixed_key(self):
        # Test V1 uses fixed V1_AES_KEY and callers xor_key
        plain = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF"
            b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xaa" * 2000
            + b"\xff\xd9"
        )
        # Plaintext has 1024 AES part, raw part, and 1000 byte XOR part
        dat = make_synthetic_v2(
            plain,
            aes_key=b"dummy_not_used__",
            xor_key=self.xor_key,  # Non-zero XOR key (0x5A)
            aes_size=1024,
            version="v1",
        )
        p = self.root / "v1_img.dat"
        p.write_bytes(dat)

        # Decrypt must use V1_AES_KEY and keys.xor_key
        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(res.data, plain)
        self.assertEqual(res.data[-2:], b"\xff\xd9")
        self.assertEqual(res.meta.version, "v1")

    def test_xor3_legacy_single_byte(self):
        # 3.x legacy file: XORed with 0x42
        plain = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF"
            b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"more_jpg_data" * 50
        )
        xor_key = 0x42
        enc = bytes(b ^ xor_key for b in plain)
        p = self.root / "legacy_xor.dat"
        p.write_bytes(enc)

        res = decrypt_dat(p, self.keys)
        self.assertIsNotNone(res)
        self.assertEqual(res.meta.version, "xor3")
        self.assertEqual(res.data, plain)
        self.assertEqual(res.fmt, "jpg")

    def test_corrupted_file(self):
        # Empty file
        p = self.root / "empty.dat"
        p.write_bytes(b"")
        self.assertIsNone(decrypt_dat(p, self.keys))

        # Truncated V2 container
        p2 = self.root / "trunc.dat"
        head_trunc = (
            V2_MAGIC + struct.pack("<II", 1024, 0) + b"\x01" + b"\x00" * 20
        )
        p2.write_bytes(head_trunc)
        self.assertIsNone(decrypt_dat(p2, self.keys))


class TestKeyDerivation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.account_dir = self.root / "account_wxid"
        self.kvcomm_dir = self.account_dir / "net" / "kvcomm"
        self.kvcomm_dir.mkdir(parents=True)

        self.code = 1234567890
        self.wxid = "wxid_realaccount_1234"
        self.clean_id = clean_wxid(self.wxid)

        # Expected keys
        seed = f"{self.code}{self.clean_id}".encode("utf-8")
        self.aes_key = hashlib.md5(seed).hexdigest()[:16].encode("ascii")
        self.xor_key = self.code & 0xFF

        # Create kvcomm file
        stat_file = self.kvcomm_dir / f"key_{self.code}_123456_input.statistic"
        stat_file.write_bytes(b"statistic_dummy_content")

        # Create 3 valid V2 template files
        self.templates = []
        for i in range(3):
            plain = (
                b"\xff\xd8\xff\xe0\x00\x10JFIF"
                b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                + f"template_{i}".encode() * 20
            )
            dat = make_synthetic_v2(
                plain, self.aes_key, self.xor_key, aes_size=1024
            )
            p = self.root / f"tmpl_{i}_t.dat"
            p.write_bytes(dat)
            self.templates.append(p)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_derive_keys_success(self):
        keys = derive_keys_from_kvcomm(
            self.account_dir, self.wxid, self.templates
        )
        self.assertIsNotNone(keys)
        self.assertEqual(keys.code, self.code)
        self.assertEqual(keys.aes_key, self.aes_key)
        self.assertEqual(keys.xor_key, self.xor_key)
        self.assertEqual(keys.source, "kvcomm")

    def test_derive_keys_mismatch_fails(self):
        # If templates are encrypted with wrong key, derivation returns None
        wrong_aes = b"wrong_key_123456"
        wrong_templates = []
        for i in range(3):
            plain = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100
            dat = make_synthetic_v2(plain, wrong_aes, 0x11, aes_size=1024)
            p = self.root / f"wrong_{i}_t.dat"
            p.write_bytes(dat)
            wrong_templates.append(p)

        keys = derive_keys_from_kvcomm(
            self.account_dir, self.wxid, wrong_templates
        )
        self.assertIsNone(keys)

    def test_resolve_keys_with_cache(self):
        cache_file = self.root / "cache" / "keys.json"
        keys1 = resolve_keys(
            self.account_dir,
            self.wxid,
            self.templates,
            cache_path=cache_file,
        )
        self.assertIsNotNone(keys1)
        self.assertEqual(keys1.source, "kvcomm")
        self.assertTrue(cache_file.is_file())

        # Second call should load from cache
        keys2 = resolve_keys(
            self.account_dir,
            self.wxid,
            self.templates,
            cache_path=cache_file,
        )
        self.assertIsNotNone(keys2)
        self.assertEqual(keys2.source, "cache")
        self.assertEqual(keys2.aes_key, self.aes_key)

        # Corrupt cache -> should invalidate and derive anew
        cache_data = json.loads(cache_file.read_text())
        cache_data["aes_key"] = "corrupted_key_12"
        cache_file.write_text(json.dumps(cache_data))

        keys3 = resolve_keys(
            self.account_dir,
            self.wxid,
            self.templates,
            cache_path=cache_file,
        )
        self.assertIsNotNone(keys3)
        self.assertEqual(keys3.source, "kvcomm")

    def test_resolve_keys_auto_discover_templates(self):
        # Put 3 V2 templates under account_dir/msg/attach/hash/2026-10/Img/
        img_dir = (
            self.account_dir
            / "msg"
            / "attach"
            / "testhash123"
            / "2026-10"
            / "Img"
        )
        img_dir.mkdir(parents=True)
        for i in range(3):
            plain = (
                b"\xff\xd8\xff\xe0\x00\x10JFIF"
                b"\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                + f"autodiscover_{i}".encode() * 20
            )
            dat = make_synthetic_v2(
                plain, self.aes_key, self.xor_key, aes_size=1024
            )
            (img_dir / f"auto_{i}_t.dat").write_bytes(dat)

        # Call resolve_keys without templates (templates=None)
        keys = resolve_keys(self.account_dir, self.wxid, templates=None)
        self.assertIsNotNone(keys)
        self.assertEqual(keys.source, "kvcomm")
        self.assertEqual(keys.code, self.code)
        self.assertEqual(keys.aes_key, self.aes_key)
        self.assertEqual(keys.xor_key, self.xor_key)

    def test_resolve_keys_empty_dir_safe(self):
        # An account dir with no templates should return None without error
        empty_acc = self.root / "empty_acc"
        empty_acc.mkdir(parents=True)
        # templates=None and empty directory -> must safely return None
        keys = resolve_keys(empty_acc, "wxid_dummy", templates=None)
        self.assertIsNone(keys)
        # derive_keys_from_kvcomm also safely returns None
        keys2 = derive_keys_from_kvcomm(
            empty_acc, "wxid_dummy", templates=None
        )
        self.assertIsNone(keys2)


class TestWxgfConversion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_convert_wxgf_missing_ffmpeg(self):
        out = self.root / "out.png"
        ok, animated = convert_wxgf(
            b"wxgf_data", out, ffmpeg="non_existent_ffmpeg_bin"
        )
        self.assertFalse(ok)
        self.assertFalse(animated)

    def test_convert_wxgf_animated_skip(self, monkeypatch=None):
        import subprocess

        out = self.root / "out.png"

        # Mock ffprobe reporting 5 frames
        orig_run = subprocess.run

        def mock_run(cmd, *args, **kwargs):
            if "ffprobe" in str(cmd[0]):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=b"5\n", stderr=b""
                )
            return orig_run(cmd, *args, **kwargs)

        import unittest.mock as mock
        which_mock = mock.patch(
            "bin.wechat_image.shutil.which",
            side_effect=lambda x: f"/bin/{x}",
        )
        with which_mock:
            with mock.patch("subprocess.run", side_effect=mock_run):
                ok, was_animated = convert_wxgf(
                    b"fake_wxgf_data", out, animated="skip"
                )
                self.assertFalse(ok)
                self.assertTrue(was_animated)

    def test_extract_hevc_from_wxgf(self):
        # Synthetic wxgf: header + NAL start code + payload
        fake_wxgf_4 = (
            b"wxgf\x12\x00\xaa\xbb\x00\x00\x00\x01\x40\x01nal_payload"
        )
        stream_4 = extract_hevc_from_wxgf(fake_wxgf_4)
        self.assertEqual(stream_4, b"\x00\x00\x00\x01\x40\x01nal_payload")

        fake_wxgf_3 = (
            b"wxgf\x12\x00\xaa\xbb\x00\x00\x01\x40\x01nal_payload"
        )
        stream_3 = extract_hevc_from_wxgf(fake_wxgf_3)
        self.assertEqual(stream_3, b"\x00\x00\x01\x40\x01nal_payload")

        # Non-wxgf bytes return untouched
        self.assertEqual(extract_hevc_from_wxgf(b"not_wxgf"), b"not_wxgf")


if __name__ == "__main__":
    unittest.main()
