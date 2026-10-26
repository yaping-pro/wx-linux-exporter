#!/usr/bin/env python3
"""WeChat 媒体解析 worker（Linux 家服）。

职责：把「白名单会话内、实体存在的文件附件」解析成 Markdown，写进本地 ledger，
供 export API 注入到既有 text 字段。全程零凭据（走 MinerU 免 token agent 通道）。

设计约束（均来自实测）：
  * msg/file 是微信实时写入的**活动目录**，不在原子快照里 → 必须先复制到 staging
    并校验大小稳定，否则会把"下载中的半截 PDF"解析成残缺正文且无感知。
  * 消息 ↔ 磁盘路径的确定性关联只能靠 hardlink.db：appmsg `<md5>` →
    file_hardlink_info_v4.md5 → file_name + dir2id[dir1]（月份目录）。
    实测 68/78 可解；余下 10 条 dir1 是会话哈希，走同名文件唯一性兜底，仍不唯一则
    记 unresolved，不上传、不推进该媒体状态。
  * 幂等按内容 md5：同一文件转发到多个白名单群只解析一次。
  * 日预算硬闸：超限立即短路，不静默烧额度。

退出码：0 正常（含无事可做）；1 致命错误（配置/IO）。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zstandard
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI_TZ = timezone(timedelta(hours=8))
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SCRIPT_DIR = Path(__file__).resolve().parent
MINERU_CLIENT = SCRIPT_DIR / "mineru_agent.py"

DEFAULT_VAULT = Path.home() / ".local/share" / "wechat-vault"
DEFAULT_POLICY = DEFAULT_VAULT / "config" / "media_policy.json"
DEFAULT_WECHAT_GLOB = str(Path.home() / "Documents" / "xwechat_files" / "*" / "msg" / "file")

# MinerU 通道边界（与 mineru_agent.py 保持一致）
AGENT_MAX_BYTES = 10 * 1024 * 1024
PRECISE_MAX_BYTES = 200 * 1024 * 1024
# agent 轻量通道实测支持的扩展名；html/doc/xls/ppt 等需走 precise（要有 token）
AGENT_SUPPORTED_EXT = {"pdf", "docx", "pptx", "xlsx",
                       "png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}


def precise_token_available(token_file: str | None) -> bool:
    """token 探测：只判断存在性，绝不读取内容到日志。"""
    if token_file:
        p = Path(token_file).expanduser()
        if p.is_file() and p.read_text(encoding="utf-8").strip():
            return True
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred:
        for name in ("mineru-token", "mineru_token", "mineru"):
            p = Path(cred) / name
            if p.is_file() and p.read_text(encoding="utf-8").strip():
                return True
    return bool(os.environ.get("MINERU_API_TOKEN", "").strip())


STATE_READY = "ready"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"
STATE_PENDING = "pending"

# ── 微信自带语音转写（实测：转写文本落在 message 表 packed_info_data 列）────────
# 实测 blob（protobuf）：
#   未转写: 08 35 10 0d 58 00                      → field1=53, field2=13, field11=0
#   已转写: 08 35 10 0d 2a 7c 08 02 12 78 <120B文本> 58 00
#                                            ^^ field5 → 子消息 field2 = 转写文本
VOICE_TEXT_FIELD = 5
VOICE_TEXT_SUBFIELD = 2


def _pb_read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 越界")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _pb_fields(buf: bytes):
    """极简 protobuf 字段迭代器，产出 (field_no, wire_type, value)。"""
    i = 0
    while i < len(buf):
        tag, i = _pb_read_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _pb_read_varint(buf, i)
            yield fn, wt, v
        elif wt == 2:
            ln, i = _pb_read_varint(buf, i)
            if i + ln > len(buf):
                raise ValueError("length 越界")
            yield fn, wt, buf[i:i + ln]
            i += ln
        elif wt == 5:
            i += 4
            yield fn, wt, None
        elif wt == 1:
            i += 8
            yield fn, wt, None
        else:
            raise ValueError(f"不支持的 wire type {wt}")


def extract_voice_text(packed) -> str:
    """从语音消息的 packed_info_data 取微信自带转写文本；未转写/解析失败返回空串。"""
    if not packed:
        return ""
    b = bytes(packed) if isinstance(packed, (bytes, bytearray, memoryview)) else b""
    if not b:
        return ""
    if b[:4] == ZSTD_MAGIC:
        try:
            b = zstandard.ZstdDecompressor().decompress(b)
        except Exception:  # noqa: BLE001
            return ""
    try:
        for fn, wt, val in _pb_fields(b):
            if fn == VOICE_TEXT_FIELD and wt == 2:
                for sfn, swt, sval in _pb_fields(val):
                    if sfn == VOICE_TEXT_SUBFIELD and swt == 2:
                        return sval.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


DEFAULT_POLICY_BODY = {
    "enabled": False,
    "chat_allowlist": [],
    "kinds": {"file": True, "image": False, "voice": True, "video": False},
    "image": {
        "enabled": False,
        "quality_preference": ["hd", "full", "thumb", "bubble"],
        "max_pixels": 50_000_000,
        "allow_sudo_memscan": False,
        "wxgf": {"animated": "skip"},
        "ocr": {"enabled": True, "providers": ["rapidocr", "tesseract"], "timeout": 60},
    },
    "file_ext_allow": ["pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
                       "html", "htm", "md", "txt", "csv"],
    "skip_ext": ["zip", "rar", "7z", "gz", "svg", "m4a", "mp3", "mp4"],
    "max_bytes": AGENT_MAX_BYTES,
    "precise": {"enabled": True, "max_bytes": PRECISE_MAX_BYTES, "model": "vlm", "token_file": None},
    "max_items_per_run": 10,
    "daily_file_budget": 30,
    "lookback_hours": 72,
    "initial_since_days": 7,
    "dedupe": {"by": "md5", "ttl_days": 180},
    "readiness": {"max_pending_age_s": 1800, "max_parse_retries": 2},
    "render": {"max_chars_per_doc": 4000, "inject_into_text": False},
}


def log(msg: str, verbose: bool = True) -> None:
    if verbose:
        print(msg, file=sys.stderr)


def load_policy(path: Path) -> dict:
    if not path.is_file():
        log(f"[WARN] 策略文件不存在，使用安全默认（enabled=false）：{path}")
        return dict(DEFAULT_POLICY_BODY)
    body = dict(DEFAULT_POLICY_BODY)
    body.update(json.loads(path.read_text(encoding="utf-8")))
    for k, v in DEFAULT_POLICY_BODY.items():
        if isinstance(v, dict) and isinstance(body.get(k), dict):
            merged = dict(v)
            merged.update(body[k])
            body[k] = merged
    return body


def load_ledger(path: Path) -> dict:
    if path.is_file():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("assets", {})
                d.setdefault("by_message", {})
                d.setdefault("anchors", {})
                d.setdefault("daily", {})
                return d
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] ledger 解析失败，另起新账本：{e}")
    return {"version": 1, "watermark": 0, "assets": {}, "by_message": {}, "anchors": {}, "daily": {}}


def save_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def resolve_decrypted(vault: Path) -> Path:
    cur = vault / "decrypted" / "current"
    if cur.is_dir():
        return cur
    versions = vault / "decrypted" / "versions"
    if versions.is_dir():
        cands = sorted((d for d in versions.iterdir() if d.is_dir()), key=lambda d: d.name)
        if cands:
            return cands[-1]
    return cur


def decompress(raw) -> str:
    if raw is None:
        return ""
    b = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8", "replace")
    b = bytes(b)
    if b.startswith(ZSTD_MAGIC):
        try:
            return zstandard.ZstdDecompressor().decompress(b).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
    return b.decode("utf-8", "replace")


def discover_wechat_files_root(pattern: str) -> Path | None:
    import glob as _glob
    for cand in sorted(_glob.glob(pattern)):
        p = Path(cand)
        if p.is_dir():
            return p
    return None


def load_hardlink(decrypted: Path) -> tuple[dict[int, str], dict[str, tuple]]:
    hl = decrypted / "hardlink" / "hardlink.db"
    if not hl.is_file():
        return {}, {}
    con = sqlite3.connect(f"file:{hl}?mode=ro&immutable=1", uri=True)
    try:
        dir2id = {r[0]: r[1] for r in con.execute("SELECT rowid, username FROM dir2id")}
        files = {}
        cursor = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='file_hardlink_info_v4'"
        )
        if cursor.fetchone():
            for md5, fn, d1, d2, size in con.execute(
                "SELECT md5, file_name, dir1, dir2, file_size FROM file_hardlink_info_v4"
            ):
                files[str(md5)] = (str(fn), d1, d2, int(size or 0))
        return dir2id, files
    finally:
        con.close()


def resolve_source(files_root: Path, dir2id: dict, entry: tuple, name_hint: str) -> tuple[Path | None, str]:
    """返回 (路径, 判定说明)。dir1 为月份目录时直取；否则按同名唯一性兜底。"""
    fn, d1, d2, _size = entry
    month = dir2id.get(d1) or dir2id.get(d2) or ""
    if re.fullmatch(r"\d{4}-\d{2}", month or ""):
        p = files_root / month / fn
        if p.is_file():
            return p, "hardlink:month"
    # dir1 是会话哈希：在全部月份目录里找同名文件
    matches = [p for p in files_root.glob(f"*/{fn}") if p.is_file()]
    if len(matches) == 1:
        return matches[0], "hardlink:unique-name"
    if len(matches) > 1:
        return None, f"ambiguous:{len(matches)}"
    # 最后兜底：消息 XML 的 title 精确匹配（同名唯一时）
    if name_hint:
        matches = [p for p in files_root.glob(f"*/{name_hint}") if p.is_file()]
        if len(matches) == 1:
            return matches[0], "title:unique-name"
    return None, "unresolved"


def stage_file(src: Path, staging: Path, expected_size: int) -> tuple[Path, str, int]:
    """复制到 staging 并校验大小稳定，避免解析"下载中的半截文件"。"""
    staging.mkdir(parents=True, exist_ok=True)
    dst = staging / src.name
    shutil.copy2(src, dst)
    size = dst.stat().st_size
    if expected_size and size != expected_size:
        # 微信可能仍在写入：等一拍再看
        time.sleep(1.5)
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        if size != expected_size:
            raise ValueError(f"size mismatch: got {size}, expect {expected_size}")
    s1 = dst.stat().st_size
    time.sleep(0.3)
    s2 = dst.stat().st_size
    if s1 != s2:
        raise ValueError(f"unstable size: {s1} -> {s2}")
    h = hashlib.sha256()
    with dst.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return dst, h.hexdigest(), size


def call_mineru(staged: Path, out_dir: Path, name: str, lang: str, timeout: int,
                pages: str | None = None, mode: str = "agent",
                token_file: str | None = None) -> tuple[dict, int]:
    cmd = [sys.executable, str(MINERU_CLIENT), str(staged), "-o", str(out_dir),
           "--name", name, "-l", lang, "--timeout", str(timeout), "--mode", mode]
    if pages:
        cmd += ["--pages", pages]
    if token_file:
        cmd += ["--token-file", token_file]
    p = subprocess.run(cmd, capture_output=True, text=True)
    payload: dict = {}
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except Exception:  # noqa: BLE001
                continue
    if not payload:
        payload = {"state": "failed", "deny_reason": f"client_rc={p.returncode}",
                   "detail": (p.stderr or "")[-300:]}
    return payload, p.returncode


EXIT_PAGE_LIMIT = 3
EXIT_NO_CREDENTIAL = 9
AGENT_PAGE_CHUNK = 20
PRECISE_PAGE_CHUNK = 200
MAX_CHUNKS = 8


def call_mineru_chunked(staged: Path, out_dir: Path, name: str, lang: str,
                        timeout: int, mode: str = "agent",
                        token_file: str | None = None) -> tuple[dict, int]:
    """先整篇试；命中页数上限则按通道各自的页上限切片解析后拼接。"""
    payload, rc = call_mineru(staged, out_dir, name, lang, timeout, mode=mode,
                              token_file=token_file)
    if rc != EXIT_PAGE_LIMIT:
        return payload, rc

    chunk = AGENT_PAGE_CHUNK if mode == "agent" else PRECISE_PAGE_CHUNK
    log(f"[INFO] {name} 超过 {chunk} 页，改按页切片解析（{mode}）")
    parts: list[str] = []
    for i in range(MAX_CHUNKS):
        start = i * chunk + 1
        end = (i + 1) * chunk
        part_dir = out_dir / f"part{i + 1}"
        p2, rc2 = call_mineru(staged, part_dir, name, lang, timeout,
                              pages=f"{start}-{end}", mode=mode, token_file=token_file)
        md = part_dir / "full.md"
        if rc2 != 0 or not md.is_file():
            break
        body = md.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            break
        parts.append(body)

    if not parts:
        return payload, rc

    out_dir.mkdir(parents=True, exist_ok=True)
    merged = out_dir / "full.md"
    merged.write_text("\n\n".join(parts), encoding="utf-8")
    for i in range(MAX_CHUNKS):
        shutil.rmtree(out_dir / f"part{i + 1}", ignore_errors=True)
    return {
        "state": "ready",
        "md_path": str(merged),
        "chars": len(merged.read_text(encoding="utf-8", errors="replace")),
        "chunks": len(parts),
    }, 0


def iter_file_messages(decrypted: Path, allow_md5: set[str], since: int):
    """产出 (chat_id, local_id, create_time, appmsg_xml)。"""
    shards = sorted(p for p in (decrypted / "message").glob("*.db")
                    if re.fullmatch(r"(message|biz_message)_\d+\.db", p.name))
    for shard in shards:
        con = sqlite3.connect(f"file:{shard}?mode=ro&immutable=1", uri=True)
        try:
            chat_by_table: dict[str, str] = {}
            try:
                for _rid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
                    if not uname:
                        continue
                    h = hashlib.md5(str(uname).encode()).hexdigest()
                    if h in allow_md5:
                        chat_by_table[h] = str(uname)
            except sqlite3.Error:
                pass
            for tbl, in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ):
                chat = chat_by_table.get(tbl[4:])
                if not chat:
                    continue
                q = (f"SELECT local_id, create_time, message_content, compress_content "
                     f"FROM [{tbl}] WHERE (local_type & 0xFFFFFFFF) IN (49) "
                     f"AND create_time >= ? ORDER BY create_time DESC")
                for lid, ct, mc, cc in con.execute(q, (since,)):
                    text = decompress(mc) or decompress(cc)
                    if "<type>6</type>" not in text and "<type>74</type>" not in text:
                        continue
                    yield chat, int(lid), int(ct), text
        finally:
            con.close()


def iter_voice_messages(decrypted: Path, allow_md5: set[str], since: int):
    """产出 (chat_id, local_id, create_time, packed_info_data)——仅语音消息。"""
    shards = sorted(p for p in (decrypted / "message").glob("*.db")
                    if re.fullmatch(r"(message|biz_message)_\d+\.db", p.name))
    for shard in shards:
        con = sqlite3.connect(f"file:{shard}?mode=ro&immutable=1", uri=True)
        try:
            chat_by_table: dict[str, str] = {}
            try:
                for _rid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
                    if not uname:
                        continue
                    h = hashlib.md5(str(uname).encode()).hexdigest()
                    if h in allow_md5:
                        chat_by_table[h] = str(uname)
            except sqlite3.Error:
                pass
            for tbl, in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ):
                chat = chat_by_table.get(tbl[4:])
                if not chat:
                    continue
                q = (f"SELECT local_id, create_time, packed_info_data FROM [{tbl}] "
                     f"WHERE (local_type & 0xFFFFFFFF) = 34 AND create_time >= ? "
                     f"ORDER BY create_time DESC")
                for lid, ct, packed in con.execute(q, (since,)):
                    yield chat, int(lid), int(ct), packed
        finally:
            con.close()


def clean_asset_id(raw_name: str) -> str:
    """去除 _t / _h / _b 后缀及扩展名，获取规范的 32 位十六进制 asset_id。"""
    stem = Path(raw_name).stem.lower()
    base = re.sub(r"_[thb]$", "", stem)
    return base


def variant_of_path(path: Path) -> str:
    stem = path.stem.lower()
    if stem.endswith("_h"):
        return "_h"
    elif stem.endswith("_t"):
        return "_t"
    elif stem.endswith("_b"):
        return "_b"
    return ""


def quality_of_path(path: Path) -> str:
    try:
        import wechat_image
        return wechat_image.quality_of(path)
    except (ImportError, AttributeError):
        stem = path.stem.lower()
        if stem.endswith("_h"):
            return "hd"
        elif stem.endswith("_t"):
            return "thumb"
        elif stem.endswith("_b"):
            return "bubble"
        return "full"


def rank_image_variants(paths: list[Path]) -> list[Path]:
    try:
        import wechat_image
        return wechat_image.rank_variants(paths)
    except (ImportError, AttributeError):
        def _score(p: Path) -> int:
            s = p.stem.lower()
            if s.endswith("_h"):
                return 4
            elif s.endswith("_t"):
                return 2
            elif s.endswith("_b"):
                return 1
            return 3  # full (no suffix)

        return sorted(paths, key=_score, reverse=True)


def resolve_chat_name(chat_id: str, target: Path) -> str:
    decrypted = resolve_decrypted(target) if (target / "decrypted").is_dir() else target
    contact_path = decrypted / "contact" / "contact.db"
    if contact_path.is_file():
        try:
            uri = f"file:{contact_path.resolve()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
            try:
                cursor = con.execute("PRAGMA table_info(contact)")
                cols = {r[1].lower(): r[1] for r in cursor.fetchall()}
                u_col = cols.get("username", "username")
                r_col = cols.get("remark", "remark")
                n_col = cols.get("nick_name", cols.get("nickname", "nick_name"))
                a_col = cols.get("alias", "alias")
                query = f"SELECT {r_col}, {n_col}, {a_col} FROM contact WHERE {u_col} = ?"
                row = con.execute(query, (chat_id,)).fetchone()
                if row:
                    remark = str(row[0] or "").strip()
                    nick = str(row[1] or "").strip()
                    alias = str(row[2] or "").strip()
                    display = remark or nick or alias
                    if display:
                        return display
            finally:
                con.close()
        except Exception:
            pass

    session_path = decrypted / "session" / "session.db"
    if session_path.is_file():
        try:
            uri = f"file:{session_path.resolve()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
            try:
                cursor = con.execute("PRAGMA table_info(SessionTable)")
                cols = {r[1].lower(): r[1] for r in cursor.fetchall()}
                if "username" in cols:
                    u_col = cols["username"]
                    d_col = (
                        cols.get("display_name")
                        or cols.get("remark")
                        or cols.get("nick_name")
                    )
                    if d_col:
                        query = f"SELECT {d_col} FROM SessionTable WHERE {u_col} = ?"
                        row = con.execute(query, (chat_id,)).fetchone()
                        if row and str(row[0] or "").strip():
                            return str(row[0]).strip()
            finally:
                con.close()
        except Exception:
            pass

    return chat_id


def load_image_hardlink(decrypted: Path) -> tuple[dict[int, str], dict[str, tuple]]:
    hl = decrypted / "hardlink" / "hardlink.db"
    if not hl.is_file():
        return {}, {}
    con = sqlite3.connect(f"file:{hl}?mode=ro&immutable=1", uri=True)
    try:
        dir2id: dict[int, str] = {}
        try:
            dir2id = {r[0]: r[1] for r in con.execute("SELECT rowid, username FROM dir2id")}
        except sqlite3.Error:
            pass
        images: dict[str, tuple] = {}
        try:
            cursor = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='image_hardlink_info_v4'"
            )
            if cursor.fetchone():
                for md5, fn, d1, d2 in con.execute(
                    "SELECT md5, file_name, dir1, dir2 FROM image_hardlink_info_v4"
                ):
                    images[str(md5).lower()] = (str(fn), d1, d2)
        except sqlite3.Error:
            pass
        return dir2id, images
    finally:
        con.close()


def find_image_dat(
    attach_root: Path | None,
    cache_root: Path | None,
    chat_md5: str,
    month: str | None,
    asset_id: str,
) -> tuple[list[Path], str]:
    """寻找与 asset_id 匹配的所有变体 .dat 文件。"""
    candidates: list[Path] = []
    if month and re.fullmatch(r"\d{4}-\d{2}", month):
        if attach_root:
            img_dir = attach_root / chat_md5 / month / "Img"
            if img_dir.is_dir():
                for suffix in ("_h.dat", ".dat", "_t.dat"):
                    p = img_dir / f"{asset_id}{suffix}"
                    if p.is_file():
                        candidates.append(p)
        if cache_root:
            bubble_file = cache_root / month / "Message" / chat_md5 / "Bubble" / f"{asset_id}_b.dat"
            if bubble_file.is_file():
                candidates.append(bubble_file)
        if candidates:
            return candidates, "hardlink:month"

    found_by_month: dict[str, list[Path]] = {}
    if attach_root and (attach_root / chat_md5).is_dir():
        for month_dir in (attach_root / chat_md5).iterdir():
            if month_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}", month_dir.name):
                img_dir = month_dir / "Img"
                if img_dir.is_dir():
                    for suffix in ("_h.dat", ".dat", "_t.dat"):
                        p = img_dir / f"{asset_id}{suffix}"
                        if p.is_file():
                            found_by_month.setdefault(month_dir.name, []).append(p)

    if cache_root and cache_root.is_dir():
        for month_dir in cache_root.iterdir():
            if month_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}", month_dir.name):
                bubble_file = month_dir / "Message" / chat_md5 / "Bubble" / f"{asset_id}_b.dat"
                if bubble_file.is_file():
                    found_by_month.setdefault(month_dir.name, []).append(bubble_file)

    if not found_by_month:
        # 兜底：转发图片可能保存在最初接收该文件的其他会话目录下
        if attach_root and attach_root.is_dir():
            for c_dir in attach_root.iterdir():
                if c_dir.is_dir() and c_dir.name != chat_md5:
                    for month_dir in c_dir.iterdir():
                        if month_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}", month_dir.name):
                            img_dir = month_dir / "Img"
                            if img_dir.is_dir():
                                for suffix in ("_h.dat", ".dat", "_t.dat"):
                                    p = img_dir / f"{asset_id}{suffix}"
                                    if p.is_file():
                                        found_by_month.setdefault(month_dir.name, []).append(p)

    if not found_by_month:
        return [], "unresolved:not_found"
    if len(found_by_month) > 1:
        return [], "unresolved:ambiguous_month"
    single_month = next(iter(found_by_month.keys()))
    return found_by_month[single_month], f"probe:{single_month}"


def iter_image_messages(decrypted: Path, allow_md5: set[str], since: int):
    """产出 (chat_id, local_id, create_time, sender, img_info)——仅图片消息。"""
    shards = sorted(p for p in (decrypted / "message").glob("*.db")
                    if re.fullmatch(r"(message|biz_message)_\d+\.db", p.name))
    for shard in shards:
        con = sqlite3.connect(f"file:{shard}?mode=ro&immutable=1", uri=True)
        try:
            chat_by_table: dict[str, str] = {}
            n2id: dict[int, str] = {}
            try:
                for rid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
                    if not uname:
                        continue
                    uname_str = str(uname)
                    n2id[int(rid)] = uname_str
                    h = hashlib.md5(uname_str.encode()).hexdigest()
                    if h in allow_md5:
                        chat_by_table[h] = uname_str
            except sqlite3.Error:
                pass

            for tbl, in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ):
                chat = chat_by_table.get(tbl[4:])
                if not chat:
                    continue
                is_group = chat.endswith("@chatroom")
                q = (f"SELECT local_id, real_sender_id, create_time, message_content, compress_content "
                     f"FROM [{tbl}] WHERE (local_type & 0xFFFFFFFF) = 3 AND create_time >= ? "
                     f"ORDER BY create_time DESC")
                for lid, rsid, ct, mc, cc in con.execute(q, (since,)):
                    text = decompress(mc) or decompress(cc)
                    sender = "我"
                    if is_group:
                        match_sender = re.match(r"^([a-zA-Z0-9_\-]+):\n", text)
                        if match_sender:
                            sender = match_sender.group(1)
                        elif rsid and int(rsid) in n2id:
                            sender = n2id[int(rsid)]
                    else:
                        if rsid and int(rsid) in n2id:
                            sender = n2id[int(rsid)]
                        elif rsid == 0:
                            sender = "我"
                        else:
                            sender = chat

                    m_md5 = (
                        re.search(r'md5=["\']([0-9a-fA-F]{32}(?:_[thb])?)["\']', text)
                        or re.search(r'<md5>([0-9a-fA-F]{32}(?:_[thb])?)</md5>', text)
                    )
                    m_len = (
                        re.search(r'length=["\'](\d+)["\']', text)
                        or re.search(r'<totallen>(\d+)</totallen>', text)
                    )
                    m_aes = re.search(r'aeskey=["\']([^"\']+)["\']', text)
                    img_info = {
                        "md5": (m_md5.group(1).lower() if m_md5 else ""),
                        "length": int(m_len.group(1)) if m_len else 0,
                        "aeskey": (m_aes.group(1) if m_aes else ""),
                        "raw_xml": text,
                    }
                    yield chat, int(lid), int(ct), sender, img_info
        finally:
            con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="WeChat 媒体解析 worker")
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--vault", default=str(DEFAULT_VAULT))
    ap.add_argument("--dry-run", action="store_true", help="只打印将解析的清单，不上传")
    ap.add_argument("--max-items", type=int, default=None)
    ap.add_argument("--since-days", type=int, default=None,
                    help="显式回溯天数（一次性回填用；不给则按 ledger 水位 − lookback_hours）")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--lang", default="ch")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    policy_path = Path(args.policy).expanduser()
    vault = Path(args.vault).expanduser()
    policy = load_policy(policy_path)

    if not policy.get("enabled"):
        log("[INFO] media_policy.enabled=false，无事可做。")
        return 0

    allow = [c for c in (policy.get("chat_allowlist") or []) if c]
    if not allow:
        log("[INFO] chat_allowlist 为空 = 媒体全关，无事可做。")
        return 0
    kinds = policy.get("kinds") or {}
    if not (kinds.get("file") or kinds.get("voice") or kinds.get("image")):
        log("[INFO] kinds.file、kinds.voice 与 kinds.image 均关闭，无事可做。")
        return 0

    lock_path = vault / "state" / "media.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("[INFO] 已有 worker 在运行，跳过本轮。")
        return 0
    decrypted = resolve_decrypted(vault)
    if not (decrypted / "message").is_dir():
        log(f"[ERROR] 解密库不可用：{decrypted}")
        lock_fh.close()
        return 1

    ledger_path = vault / "state" / "media_ledger.json"
    ledger = load_ledger(ledger_path)
    today = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    daily = ledger["daily"].setdefault(today, {"parsed": 0, "skipped": 0, "bytes": 0})

    budget = int(policy.get("daily_file_budget") or 0)
    max_items = args.max_items if args.max_items is not None else int(policy.get("max_items_per_run") or 10)
    precise_cfg = policy.get("precise") or {}
    precise_max_bytes = int(precise_cfg.get("max_bytes") or PRECISE_MAX_BYTES)
    token_file = precise_cfg.get("token_file")
    token_ok = bool(precise_cfg.get("enabled", True)) and precise_token_available(token_file)
    ext_allow = {e.lower().lstrip(".") for e in policy.get("file_ext_allow") or []}
    skip_ext = {e.lower().lstrip(".") for e in policy.get("skip_ext") or []}
    render_cfg = policy.get("render") or {}
    max_chars = int(render_cfg.get("max_chars_per_doc") or 4000)

    files_root = discover_wechat_files_root(policy.get("wechat_files_glob") or DEFAULT_WECHAT_GLOB)
    if not files_root and kinds.get("file"):
        log("[ERROR] 未找到微信接收文件目录（msg/file）。")
        return 1

    account_dir = (
        Path(policy["wechat_account_dir"]).expanduser()
        if policy.get("wechat_account_dir")
        else (files_root.parent.parent if files_root else None)
    )
    attach_root = (
        Path(policy["wechat_attach_root"]).expanduser()
        if policy.get("wechat_attach_root")
        else ((account_dir / "msg" / "attach") if account_dir else None)
    )
    cache_root = (
        Path(policy["wechat_cache_root"]).expanduser()
        if policy.get("wechat_cache_root")
        else ((account_dir / "cache") if account_dir else None)
    )
    wxid = policy.get("wxid") or (account_dir.name if account_dir else "")
    log(f"[INFO] files_root={files_root}  attach_root={attach_root}  vault={vault}  allow={allow}")
    dir2id, hl_files = load_hardlink(decrypted)
    allow_md5 = {hashlib.md5(c.encode()).hexdigest() for c in allow}

    lookback = int(policy.get("lookback_hours") or 72) * 3600
    initial_days = int(policy.get("initial_since_days") or 7)
    wm = int(ledger.get("watermark") or 0)
    # 首次运行不做全历史回溯：只从最近 N 天开始，且按时间倒序（新内容优先）
    if args.since_days is not None:
        since = int(time.time()) - int(args.since_days) * 86400
    else:
        since = (wm - lookback) if wm else int(time.time()) - initial_days * 86400
    since = max(0, since)
    log(f"[INFO] since={datetime.fromtimestamp(since, SHANGHAI_TZ).isoformat()}  "
        f"budget={daily['parsed']}/{budget}  max_items={max_items}  "
        f"precise_token={'available' if token_ok else 'unavailable'}")

    staging_root = vault / "staging" / "media"
    out_root = vault / "exports" / "media"
    processed = 0
    settled_max = since
    summary = {"ready": 0, "skipped": 0, "failed": 0, "unresolved": 0}

    def mark(key: str, value, ts: int) -> None:
        """登记消息→资产映射，并把已结算水位推进到该消息时间。"""
        nonlocal settled_max
        ledger["by_message"][key] = value
        if ts > settled_max:
            settled_max = ts

    file_iter = (iter_file_messages(decrypted, allow_md5, since)
                 if kinds.get("file") else iter(()))
    for chat, lid, ct, text in file_iter:
        if processed >= max_items:
            log("[INFO] 达到本轮 max_items 上限，收工。")
            break
        if daily["parsed"] >= budget:
            log("[INFO] 今日预算用尽，收工。")
            break

        m_md5 = re.search(r"<md5>([0-9a-f]{32})</md5>", text)
        m_title = re.search(r"<title>(.*?)</title>", text, re.S)
        m_ext = re.search(r"<fileext>(.*?)</fileext>", text)
        m_len = re.search(r"<totallen>(\d+)</totallen>", text)
        md5 = (m_md5.group(1) if m_md5 else "").lower()
        title = (m_title.group(1).strip() if m_title else "")
        ext = (m_ext.group(1).strip().lower() if m_ext else "")
        ext = re.sub(r"[^a-z0-9]", "", ext)
        declared = int(m_len.group(1)) if m_len else 0
        key = f"{chat}|{lid}"

        if not md5:
            mark(key, None, ct)
            summary["skipped"] += 1
            continue

        existing = ledger["assets"].get(md5)
        if existing and existing.get("state") in (STATE_READY, STATE_SKIPPED, STATE_FAILED):
            mark(key, md5, ct)
            summary["skipped"] += 1
            continue
        if existing and existing.get("state") == "pending":
            max_retries = int((policy.get("readiness") or {}).get("max_parse_retries") or 2)
            if int(existing.get("attempts") or 0) >= max_retries:
                existing["state"] = STATE_FAILED
                existing["deny_reason"] = "retries_exhausted"
                mark(key, md5, ct)
                summary["failed"] += 1
                continue

        if ext in skip_ext:
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED, "deny_reason": "ext_denied",
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared}
            mark(key, md5, ct)
            daily["skipped"] += 1
            summary["skipped"] += 1
            continue
        if ext_allow and ext not in ext_allow:
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED, "deny_reason": "ext_not_allowed",
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared}
            mark(key, md5, ct)
            daily["skipped"] += 1
            summary["skipped"] += 1
            continue
        # 通道选择：agent 覆盖不到（格式或体积）则转 precise；无凭据时明确记因不静默丢
        if ext in AGENT_SUPPORTED_EXT and (not declared or declared <= AGENT_MAX_BYTES):
            channel = "agent"
        elif token_ok:
            channel = "precise"
        else:
            reason = "agent_unsupported" if ext not in AGENT_SUPPORTED_EXT else "oversize"
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED, "deny_reason": reason,
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared}
            mark(key, md5, ct)
            daily["skipped"] += 1
            summary["skipped"] += 1
            continue
        limit = AGENT_MAX_BYTES if channel == "agent" else precise_max_bytes
        if declared and declared > limit:
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED, "deny_reason": "oversize",
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared, "channel": channel}
            mark(key, md5, ct)
            daily["skipped"] += 1
            summary["skipped"] += 1
            continue

        entry = hl_files.get(md5)
        if not entry:
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED, "deny_reason": "missing_source",
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared}
            mark(key, md5, ct)
            summary["unresolved"] += 1
            continue

        src, how = resolve_source(files_root, dir2id, entry, title)
        if not src:
            ledger["assets"][md5] = {"md5": md5, "state": STATE_SKIPPED,
                                     "deny_reason": f"unresolved:{how}",
                                     "ext": ext, "name": title, "chat": chat, "local_id": lid,
                                     "create_time": ct, "bytes": declared}
            mark(key, md5, ct)
            summary["unresolved"] += 1
            continue

        if args.dry_run:
            log(f"[DRY] {chat} lid={lid} {ext} {declared}B {src.name} ({how})")
            processed += 1
            continue

        processed += 1
        stage_dir = staging_root / md5
        out_dir = out_root / today / md5
        try:
            staged, sha, size = stage_file(src, stage_dir, declared or entry[3])
        except Exception as e:  # noqa: BLE001
            ledger["assets"][md5] = {"md5": md5, "state": STATE_FAILED, "deny_reason": "staging",
                                     "detail": str(e)[:200], "ext": ext, "name": title,
                                     "chat": chat, "local_id": lid, "create_time": ct}
            mark(key, md5, ct)
            summary["failed"] += 1
            shutil.rmtree(stage_dir, ignore_errors=True)
            continue

        payload, rc = call_mineru_chunked(staged, out_dir, src.name, args.lang, args.timeout,
                                          mode=channel, token_file=token_file)
        state = payload.get("state") or STATE_FAILED
        if rc == EXIT_PAGE_LIMIT and state != "ready":
            state = STATE_SKIPPED
            payload["deny_reason"] = "page_limit"
        if rc == EXIT_NO_CREDENTIAL:
            state = STATE_SKIPPED
            payload["deny_reason"] = "no_credential"
        prev_attempts = int((ledger["assets"].get(md5) or {}).get("attempts") or 0)
        rec = {
            "md5": md5, "sha256": sha, "ext": ext, "name": title,
            "chat": chat, "local_id": lid, "create_time": ct, "bytes": size,
            "source_kind": how, "state": state, "attempts": prev_attempts + 1,
            "channel": channel,
            "deny_reason": payload.get("deny_reason"),
            "detail": payload.get("detail"),
            "parsed_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "elapsed_s": payload.get("elapsed_s"),
        }
        if state == STATE_READY:
            rec["md_path"] = payload.get("md_path")
            rec["chars"] = payload.get("chars")
            rec["text_head"] = (out_dir / "full.md").read_text(encoding="utf-8", errors="replace")[:max_chars]
            daily["parsed"] += 1
            daily["bytes"] += size
            summary["ready"] += 1
        else:
            if state == "skipped":
                daily["skipped"] += 1
                summary["skipped"] += 1
                ledger["assets"][md5] = rec
                mark(key, md5, ct)
                shutil.rmtree(stage_dir, ignore_errors=True)
                log(f"[SKIP] {src.name}: {rec['deny_reason']}")
                continue
            # 可重试类错误不落终态，下轮再试
            reason = str(payload.get("deny_reason") or "")
            retryable = (
                reason.startswith("mineru:8")
                or reason in ("network", "timeout", "upload", "download", "throttled")
            )
            if retryable:
                rec["state"] = "pending"
            else:
                summary["failed"] += 1
            ledger["assets"][md5] = rec
            mark(key, md5, ct)
            shutil.rmtree(stage_dir, ignore_errors=True)
            log(f"[WARN] {src.name}: {rec['deny_reason']} {rec.get('detail') or ''}")
            continue

        ledger["assets"][md5] = rec
        ledger["by_message"][key] = md5
        shutil.rmtree(stage_dir, ignore_errors=True)
        log(f"[OK] {src.name} -> {out_dir}/full.md ({rec['chars']} chars, {rec['elapsed_s']}s)")

    # ── 语音：读微信自带转写（零成本、零外部依赖、无网络请求）──────────────
    if policy.get("kinds", {}).get("voice"):
        v_window = int((policy.get("readiness") or {}).get("max_pending_age_s") or 1800)
        v_ready = v_pending = v_skipped = 0
        now_ts = time.time()
        for chat, lid, ct, packed in iter_voice_messages(decrypted, allow_md5, since):
            key = f"{chat}|{lid}"
            vkey = hashlib.md5(f"voice|{chat}|{lid}".encode()).hexdigest()
            text = extract_voice_text(packed)
            if text:
                ledger["assets"][vkey] = {
                    "md5": vkey, "kind": "voice", "chat": chat, "local_id": lid,
                    "create_time": ct, "state": STATE_READY, "channel": "wechat-asr",
                    "chars": len(text), "text_head": text[:max_chars],
                    "parsed_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                }
                mark(key, vkey, ct)
                v_ready += 1
                log(f"[VOICE] {chat} lid={lid} -> {len(text)} 字")
            elif now_ts - ct > v_window:
                # 已过等待窗口仍无转写：落终态，避免永久挂住水位
                ledger["assets"][vkey] = {
                    "md5": vkey, "kind": "voice", "chat": chat, "local_id": lid,
                    "create_time": ct, "state": STATE_SKIPPED,
                    "deny_reason": "voice_not_transcribed",
                }
                mark(key, vkey, ct)
                v_skipped += 1
            else:
                # 保持 pending：转写到达后下一轮自动转 ready
                prev = ledger["assets"].get(vkey) or {}
                ledger["assets"][vkey] = {
                    "md5": vkey, "kind": "voice", "chat": chat, "local_id": lid,
                    "create_time": ct, "state": STATE_PENDING,
                    "deny_reason": "voice_awaiting_transcription",
                    "attempts": int(prev.get("attempts") or 0),
                }
                ledger["by_message"].setdefault(key, vkey)
                v_pending += 1
        summary["voice_ready"] = v_ready
        summary["voice_pending"] = v_pending
        summary["voice_skipped"] = v_skipped
        log(f"[VOICE] ready={v_ready} pending={v_pending} skipped={v_skipped}")

    # ── 图片：离线 V2 AES+XOR 解密 + 变体择优 + 本地 OCR ──────────────────
    if kinds.get("image"):
        img_cfg = policy.get("image") or {}
        max_pixels = int(img_cfg.get("max_pixels") or 50_000_000)
        wxgf_cfg = img_cfg.get("wxgf") or {}
        wxgf_animated = wxgf_cfg.get("animated", "skip")
        ocr_cfg = img_cfg.get("ocr") or {}
        ocr_enabled = bool(ocr_cfg.get("enabled", True))
        ocr_providers = tuple(ocr_cfg.get("providers", ["rapidocr", "tesseract"]))
        ocr_timeout = int(ocr_cfg.get("timeout", 60))
        img_pending_window = int((policy.get("readiness") or {}).get("max_pending_age_s") or 1800)
        img_max_retries = int((policy.get("readiness") or {}).get("max_parse_retries") or 2)

        dir2id_img, hl_images = load_image_hardlink(decrypted)

        try:
            import wechat_image
        except ImportError:
            wechat_image = None

        image_keys = None
        if wechat_image and account_dir:
            try:
                image_keys = wechat_image.resolve_keys(
                    account_dir,
                    wxid,
                    templates=None,
                    allow_sudo=bool(img_cfg.get("allow_sudo_memscan", False)),
                    cache_path=vault / "state" / "image_keys.json",
                )
            except Exception as e:  # noqa: BLE001
                log(f"[WARN] resolve_keys 失败: {type(e).__name__}: {e}")

        now_ts = time.time()
        for chat, lid, ct, sender, img_info in iter_image_messages(decrypted, allow_md5, since):
            if processed >= max_items:
                log("[INFO] 达到本轮 max_items 上限，收工。")
                break
            if daily["parsed"] >= budget:
                log("[INFO] 今日预算用尽，收工。")
                break

            key = f"{chat}|{lid}"
            raw_md5 = img_info.get("md5", "")
            if not raw_md5:
                mark(key, None, ct)
                summary["skipped"] += 1
                continue

            asset_id = clean_asset_id(raw_md5)
            chat_name = resolve_chat_name(chat, vault)
            anchor = {
                "chat_id": chat,
                "chat_name": chat_name,
                "local_id": lid,
                "ts": ct,
                "sender": sender,
            }

            existing = ledger["assets"].get(asset_id)
            if existing and existing.get("state") in (STATE_READY, STATE_SKIPPED, STATE_FAILED):
                if existing.get("state") == STATE_READY and "text_head" not in existing:
                    ocr_info = existing.get("ocr") or {}
                    if ocr_info.get("status") == "ready" and ocr_info.get("text"):
                        raw_ocr = str(ocr_info["text"]).strip()
                        existing["text_head"] = (raw_ocr[:2000] + "…") if len(raw_ocr) > 2000 else raw_ocr
                    else:
                        existing["text_head"] = ""
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["skipped"] += 1
                continue

            if existing and existing.get("state") == STATE_PENDING:
                attempts = int(existing.get("attempts") or 0)
                if attempts >= img_max_retries:
                    existing["state"] = STATE_FAILED
                    existing["deny_reason"] = "retries_exhausted"
                    mark(key, asset_id, ct)
                    ledger.setdefault("anchors", {})[key] = anchor
                    summary["failed"] += 1
                    continue

            hl_entry = hl_images.get(asset_id) or hl_images.get(raw_md5)
            month = None
            disk_asset_id = asset_id
            if hl_entry:
                disk_asset_id = clean_asset_id(hl_entry[0])
                month = dir2id_img.get(hl_entry[1]) or dir2id_img.get(hl_entry[2])

            chat_hash = hashlib.md5(chat.encode("utf-8")).hexdigest()
            candidates, how = find_image_dat(attach_root, cache_root, chat_hash, month, disk_asset_id)
            if not candidates and disk_asset_id != asset_id:
                candidates, how = find_image_dat(attach_root, cache_root, chat_hash, month, asset_id)
            asset_id = disk_asset_id

            existing = ledger["assets"].get(asset_id)
            if existing and existing.get("state") in (STATE_READY, STATE_SKIPPED, STATE_FAILED):
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["skipped"] += 1
                continue
            if not candidates:
                if how.startswith("unresolved:ambiguous"):
                    rec = {
                        "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                        "deny_reason": how, "anchor": anchor, "create_time": ct,
                    }
                    ledger["assets"][asset_id] = rec
                    mark(key, asset_id, ct)
                    ledger.setdefault("anchors", {})[key] = anchor
                    summary["unresolved"] += 1
                    continue

                if now_ts - ct < img_pending_window:
                    prev_att = int((existing or {}).get("attempts") or 0)
                    rec = {
                        "asset_id": asset_id, "kind": "image", "state": STATE_PENDING,
                        "deny_reason": "image_not_on_disk", "attempts": prev_att + 1,
                        "anchor": anchor, "create_time": ct,
                    }
                    ledger["assets"][asset_id] = rec
                    ledger["by_message"].setdefault(key, asset_id)
                    ledger.setdefault("anchors", {})[key] = anchor
                    continue
                else:
                    rec = {
                        "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                        "deny_reason": "missing_source", "anchor": anchor, "create_time": ct,
                    }
                    ledger["assets"][asset_id] = rec
                    mark(key, asset_id, ct)
                    ledger.setdefault("anchors", {})[key] = anchor
                    summary["skipped"] += 1
                    continue

            ranked = rank_image_variants(candidates)
            best_dat = ranked[0]
            quality = quality_of_path(best_dat)
            variant = variant_of_path(best_dat)
            rel_dat_str = (
                str(best_dat.relative_to(account_dir))
                if (account_dir and best_dat.is_relative_to(account_dir))
                else str(best_dat)
            )

            if args.dry_run:
                log(f"[DRY] Image {chat} lid={lid} {asset_id} {best_dat.name} ({how}, {quality})")
                processed += 1
                continue

            if not wechat_image:
                rec = {
                    "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                    "deny_reason": "skipped:decryptor_unavailable", "anchor": anchor, "create_time": ct,
                }
                ledger["assets"][asset_id] = rec
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["skipped"] += 1
                continue

            if not image_keys:
                rec = {
                    "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                    "deny_reason": "missing_keys", "anchor": anchor, "create_time": ct,
                }
                ledger["assets"][asset_id] = rec
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["skipped"] += 1
                continue

            dec = wechat_image.decrypt_dat(best_dat, image_keys)
            if not dec or not dec.data:
                prev_att = int((existing or {}).get("attempts") or 0)
                rec = {
                    "asset_id": asset_id, "kind": "image", "state": STATE_FAILED,
                    "deny_reason": "decrypt_failed", "attempts": prev_att + 1,
                    "anchor": anchor, "create_time": ct,
                }
                ledger["assets"][asset_id] = rec
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["failed"] += 1
                continue

            width = dec.width
            height = dec.height
            if width and height and (width * height > max_pixels):
                rec = {
                    "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                    "deny_reason": "max_pixels_exceeded", "anchor": anchor, "create_time": ct,
                }
                ledger["assets"][asset_id] = rec
                mark(key, asset_id, ct)
                ledger.setdefault("anchors", {})[key] = anchor
                summary["skipped"] += 1
                continue

            final_data = dec.data
            final_fmt = (dec.fmt or "jpg").lower()
            if final_fmt == "wxgf":
                staging_png = staging_root / f"{asset_id}.png"
                staging_png.parent.mkdir(parents=True, exist_ok=True)
                try:
                    ok, is_anim = wechat_image.convert_wxgf(dec.data, staging_png, animated=wxgf_animated)
                    if is_anim and wxgf_animated == "skip":
                        rec = {
                            "asset_id": asset_id, "kind": "image", "state": STATE_SKIPPED,
                            "deny_reason": "skipped:animated", "anchor": anchor, "create_time": ct,
                        }
                        ledger["assets"][asset_id] = rec
                        mark(key, asset_id, ct)
                        ledger.setdefault("anchors", {})[key] = anchor
                        summary["skipped"] += 1
                        continue
                    if not ok or not staging_png.is_file():
                        rec = {
                            "asset_id": asset_id, "kind": "image", "state": STATE_FAILED,
                            "deny_reason": "wxgf_convert_failed", "anchor": anchor, "create_time": ct,
                        }
                        ledger["assets"][asset_id] = rec
                        mark(key, asset_id, ct)
                        ledger.setdefault("anchors", {})[key] = anchor
                        summary["failed"] += 1
                        continue
                    final_data = staging_png.read_bytes()
                    final_fmt = "png"
                finally:
                    if staging_png.exists():
                        staging_png.unlink()

            sha256_hex = hashlib.sha256(final_data).hexdigest()
            ocr_record = {
                "status": "skipped",
                "text": "",
                "provider": "none",
                "lang": "",
                "confidence": None,
                "version": 1,
                "sha256": sha256_hex,
            }
            if ocr_enabled:
                try:
                    import wechat_ocr
                except ImportError:
                    wechat_ocr = None

                if not wechat_ocr:
                    ocr_record["status"] = "unavailable"
                else:
                    try:
                        ores = wechat_ocr.ocr_image(final_data, providers=ocr_providers, timeout=ocr_timeout)
                        ocr_record = {
                            "status": ores.status,
                            "text": ores.text,
                            "provider": ores.provider,
                            "lang": ores.lang,
                            "confidence": ores.confidence,
                            "version": 1,
                            "sha256": sha256_hex,
                        }
                    except Exception as e:  # noqa: BLE001
                        ocr_record = {
                            "status": "failed",
                            "text": "",
                            "provider": "none",
                            "lang": "",
                            "confidence": None,
                            "version": 1,
                            "sha256": sha256_hex,
                            "error": str(e)[:200],
                        }

            date_dir = out_root / today / asset_id
            date_dir.mkdir(parents=True, exist_ok=True)
            export_file = date_dir / f"image.{final_fmt}"
            export_file.write_bytes(final_data)

            vault_media_dir = vault / "media" / asset_id
            vault_media_dir.mkdir(parents=True, exist_ok=True)
            vault_media_file = vault_media_dir / f"image.{final_fmt}"
            if not vault_media_file.exists():
                try:
                    os.link(export_file, vault_media_file)
                except OSError:
                    shutil.copy2(export_file, vault_media_file)
            if ocr_record.get("status") == "ready" and ocr_record.get("text"):
                raw_ocr = str(ocr_record["text"]).strip()
                text_head = (raw_ocr[:2000] + "…") if len(raw_ocr) > 2000 else raw_ocr
            else:
                text_head = ""

            rec = {
                "asset_id": asset_id,
                "kind": "image",
                "quality": getattr(dec, "quality", None) or quality,
                "format": final_fmt,
                "width": width,
                "height": height,
                "bytes": len(final_data),
                "sha256": sha256_hex,
                "text_head": text_head,
                "path": f"media/{asset_id}/image.{final_fmt}",
                "url": f"/api/wechat/media/{asset_id}/image.{final_fmt}",
                "anchor": anchor,
                "source": {"dat": rel_dat_str, "variant": variant},
                "ocr": ocr_record,
                "state": STATE_READY,
                "parsed_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            }
            ledger["assets"][asset_id] = rec
            mark(key, asset_id, ct)
            ledger.setdefault("anchors", {})[key] = anchor
            daily["parsed"] += 1
            daily["bytes"] += len(final_data)
            summary["ready"] += 1
            processed += 1
            log(f"[OK] Image {chat} lid={lid} {asset_id} -> {export_file} "
                f"({len(final_data)}B, ocr={ocr_record['status']})")

    ledger["watermark"] = max(int(ledger.get("watermark") or 0), settled_max)
    ledger["updated_at"] = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    if not args.dry_run:
        save_json_atomic(ledger_path, ledger)
    log(f"[DONE] {summary} daily={daily} watermark={ledger['watermark']}")
    lock_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
