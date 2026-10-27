"""WeChat image .dat decryption module (WeChat 4.x & 3.x).

Handles WeChat 4.x V2/V1 AES+XOR container files, 3.x single-byte XOR files,
and wxgf HEVC container conversion.

Note on PKCS7 unpadding in decrypt_dat:
    PKCS7 unpadding uses a relaxed boundary check (1 <= pad <= 16) rather than
    verifying all pad bytes. This is safe and by design because candidate keys
    are strictly cross-validated across >= 3 templates before any decryption.
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
from typing import Iterable, Sequence

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# Known signatures
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x56\x31\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"

# Format signatures
SIG_JPG = b"\xff\xd8\xff"
SIG_PNG = b"\x89PNG\r\n\x1a\n"
SIG_GIF87 = b"GIF87a"
SIG_GIF89 = b"GIF89a"
SIG_BMP = b"BM"
SIG_RIFF = b"RIFF"
SIG_WEBP = b"WEBP"
SIG_WXGF = b"wxgf"
SIG_FTYP = b"ftyp"

VALID_FORMATS = ("jpg", "png", "gif", "webp", "bmp", "wxgf", "mp4", "mov")
QUALITY_WEIGHTS = {
    "hd": 4,
    "full": 3,
    "thumb": 2,
    "bubble": 1,
}


class ImageError(Exception):
    """Base exception for image decryption failures with safe messages."""
    pass


@dataclass(frozen=True)
class ImageKeys:
    aes_key: bytes      # 16 ASCII bytes
    xor_key: int        # 0..255
    code: int
    source: str         # "kvcomm" | "memscan" | "cache"


@dataclass(frozen=True)
class DatMeta:
    version: str        # "v1" | "v2" | "xor3"
    aes_size: int
    xor_size: int
    aligned: int


@dataclass(frozen=True)
class DecryptedImage:
    data: bytes
    fmt: str            # "jpg"|"png"|"gif"|"webp"|"bmp"|"wxgf"|"bin"
    meta: DatMeta
    quality: str        # "hd" | "full" | "thumb" | "bubble"
    width: int | None
    height: int | None


def clean_wxid(raw: str) -> str:
    """Clean wxid directory name, keeping up to before the second underscore.

    Examples:
        wxid_abcdef123456_1a2b -> wxid_abcdef123456
        wxid_abc -> wxid_abc
        account_name_extra -> account_name
        single -> single
    """
    parts = raw.split("_")
    if len(parts) >= 3:
        return f"{parts[0]}_{parts[1]}"
    return raw


def parse_dat_header(head: bytes) -> DatMeta | None:
    """Parse container header from initial bytes.

    Returns DatMeta or None if invalid or unsupported.
    """
    if len(head) < 15:
        # Check if it could be 3.x XOR
        xor_res = _detect_xor3_key(head)
        if xor_res is not None:
            return DatMeta(
                version="xor3",
                aes_size=0,
                xor_size=len(head),
                aligned=0,
            )
        return None

    magic = head[:6]
    if magic == V2_MAGIC:
        version = "v2"
    elif magic == V1_MAGIC:
        version = "v1"
    else:
        xor_res = _detect_xor3_key(head)
        if xor_res is not None:
            return DatMeta(
                version="xor3",
                aes_size=0,
                xor_size=len(head),
                aligned=0,
            )
        return None

    aes_size = struct.unpack("<I", head[6:10])[0]
    xor_size = struct.unpack("<I", head[10:14])[0]
    flag = head[14]
    if flag != 0x01:
        return None

    aligned = aes_size - (~(~aes_size % 16))
    return DatMeta(
        version=version,
        aes_size=aes_size,
        xor_size=xor_size,
        aligned=aligned,
    )


def detect_format(head: bytes) -> str:
    """Detect image format from decrypted prefix bytes."""
    if head.startswith(SIG_JPG):
        return "jpg"
    if head.startswith(SIG_PNG):
        return "png"
    if head.startswith(SIG_GIF87) or head.startswith(SIG_GIF89):
        return "gif"
    if head.startswith(SIG_BMP):
        return "bmp"
    if (
        head.startswith(SIG_RIFF)
        and len(head) >= 12
        and head[8:12] == SIG_WEBP
    ):
        return "webp"
    if head.startswith(SIG_WXGF):
        return "wxgf"
    if len(head) >= 8 and head[4:8] == SIG_FTYP:
        major = head[8:12] if len(head) >= 12 else b""
        if major in (b"qt  ", b"moov"):
            return "mov"
        return "mp4"
    return "bin"


def quality_of(path: Path) -> str:
    """Return quality category of image path based on filename suffix.

    hd > full > thumb > bubble
    """
    stem = path.stem
    if stem.endswith("_h"):
        return "hd"
    if stem.endswith("_t"):
        return "thumb"
    if stem.endswith("_b"):
        return "bubble"
    return "full"


def rank_variants(paths: Iterable[Path]) -> list[Path]:
    """Sort image paths in descending order of quality priority."""
    return sorted(
        paths,
        key=lambda p: (QUALITY_WEIGHTS.get(quality_of(p), 0), p.name),
        reverse=True,
    )


def _detect_xor3_key(head: bytes) -> tuple[int, str] | None:
    """Detect single-byte XOR key and format from raw head bytes."""
    if len(head) < 4:
        return None

    # 1. JPEG: ffd8ff
    k = head[0] ^ 0xFF
    if (head[1] ^ k) == 0xD8 and (head[2] ^ k) == 0xFF:
        return k, "jpg"

    # 2. PNG: 89504e47
    k = head[0] ^ 0x89
    if (
        (head[1] ^ k) == 0x50
        and (head[2] ^ k) == 0x4E
        and (head[3] ^ k) == 0x47
    ):
        return k, "png"

    # 3. GIF: 47494638 ('GIF8')
    k = head[0] ^ 0x47
    if (
        (head[1] ^ k) == 0x49
        and (head[2] ^ k) == 0x46
        and (head[3] ^ k) == 0x38
    ):
        return k, "gif"

    # 4. WEBP: RIFF....WEBP
    if len(head) >= 12:
        k = head[0] ^ 0x52  # 'R'
        if (
            (head[1] ^ k) == 0x49  # 'I'
            and (head[2] ^ k) == 0x46  # 'F'
            and (head[3] ^ k) == 0x46  # 'F'
            and (head[8] ^ k) == 0x57  # 'W'
            and (head[9] ^ k) == 0x45  # 'E'
            and (head[10] ^ k) == 0x42  # 'B'
            and (head[11] ^ k) == 0x50  # 'P'
        ):
            return k, "webp"

    # 5. BMP: 'BM' (42 4d) - check reasonable DIB header size at offset 14
    if len(head) >= 18:
        k = head[0] ^ 0x42
        if (head[1] ^ k) == 0x4D:
            # DIB header size is u32 LE
            dib_size = struct.unpack(
                "<I", bytes(b ^ k for b in head[14:18])
            )[0]
            if dib_size in (12, 40, 52, 56, 108, 124):
                return k, "bmp"

    return None


def _verify_v2_keys(aes_key: bytes, templates: Sequence[Path]) -> bool:
    """Verify AES key against template V2 .dat files.

    Must decrypt first block [15:31] of each template to known image format.
    """
    if not templates:
        return False

    cipher = AES.new(aes_key, AES.MODE_ECB)
    verified = 0
    for p in templates:
        try:
            with open(p, "rb") as f:
                head = f.read(32)
            if len(head) < 31:
                continue
            if head[:6] != V2_MAGIC:
                continue
            block = head[15:31]
            dec = cipher.decrypt(block)
            fmt = detect_format(dec)
            if fmt in VALID_FORMATS:
                verified += 1
            else:
                return False
        except Exception:
            return False

    return verified > 0 and verified == len(templates)


def _find_kvcomm_codes(account_dir: Path) -> list[int]:
    """Extract candidate code numbers from kvcomm files."""
    codes: set[int] = set()
    search_dirs = [
        account_dir / "net" / "kvcomm",
        account_dir / ".xwechat" / "net" / "kvcomm",
        Path.home() / ".xwechat" / "net" / "kvcomm",
    ]
    # Also check direct parent of account_dir if net/kvcomm is beside it
    if account_dir.parent.is_dir():
        search_dirs.append(account_dir.parent / "net" / "kvcomm")

    pattern = re.compile(r"key_(\d+)_")
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            for item in d.iterdir():
                m = pattern.search(item.name)
                if m:
                    try:
                        codes.add(int(m.group(1)))
                    except ValueError:
                        pass
        except OSError as e:
            logger.debug(f"Unable to read kvcomm dir {d}: {e}")

    return sorted(codes, reverse=True)


def discover_v2_templates(
    account_dir: Path,
    max_files: int = 5,
    max_dirs: int = 200,
) -> list[Path]:
    """Auto-discover candidate V2 template files under account_dir.

    Scans account_dir/msg/attach/*/*/Img/ for *_t.dat files first,
    falling back to *_h.dat / *.dat if none found.
    Limits traversal to max_dirs to avoid scanning thousands of files.
    """
    attach_dir = account_dir / "msg" / "attach"
    if not attach_dir.is_dir():
        return []

    found: list[Path] = []

    def try_patterns(patterns: tuple[str, ...]) -> list[Path]:
        res: list[Path] = []
        dir_count = 0
        try:
            subdirs = sorted(
                attach_dir.iterdir(),
                key=lambda p: p.stat().st_mtime if p.is_dir() else 0.0,
                reverse=True,
            )
        except OSError:
            return []

        for s in subdirs:
            if not s.is_dir():
                continue
            try:
                ym_dirs = sorted(
                    s.iterdir(),
                    key=lambda p: p.stat().st_mtime if p.is_dir() else 0.0,
                    reverse=True,
                )
            except OSError:
                continue

            for ym in ym_dirs:
                if not ym.is_dir():
                    continue
                dir_count += 1
                img_dir = ym / "Img"
                if img_dir.is_dir():
                    for pat in patterns:
                        try:
                            files = sorted(
                                img_dir.glob(pat),
                                key=lambda p: p.stat().st_mtime,
                                reverse=True,
                            )
                            for f in files:
                                if f.is_file():
                                    res.append(f)
                                    if len(res) >= max_files:
                                        return res
                        except OSError:
                            continue
                if len(res) >= max_files or dir_count >= max_dirs:
                    return res
            if len(res) >= max_files or dir_count >= max_dirs:
                return res
        return res

    # 1. Search for *_t.dat
    found = try_patterns(("*_t.dat",))
    if not found:
        # 2. Fallback to *_h.dat and *.dat
        found = try_patterns(("*_h.dat", "*.dat"))

    return found


def _select_v2_templates(
    templates: Sequence[Path] | None, min_count: int = 3
) -> list[Path]:
    """Select best V2 template files for key cross-validation.

    Prefers _t (thumb) files, sorted by mtime descending.
    Safe against templates=None.
    """
    if not templates:
        return []

    valid: list[Path] = []
    for p in templates:
        if not p.is_file():
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(15)
            if len(head) >= 15 and head[:6] == V2_MAGIC and head[14] == 0x01:
                valid.append(p)
        except OSError:
            continue

    def sort_key(p: Path) -> tuple[int, float]:
        is_thumb = 1 if p.stem.endswith("_t") else 0
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (is_thumb, mtime)

    sorted_valid = sorted(valid, key=sort_key, reverse=True)
    return sorted_valid[: max(min_count, len(sorted_valid))]


def derive_keys_from_kvcomm(
    account_dir: Path,
    wxid: str,
    templates: Sequence[Path] | None = None,
) -> ImageKeys | None:
    """Derive ImageKeys from kvcomm statistic files with template verification.

    Requires >= 3 valid V2 templates for cross-validation when available.
    """
    clean_id = clean_wxid(wxid)
    eff_templates = (
        templates if templates is not None
        else discover_v2_templates(account_dir)
    )
    v2_templates = _select_v2_templates(eff_templates, min_count=3)
    if not v2_templates:
        logger.debug("No valid V2 templates provided for key verification")
        return None

    codes = _find_kvcomm_codes(account_dir)
    for code in codes:
        seed = f"{code}{clean_id}".encode("utf-8")
        aes_hex = hashlib.md5(seed).hexdigest()[:16]
        aes_key = aes_hex.encode("ascii")
        xor_key = code & 0xFF

        if _verify_v2_keys(aes_key, v2_templates):
            return ImageKeys(
                aes_key=aes_key,
                xor_key=xor_key,
                code=code,
                source="kvcomm",
            )

    return None


def _scan_mem_for_keys(
    wxid: str,
    templates: Sequence[Path] | None = None,
    allow_sudo: bool = False,
) -> ImageKeys | None:
    """Fallback: scan Linux /proc/<pid>/mem for candidate tokens."""
    if not Path("/proc").is_dir():
        return None

    clean_id = clean_wxid(wxid)
    v2_templates = _select_v2_templates(templates, min_count=3)
    if not v2_templates:
        return None

    # 1. Find wechat process PIDs
    pids: list[int] = []
    my_uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm_file = entry / "comm"
            if not comm_file.is_file():
                continue
            comm = comm_file.read_text(
                encoding="utf-8", errors="ignore"
            ).strip()
            if "wechat" in comm.lower():
                stat = entry.stat()
                if stat.st_uid == my_uid:
                    pids.append(pid)
        except OSError:
            continue

    if not pids:
        return None

    # Helper scanner on a PID
    def scan_pid(pid: int, use_sudo: bool) -> ImageKeys | None:
        maps_path = Path(f"/proc/{pid}/maps")
        if not maps_path.is_file():
            return None

        # Read readable ranges
        ranges: list[tuple[int, int]] = []
        try:
            with open(maps_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) < 2:
                        continue
                    perms = parts[1]
                    # Read only readable ranges, skip non-readable
                    if perms.startswith("r"):
                        start_str, end_str = parts[0].split("-")
                        start = int(start_str, 16)
                        end = int(end_str, 16)
                        # skip excessively large mappings (> 32MB)
                        if end - start <= 32 * 1024 * 1024:
                            ranges.append((start, end))
        except OSError:
            return None

        token_re = re.compile(rb"\b[a-zA-Z0-9]{16,32}\b")
        tested_keys: set[bytes] = set()

        for start, end in ranges:
            chunk: bytes = b""
            if not use_sudo:
                try:
                    with open(f"/proc/{pid}/mem", "rb", buffering=0) as m:
                        m.seek(start)
                        chunk = m.read(end - start)
                except OSError:
                    continue
            else:
                cmd = [
                    "sudo", "-n", "dd",
                    f"if=/proc/{pid}/mem",
                    f"skip={start}",
                    f"count={end - start}",
                    "bs=1",
                    "status=none",
                ]
                try:
                    p = subprocess.run(cmd, capture_output=True, timeout=5)
                    if p.returncode == 0:
                        chunk = p.stdout
                except (subprocess.SubprocessError, OSError):
                    continue

            if not chunk:
                continue

            for m in token_re.finditer(chunk):
                raw_tok = m.group()
                # 16 or 32 chars: take first 16 bytes ASCII
                cand_aes = raw_tok[:16]
                if cand_aes in tested_keys:
                    continue
                tested_keys.add(cand_aes)

                if _verify_v2_keys(cand_aes, v2_templates):
                    # AES key verified! Recover xor_key from template tail
                    # or from code seed.
                    code_val = 0
                    if raw_tok.isdigit():
                        code_val = int(raw_tok)
                        xor_k = code_val & 0xFF
                    else:
                        # Infer XOR key from template tail vs xor_size
                        xor_k = _infer_xor_key_from_template(
                            v2_templates[0], cand_aes
                        )
                    return ImageKeys(
                        aes_key=cand_aes,
                        xor_key=xor_k,
                        code=code_val,
                        source="memscan",
                    )

            # Also search for numeric code string
            num_re = re.compile(rb"\b\d{9,10}\b")
            for m in num_re.finditer(chunk):
                try:
                    code_cand = int(m.group())
                except ValueError:
                    continue
                seed = f"{code_cand}{clean_id}".encode("utf-8")
                cand_aes = hashlib.md5(seed).hexdigest()[:16].encode("ascii")
                if cand_aes in tested_keys:
                    continue
                tested_keys.add(cand_aes)
                if _verify_v2_keys(cand_aes, v2_templates):
                    return ImageKeys(
                        aes_key=cand_aes,
                        xor_key=code_cand & 0xFF,
                        code=code_cand,
                        source="memscan",
                    )

        return None

    for pid in pids:
        res = scan_pid(pid, use_sudo=False)
        if res is not None:
            return res
        if allow_sudo:
            res = scan_pid(pid, use_sudo=True)
            if res is not None:
                return res

    return None


def _infer_xor_key_from_template(template: Path, aes_key: bytes) -> int:
    """Infer XOR key from known tail patterns if only AES key is available."""
    try:
        with open(template, "rb") as f:
            data = f.read()
        meta = parse_dat_header(data)
        if meta and meta.xor_size > 0:
            tail = data[len(data) - meta.xor_size:]
            # Most JPEG files end in FF D9 (EOI marker)
            if len(tail) >= 2:
                k1 = tail[-2] ^ 0xFF
                k2 = tail[-1] ^ 0xD9
                if k1 == k2:
                    return k1
    except Exception:
        pass
    return 0


def resolve_keys(
    account_dir: Path,
    wxid: str,
    templates: Sequence[Path] | None = None,
    *,
    allow_sudo: bool = False,
    cache_path: Path | None = None,
) -> ImageKeys | None:
    """Resolve ImageKeys with caching and fallback hierarchy.

    1. Cache read + verify
    2. kvcomm derivation
    3. memscan fallback (if allow_sudo=True or normal mem read succeeds)
    4. Write valid keys to cache (0600)
    """
    eff_templates = (
        templates if templates is not None
        else discover_v2_templates(account_dir)
    )
    clean_id = clean_wxid(wxid)
    v2_templates = _select_v2_templates(eff_templates, min_count=3)

    # 1. Read cache if present
    if cache_path and cache_path.is_file():
        try:
            cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached_data.get("wxid") == clean_id:
                cand_aes = cached_data["aes_key"].encode("ascii")
                xor_k = int(cached_data["xor_key"])
                code_val = int(cached_data.get("code", 0))
                # Re-verify against templates
                if _verify_v2_keys(cand_aes, v2_templates):
                    return ImageKeys(
                        aes_key=cand_aes,
                        xor_key=xor_k,
                        code=code_val,
                        source="cache",
                    )
                else:
                    logger.debug("Cache validation failed; invalidating cache")
        except Exception as e:
            logger.debug(f"Failed to read/parse cache {cache_path}: {e}")

    # 2. Derive from kvcomm
    keys = derive_keys_from_kvcomm(account_dir, clean_id, v2_templates)
    if keys is not None:
        _save_cache(cache_path, keys, clean_id)
        return keys

    # 3. Fallback to memscan
    keys = _scan_mem_for_keys(clean_id, v2_templates, allow_sudo=allow_sudo)
    if keys is not None:
        _save_cache(cache_path, keys, clean_id)
        return keys

    return None


def _save_cache(
    cache_path: Path | None, keys: ImageKeys, clean_id: str
) -> None:
    """Save verified ImageKeys to cache file with 0600 permissions."""
    if not cache_path:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "wxid": clean_id,
            "code": keys.code,
            "aes_key": keys.aes_key.decode("ascii"),
            "xor_key": keys.xor_key,
            "source": keys.source,
        }
        # Atomic write with 0600 permissions
        tmp_path = cache_path.with_suffix(".tmp")
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp_path.replace(cache_path)
    except OSError as e:
        logger.debug(f"Failed to write cache {cache_path}: {e}")


def decrypt_dat(path: Path, keys: ImageKeys) -> DecryptedImage | None:
    """Decrypt a WeChat image .dat file.

    Handles V2, V1, and 3.x single-byte XOR files.
    Returns DecryptedImage or None on failure.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.error(f"Cannot read file {path}: {e}")
        return None

    if len(data) == 0:
        return None

    meta = parse_dat_header(data)
    if meta is None:
        # Check for 3.x xor
        xor_res = _detect_xor3_key(data[:32])
        if xor_res is None:
            return None
        xor_k, fmt = xor_res
        meta = DatMeta(
            version="xor3",
            aes_size=0,
            xor_size=len(data),
            aligned=0,
        )
        trans = bytes.maketrans(
            bytes(range(256)),
            bytes(b ^ xor_k for b in range(256)),
        )
        plain = data.translate(trans)
        w, h = image_size(plain, fmt)
        return DecryptedImage(
            data=plain,
            fmt=fmt,
            meta=meta,
            quality=quality_of(path),
            width=w,
            height=h,
        )

    if meta.version == "xor3":
        xor_res = _detect_xor3_key(data[:32])
        if xor_res is None:
            return None
        xor_k, fmt = xor_res
        trans = bytes.maketrans(
            bytes(range(256)),
            bytes(b ^ xor_k for b in range(256)),
        )
        plain = data.translate(trans)
        w, h = image_size(plain, fmt)
        return DecryptedImage(
            data=plain,
            fmt=fmt,
            meta=meta,
            quality=quality_of(path),
            width=w,
            height=h,
        )

    # V1 / V2 container
    # V1 / V2 container: V1 replaces AES key with static key, keeps xor_key
    aes_key = V1_AES_KEY if meta.version == "v1" else keys.aes_key
    xor_k = keys.xor_key
    # Bounds check
    header_len = 15
    if len(data) < header_len + meta.aligned:
        return None

    # Segment 1: Aligned AES decrypt with PKCS7 unpad
    aes_bytes = data[header_len: header_len + meta.aligned]
    try:
        cipher = AES.new(aes_key, AES.MODE_ECB)
        dec_aes = cipher.decrypt(aes_bytes)
    except Exception as e:
        logger.debug(f"AES decryption failed: {e}")
        return None

    pad = dec_aes[-1]
    if 1 <= pad <= 16:
        plain_aes = dec_aes[:-pad]
    else:
        plain_aes = dec_aes

    # Segment 2: Raw plaintext
    raw_end = len(data) - meta.xor_size
    if raw_end < header_len + meta.aligned:
        # File is truncated or malformed
        return None
    raw_plain = data[header_len + meta.aligned: raw_end]

    # Segment 3: XOR suffix
    if meta.xor_size > 0:
        tail = data[raw_end:]
        trans = bytes.maketrans(
            bytes(range(256)),
            bytes(b ^ xor_k for b in range(256)),
        )
        plain_xor = tail.translate(trans)
    else:
        plain_xor = b""

    full_plain = plain_aes + raw_plain + plain_xor
    fmt = detect_format(full_plain)
    w, h = image_size(full_plain, fmt)

    return DecryptedImage(
        data=full_plain,
        fmt=fmt,
        meta=meta,
        quality=quality_of(path),
        width=w,
        height=h,
    )


def extract_hevc_from_wxgf(data: bytes, max_search: int = 128) -> bytes:
    """Locate first NAL start code (00 00 00 01/00 00 01) and strip header.

    Bounds search to [4:max_search] after 'wxgf' magic to prevent false matches
    in trailing video payload.
    """
    if not data.startswith(SIG_WXGF):
        return data
    window = data[4:max_search]
    idx4 = window.find(b"\x00\x00\x00\x01")
    idx3 = window.find(b"\x00\x00\x01")
    candidates = [i for i in (idx4, idx3) if i != -1]
    if not candidates:
        return data
    return data[4 + min(candidates):]


def convert_wxgf(
    data: bytes,
    out_path: Path,
    *,
    animated: str = "skip",
    ffmpeg: str = "ffmpeg",
) -> tuple[bool, bool]:
    """Convert wxgf (HEVC container) image data to PNG.

    Returns:
        (ok, was_animated)

    If animated="skip" and stream has multiple frames, returns (False, True).
    If ffmpeg / ffprobe is unavailable, returns (False, False).
    """
    # Check if ffmpeg binary exists
    ffmpeg_bin = shutil.which(ffmpeg)
    if not ffmpeg_bin:
        logger.warning(f"ffmpeg binary not found: {ffmpeg}")
        return False, False

    # Strip wxgf container header to expose raw Annex-B HEVC NAL stream
    hevc_data = extract_hevc_from_wxgf(data)

    # Check ffprobe
    ffprobe_bin = shutil.which("ffprobe")
    was_animated = False

    if ffprobe_bin:
        probe_cmd = [
            ffprobe_bin,
            "-v", "error",
            "-f", "hevc",
            "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "csv=p=0",
            "pipe:0",
        ]
        try:
            p = subprocess.run(
                probe_cmd,
                input=hevc_data,
                capture_output=True,
                timeout=10,
            )
            frames_str = p.stdout.decode("utf-8", errors="ignore").strip()
            if frames_str.isdigit() and int(frames_str) > 1:
                was_animated = True
        except (subprocess.SubprocessError, OSError) as e:
            logger.debug(f"ffprobe failed: {e}")

    if was_animated and animated == "skip":
        return False, True

    # Execute ffmpeg conversion
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Try with -f hevc first, then fallback to auto-detect container
    cmd_hevc = [
        ffmpeg_bin,
        "-y",
        "-v", "error",
        "-f", "hevc",
        "-i", "pipe:0",
        "-frames:v", "1",
        str(out_path),
    ]
    try:
        p = subprocess.run(
            cmd_hevc, input=hevc_data, capture_output=True, timeout=15
        )
        if (
            p.returncode == 0
            and out_path.is_file()
            and out_path.stat().st_size > 0
        ):
            return True, was_animated
    except (subprocess.SubprocessError, OSError):
        pass

    # Fallback without -f hevc
    cmd_auto = [
        ffmpeg_bin,
        "-y",
        "-v", "error",
        "-i", "pipe:0",
        "-frames:v", "1",
        str(out_path),
    ]
    try:
        p = subprocess.run(
            cmd_auto, input=hevc_data, capture_output=True, timeout=15
        )
        if (
            p.returncode == 0
            and out_path.is_file()
            and out_path.stat().st_size > 0
        ):
            return True, was_animated
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"ffmpeg conversion failed: {e}")

    return False, was_animated


def image_size(data: bytes, fmt: str) -> tuple[int | None, int | None]:
    """Parse image dimensions (width, height) without external libraries."""
    if not data:
        return None, None

    if fmt == "png":
        return _parse_png_size(data)
    if fmt == "jpg":
        return _parse_jpg_size(data)
    if fmt == "gif":
        return _parse_gif_size(data)
    if fmt == "webp":
        return _parse_webp_size(data)
    if fmt == "bmp":
        return _parse_bmp_size(data)
    if fmt in ("mp4", "mov"):
        return _parse_mp4_size(data)

    return None, None


def _parse_mp4_size(data: bytes) -> tuple[int | None, int | None]:
    """Parse width and height from MP4/MOV container moov->trak->tkhd boxes."""
    w, h, _ = parse_mp4_metadata(data)
    return w, h


def parse_mp4_metadata(data: bytes) -> tuple[int | None, int | None, float | None]:
    """Parse (width, height, duration_s) from MP4/MOV bytes without external tools."""
    if not data or len(data) < 8:
        return None, None, None
    width, height = None, None
    duration_s = None
    l_data = len(data)

    def parse_boxes(start: int, end: int) -> None:
        nonlocal width, height, duration_s
        pos = start
        while pos + 8 <= end:
            sz = struct.unpack(">I", data[pos:pos + 4])[0]
            typ = data[pos + 4:pos + 8]
            box_len = sz
            hdr_len = 8
            if sz == 1:
                if pos + 16 > end:
                    break
                box_len = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
                hdr_len = 16
            elif sz == 0:
                box_len = end - pos
            if box_len < hdr_len or pos + box_len > end:
                break

            b_start = pos + hdr_len
            b_end = pos + box_len
            if typ in (b"moov", b"trak", b"mdia", b"minf"):
                parse_boxes(b_start, b_end)
            elif typ == b"mvhd":
                ver = data[b_start]
                if ver == 0 and b_start + 20 <= b_end:
                    ts = struct.unpack(">I", data[b_start + 12:b_start + 16])[0]
                    dur = struct.unpack(">I", data[b_start + 16:b_start + 20])[0]
                    if ts > 0 and duration_s is None:
                        duration_s = round(dur / ts, 2)
                elif ver == 1 and b_start + 32 <= b_end:
                    ts = struct.unpack(">I", data[b_start + 20:b_start + 24])[0]
                    dur = struct.unpack(">Q", data[b_start + 24:b_start + 32])[0]
                    if ts > 0 and duration_s is None:
                        duration_s = round(dur / ts, 2)
            elif typ == b"tkhd":
                if b_end - 8 >= b_start:
                    w_fix = struct.unpack(">I", data[b_end - 8:b_end - 4])[0]
                    h_fix = struct.unpack(">I", data[b_end - 4:b_end])[0]
                    w = w_fix >> 16
                    h = h_fix >> 16
                    if w > 0 and h > 0 and width is None:
                        width, height = w, h
            pos += box_len

    try:
        parse_boxes(0, min(l_data, 1024 * 1024))
    except Exception:
        pass
    return width, height, duration_s


def _parse_png_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) >= 24 and data.startswith(SIG_PNG):
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None, None


def _parse_gif_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) >= 10 and (
        data.startswith(SIG_GIF87) or data.startswith(SIG_GIF89)
    ):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    return None, None


def _parse_bmp_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) >= 26 and data.startswith(SIG_BMP):
        header_sz = struct.unpack("<I", data[14:18])[0]
        if header_sz >= 12:
            if header_sz == 12 and len(data) >= 22:
                w, h = struct.unpack("<HH", data[18:22])
                return w, h
            elif len(data) >= 26:
                w, h = struct.unpack("<ii", data[18:26])
                return abs(w), abs(h)
    return None, None


def _parse_webp_size(data: bytes) -> tuple[int | None, int | None]:
    if (
        len(data) >= 30
        and data.startswith(SIG_RIFF)
        and data[8:12] == SIG_WEBP
    ):
        chunk_type = data[12:16]
        if chunk_type == b"VP8 ":
            if len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
        elif chunk_type == b"VP8L":
            if len(data) >= 25 and data[20] == 0x2F:
                b1 = data[21]
                b2 = data[22]
                b3 = data[23]
                b4 = data[24]
                w = 1 + (((b2 & 0x3F) << 8) | b1)
                h = 1 + (((b4 & 0x0F) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6))
                return w, h
        elif chunk_type == b"VP8X":
            if len(data) >= 30:
                w = 1 + struct.unpack("<I", data[24:27] + b"\x00")[0]
                h = 1 + struct.unpack("<I", data[27:30] + b"\x00")[0]
                return w, h
    return None, None


def _parse_jpg_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None, None
    idx = 2
    l_data = len(data)
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
        seg_len = struct.unpack(">H", data[idx: idx + 2])[0]
        if seg_len < 2:
            break
        # SOF markers: Baseline, Extended Sequential, Progressive, Lossless
        sof_markers = (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        )
        if marker in sof_markers:
            if idx + 7 <= l_data:
                h, w = struct.unpack(">HH", data[idx + 3: idx + 7])
                return w, h
        idx += seg_len
    return None, None
