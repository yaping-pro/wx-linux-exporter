#!/usr/bin/env python3
"""WeChat Export API - Standalone HTTP service for exporting WeChat message archives.

Conforms to the Muse WeChat Export specification.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Ensure local scripts dir is on sys.path
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import wx_contract  # noqa: E402
try:
    from wechat_shards import message_db_paths
except ImportError:
    SHARD_RE = re.compile(r"^(?:message|biz_message)_\d+\.db$")

    def message_db_paths(decrypted_dir: Path) -> list[Path]:
        d = Path(decrypted_dir) / "message"
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and SHARD_RE.match(p.name))

try:
    import zstandard
except ImportError:
    zstandard = None

# Constants
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SHANGHAI_TZ = timezone(timedelta(hours=8))

# 家服巡检简报快照：由 /home/steven/.agents/scripts/generate-report.py 每日
# 09:00 生成简报时原子写入，本服务只读转发（不触发采集、不修改简报）。
DEFAULT_BRIEFING_PATH = os.environ.get(
    "HOMELAB_BRIEFING_PATH", "/var/lib/homelab-inspector/briefing.json"
)

# ── 媒体富化（可选，默认关闭）──────────────────────────────────────────────────
# 设计原则：不新增响应字段、不新增端点、不改任何既有键名。
# 富化结果只做两件事：
#   1) 把已解析文本注入既有 `text`（图片/语音原本为空 → 填入；文件原本是 appmsg XML → 追加）
#   2) 未就绪的媒体消息暂不推进 watermark，避免下游永久错过内容
# 详见 docs/MEDIA_ENRICHMENT.md 与 docs/DATA_CONTRACT.md（v1.1 语义变更）。
MEDIA_VAULT_ROOT = Path(
    os.environ.get("WECHAT_VAULT_ROOT", str(Path.home() / ".local/share" / "wechat-vault"))
).expanduser()
MEDIA_POLICY_PATH = Path(
    os.environ.get("WECHAT_MEDIA_POLICY", str(MEDIA_VAULT_ROOT / "config" / "media_policy.json"))
).expanduser()
MEDIA_LEDGER_PATH = Path(
    os.environ.get("WECHAT_MEDIA_LEDGER", str(MEDIA_VAULT_ROOT / "state" / "media_ledger.json"))
).expanduser()
_MEDIA_CACHE: dict[str, tuple[float, dict]] = {}
ALLOWED_MEDIA_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov"}
ALLOWED_IMAGE_EXTS = ALLOWED_MEDIA_EXTS

def get_media_paths(server_context: dict[str, Any] | None = None) -> tuple[Path, Path, Path]:
    """解析 (media_vault_root, media_policy_path, media_ledger_path)。"""
    ctx = server_context or {}
    if ctx.get("media_vault_root"):
        vroot = Path(ctx["media_vault_root"]).expanduser()
        pol = Path(ctx.get("media_policy_path") or (vroot / "config" / "media_policy.json"))
        led = Path(ctx.get("media_ledger_path") or (vroot / "state" / "media_ledger.json"))
        return vroot, pol, led
    if ctx.get("media_policy_path") or ctx.get("media_ledger_path"):
        vroot = Path(ctx.get("vault_dir", MEDIA_VAULT_ROOT)).expanduser()
        pol = Path(ctx.get("media_policy_path") or (vroot / "config" / "media_policy.json"))
        led = Path(ctx.get("media_ledger_path") or (vroot / "state" / "media_ledger.json"))
        return vroot, pol, led

    vdir = Path(ctx.get("vault_dir", MEDIA_VAULT_ROOT)).expanduser()
    if (vdir / "state" / "media_ledger.json").exists():
        return vdir, vdir / "config" / "media_policy.json", vdir / "state" / "media_ledger.json"
    if (vdir.parent / "state" / "media_ledger.json").exists():
        return vdir.parent, vdir.parent / "config" / "media_policy.json", vdir.parent / "state" / "media_ledger.json"
    if (vdir.parent.parent / "state" / "media_ledger.json").exists():
        return (
            vdir.parent.parent,
            vdir.parent.parent / "config" / "media_policy.json",
            vdir.parent.parent / "state" / "media_ledger.json",
        )

    return MEDIA_VAULT_ROOT, MEDIA_POLICY_PATH, MEDIA_LEDGER_PATH


def load_json_cached(path: Path) -> dict[str, Any]:
    """按 mtime 缓存的 JSON 读取：既避免每请求 IO，又保证改策略后即时生效。"""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = _MEDIA_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _MEDIA_CACHE[key] = (st.st_mtime, data)
    return data


def load_media_context(server_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """汇总媒体注入所需的策略与账本上下文；任何异常都退化为「不注入、不挂水位」。"""
    _vroot, policy_path, ledger_path = get_media_paths(server_context)
    policy = load_json_cached(policy_path)
    render = policy.get("render") or {}
    readiness = policy.get("readiness") or {}
    kinds = policy.get("kinds") or {}
    image_cfg = policy.get("image") or {}
    enabled = bool(policy.get("enabled"))
    if not enabled:
        return {"enabled": False}
    ledger = load_json_cached(ledger_path)
    return {
        "enabled": True,
        "assets": ledger.get("assets") or {},
        "index": ledger.get("by_message") or {},
        "anchors": ledger.get("anchors") or {},
        "allow_chats": {c for c in (policy.get("chat_allowlist") or []) if c},
        "allow_ext": {str(e).lower().lstrip(".") for e in (policy.get("file_ext_allow") or [])},
        "skip_ext": {str(e).lower().lstrip(".") for e in (policy.get("skip_ext") or [])},
        "max_chars": int(render.get("max_chars_per_doc") or 4000),
        "inject_into_text": bool(render.get("inject_into_text")),
        "pending_window": int(readiness.get("max_pending_age_s") or 1800),
        "max_bytes": int(policy.get("max_bytes") or 0),
        "voice": bool(kinds.get("voice")),
        "image": bool(kinds.get("image") or image_cfg.get("enabled")),
    }


def format_rfc3339(epoch_sec: float | int) -> str:
    """Format an epoch timestamp in seconds to RFC3339 string in Asia/Shanghai timezone."""
    dt = datetime.fromtimestamp(epoch_sec, tz=SHANGHAI_TZ)
    return dt.isoformat(timespec="seconds")


def parse_time_param(val: str | None, default_epoch: float) -> float:
    """Parse time string (epoch or RFC3339/ISO8601) to epoch seconds."""
    if not val:
        return default_epoch
    val = val.strip()
    try:
        return float(val)
    except ValueError:
        pass
    val_fixed = val.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(val_fixed)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.timestamp()
    except Exception as e:
        raise ValueError(f"Invalid time format: {val!r}") from e


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize object to canonical UTF-8 JSON bytes (sorted keys, compact separators)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_cursor(ts: int, chat_id: str, local_id: int) -> str:
    """Encodes cursor tuple to base64url string without padding."""
    payload = {"ts": int(ts), "chat_id": str(chat_id), "local_id": int(local_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_cursor(cursor_str: str) -> tuple[int, str, int]:
    """Parses base64url encoded cursor string into (ts, chat_id, local_id).

    Raises ValueError if invalid.
    """
    if not isinstance(cursor_str, str) or not cursor_str.strip():
        raise ValueError("Cursor must be a non-empty string")
    s = cursor_str.strip()
    rem = len(s) % 4
    if rem > 0:
        s += "=" * (4 - rem)
    try:
        raw = base64.urlsafe_b64decode(s.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Malformed base64url cursor: {cursor_str!r}") from e

    if not isinstance(obj, dict):
        raise ValueError("Cursor payload must be a JSON object")
    for k in ("ts", "chat_id", "local_id"):
        if k not in obj:
            raise ValueError(f"Cursor payload missing required key: {k!r}")

    ts_val = obj["ts"]
    chat_id_val = obj["chat_id"]
    lid_val = obj["local_id"]

    if not isinstance(ts_val, int) or isinstance(ts_val, bool):
        raise ValueError("Cursor 'ts' must be an integer")
    if not isinstance(chat_id_val, str) or not chat_id_val:
        raise ValueError("Cursor 'chat_id' must be a non-empty string")
    if not isinstance(lid_val, int) or isinstance(lid_val, bool):
        raise ValueError("Cursor 'local_id' must be an integer")

    return int(ts_val), str(chat_id_val), int(lid_val)


def decode_content(message_content: Any, compress_content: Any) -> str:
    """Decode message text from message_content or compress_content, handling zstd decompression."""
    raw = (
        compress_content
        if (compress_content is not None and compress_content != "" and compress_content != b"")
        else message_content
    )
    if raw is None:
        return ""

    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
        if raw_bytes.startswith(ZSTD_MAGIC):
            if zstandard is not None:
                try:
                    decomp = zstandard.ZstdDecompressor().decompress(raw_bytes)
                    return decomp.decode("utf-8", errors="replace")
                except Exception:
                    pass
        return raw_bytes.decode("utf-8", errors="replace")

    if isinstance(raw, str):
        return raw

    return str(raw)


def map_message_type(local_type: int, raw_text: str) -> tuple[str, str, str]:
    """Maps local_type to (kind, text, note) per Muse spec.

    Kinds: "text" | "image" | "voice" | "video" | "link" | "file" | "other"
    """
    base_type = local_type & 0xFFFFFFFF if local_type is not None else 0

    if base_type == 1:
        return "text", raw_text, ""
    elif base_type == 3:
        return "image", "", "图片"
    elif base_type == 34:
        return "voice", "", "语音"
    elif base_type == 43:
        return "video", "", "视频"
    elif base_type == 49:
        kind = "file" if ("<type>6</type>" in raw_text or "<type>74</type>" in raw_text) else "link"
        return kind, raw_text, "卡片/链接/文件"
    elif base_type == 47:
        return "other", "", "动画表情"
    elif base_type == 48:
        return "other", raw_text, "位置"
    elif base_type == 10000:
        return "other", raw_text, "系统消息"
    else:
        return "other", raw_text, "其他"


def media_note(asset: dict[str, Any] | None, kind: str = "image") -> str:
    """向后兼容转发至 wx_contract.media_note。"""
    return wx_contract.media_note(asset, kind=kind)


def image_note(asset: dict[str, Any] | None) -> str:
    """向后兼容的 image_note 别名。"""
    return wx_contract.media_note(asset, kind="image")

def load_contacts(vault_dir: Path) -> dict[str, dict[str, Any]]:
    """Loads contacts from contact/contact.db."""
    contacts: dict[str, dict[str, Any]] = {}
    contact_path = vault_dir / "contact" / "contact.db"
    if not contact_path.exists():
        return contacts

    uri = f"{contact_path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
    except Exception:
        con = sqlite3.connect(str(contact_path))

    try:
        cursor = con.execute("PRAGMA table_info(contact)")
        cols = {r[1].lower(): r[1] for r in cursor.fetchall()}

        u_col = cols.get("username", cols.get("username", "username"))
        r_col = cols.get("remark", "remark")
        n_col = cols.get("nick_name", cols.get("nickname", "nick_name"))
        a_col = cols.get("alias", "alias")
        t_col = cols.get("local_type", cols.get("type", "local_type"))
        crt_col = cols.get("chat_room_type", "chat_room_type")

        query = f"SELECT {u_col}, {r_col}, {n_col}, {a_col}, {t_col}, {crt_col} FROM contact"
        for row in con.execute(query):
            username = str(row[0] or "")
            if not username:
                continue
            remark = str(row[1] or "").strip()
            nick_name = str(row[2] or "").strip()
            alias = str(row[3] or "").strip()
            chat_room_type = row[5] or 0

            display_name = remark or nick_name or alias or username
            is_group = username.endswith("@chatroom") or chat_room_type != 0

            contacts[username] = {
                "username": username,
                "remark": remark,
                "nick_name": nick_name,
                "alias": alias,
                "display_name": display_name,
                "is_group": is_group,
            }
    finally:
        con.close()

    return contacts


_CONTACTS_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def get_cached_contacts(vault_dir: Path) -> dict[str, dict[str, Any]]:
    """按 mtime 缓存的联系人与会话列表读取。"""
    c_path = vault_dir / "contact" / "contact.db"
    s_path = vault_dir / "session" / "session.db"
    mtime = 0.0
    for p in (c_path, s_path):
        if p.exists():
            try:
                mtime = max(mtime, p.stat().st_mtime)
            except OSError:
                pass
    key = str(vault_dir.resolve())
    hit = _CONTACTS_CACHE.get(key)
    if hit is not None and hit[0] == mtime and mtime > 0:
        return hit[1]

    contacts = load_contacts(vault_dir)
    if s_path.exists():
        try:
            uri = f"{s_path.resolve().as_uri()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
        except Exception:
            con = sqlite3.connect(str(s_path))
        try:
            cursor = con.execute("PRAGMA table_info(SessionTable)")
            cols = {r[1].lower(): r[1] for r in cursor.fetchall()}
            if "username" in cols:
                u_col = cols["username"]
                d_col = (
                    cols.get("display_name")
                    or cols.get("remark")
                    or cols.get("nick_name")
                    or cols.get("last_sender_display_name")
                )
                if d_col:
                    for row in con.execute(f"SELECT {u_col}, {d_col} FROM SessionTable"):
                        u = str(row[0] or "")
                        d = str(row[1] or "").strip()
                        if u and d and u not in contacts:
                            contacts[u] = {
                                "username": u,
                                "display_name": d,
                                "is_group": u.endswith("@chatroom"),
                            }
        except Exception:
            pass
        finally:
            con.close()

    _CONTACTS_CACHE[key] = (mtime, contacts)
    return contacts


def resolve_chat_name(
    chat_id: str,
    vault_dir: Path,
    contacts: dict[str, Any] | None = None,
) -> str:
    """解析会话可读显示名：优先 contact.db 备注/群名，次选 session.db，取不到回退 chat_id。"""
    if contacts is None:
        contacts = get_cached_contacts(vault_dir)
    cinfo = contacts.get(chat_id)
    if cinfo and cinfo.get("display_name"):
        return str(cinfo["display_name"])
    return chat_id


def fetch_message_context(
    vault_dir: Path,
    chat_id: str,
    target_local_id: int,
    target_ts: int,
    contacts: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """在同一 chat_id 内按 local_id/ts 获取紧邻的前后最多各 2 条消息。"""
    if contacts is None:
        contacts = get_cached_contacts(vault_dir)

    table_md5 = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
    table_name = f"Msg_{table_md5}"
    shards = message_db_paths(vault_dir)

    target_shard = None
    for shard in shards:
        try:
            uri = f"{shard.resolve().as_uri()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
        except Exception:
            con = sqlite3.connect(str(shard))
        try:
            cur = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            if cur.fetchone():
                target_shard = shard
                break
        finally:
            con.close()

    if not target_shard:
        return [], []

    n2id: dict[int, str] = {}
    try:
        uri = f"{target_shard.resolve().as_uri()}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
    except Exception:
        con = sqlite3.connect(str(target_shard))

    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Name2Id'")
        if cur.fetchone():
            for r in con.execute("SELECT rowid, user_name FROM Name2Id"):
                if r[1]:
                    n2id[int(r[0])] = str(r[1])

        is_group = chat_id.endswith("@chatroom")
        chat_info = contacts.get(chat_id)
        chat_display_name = chat_info.get("display_name", chat_id) if chat_info else chat_id

        def _row_to_context_item(row: tuple) -> dict[str, Any]:
            _lid, _srv_id, ltype, rsid, ct, mc, cc = row
            decoded = decode_content(mc, cc)
            _kind, text, _note = map_message_type(ltype, decoded)

            sender_display = "我"
            if is_group:
                match_sender = re.match(r"^([a-zA-Z0-9_\-]+):\n(.*)$", text, re.DOTALL)
                if match_sender:
                    sender_user = match_sender.group(1)
                    text = match_sender.group(2)
                    s_cinfo = contacts.get(sender_user)
                    sender_display = (
                        s_cinfo.get("display_name", sender_user)
                        if s_cinfo
                        else sender_user
                    )
                elif rsid and int(rsid) in n2id:
                    sender_user = n2id[int(rsid)]
                    s_cinfo = contacts.get(sender_user)
                    sender_display = (
                        s_cinfo.get("display_name", sender_user)
                        if s_cinfo
                        else sender_user
                    )
            else:
                if rsid and int(rsid) in n2id:
                    sender_user = n2id[int(rsid)]
                    if sender_user == chat_id:
                        sender_display = chat_display_name
                    else:
                        s_cinfo = contacts.get(sender_user)
                        sender_display = (
                            s_cinfo.get("display_name", sender_user)
                            if s_cinfo
                            else sender_user
                        )
                elif rsid == 0:
                    sender_display = "我"
                else:
                    sender_display = chat_display_name

            if len(text) > 200:
                text = text[:200] + "…"

            return {
                "ts": format_rfc3339(ct),
                "sender": sender_display,
                "text": text,
            }

        q_before = (
            f"SELECT local_id, server_id, local_type, real_sender_id, "
            f"create_time, message_content, compress_content FROM [{table_name}] "
            f"WHERE (create_time < ? OR (create_time = ? AND local_id < ?)) "
            f"ORDER BY create_time DESC, local_id DESC LIMIT 2"
        )
        before_rows = con.execute(q_before, (target_ts, target_ts, target_local_id)).fetchall()
        before = [_row_to_context_item(r) for r in reversed(before_rows)]

        q_after = (
            f"SELECT local_id, server_id, local_type, real_sender_id, "
            f"create_time, message_content, compress_content FROM [{table_name}] "
            f"WHERE (create_time > ? OR (create_time = ? AND local_id > ?)) "
            f"ORDER BY create_time ASC, local_id ASC LIMIT 2"
        )
        after_rows = con.execute(q_after, (target_ts, target_ts, target_local_id)).fetchall()
        after = [_row_to_context_item(r) for r in after_rows]

        return before, after
    finally:
        con.close()


def is_chat_allowed(
    chat_id: str,
    contact_info: dict[str, Any] | None,
    allowlist: list[str] | None,
    blocklist: list[str] | None,
) -> bool:
    """Checks if a chat is allowed according to allowlist and blocklist."""
    names: set[str] = {chat_id}
    if contact_info:
        for k in ("display_name", "remark", "nick_name", "alias", "username"):
            val = contact_info.get(k)
            if val:
                names.add(str(val))

    if blocklist:
        block_set = set(blocklist)
        if any(n in block_set for n in names):
            return False

    if allowlist:
        allow_set = set(allowlist)
        if not any(n in allow_set for n in names):
            return False

    return True


def export_wechat_data(
    vault_dir: Path,
    since_epoch: float,
    to_epoch: float,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    server_context: dict[str, Any] | None = None,
    media_filter: str = "all",
    media_chats: set[str] | None = None,
    ocr_mode: str = "head",
    limit: int = 0,
    cursor_tuple: tuple[int, str, int] | None = None,
) -> dict[str, Any]:
    """Exports decrypted WeChat messages within the specified time range."""
    contacts = get_cached_contacts(vault_dir)
    now_epoch = time.time()
    media = load_media_context(server_context)
    hold_epoch: float | None = None

    # Build md5 -> username map from contacts
    md5_to_user: dict[str, str] = {}
    for u in contacts:
        m = hashlib.md5(u.encode("utf-8")).hexdigest()
        md5_to_user[m] = u

    shards = message_db_paths(vault_dir)
    # Also collect Name2Id from shards to enrich md5_to_user and sender name resolution
    name2id_by_shard: dict[Path, dict[int, str]] = {}

    for shard in shards:
        try:
            uri = f"{shard.resolve().as_uri()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
        except Exception:
            con = sqlite3.connect(str(shard))

        try:
            cursor = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Name2Id'")
            if cursor.fetchone():
                n2id: dict[int, str] = {}
                for r in con.execute("SELECT rowid, user_name FROM Name2Id"):
                    rid = int(r[0])
                    uname = str(r[1] or "")
                    if uname:
                        n2id[rid] = uname
                        m = hashlib.md5(uname.encode("utf-8")).hexdigest()
                        md5_to_user[m] = uname
                name2id_by_shard[shard] = n2id
            else:
                name2id_by_shard[shard] = {}
        finally:
            con.close()

    # Query message tables from each shard
    # Collect raw candidate messages across all allowed chats
    all_candidates: list[dict[str, Any]] = []
    chat_meta: dict[str, dict[str, Any]] = {}

    for shard in shards:
        n2id = name2id_by_shard.get(shard, {})
        try:
            uri = f"{shard.resolve().as_uri()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True)
        except Exception:
            con = sqlite3.connect(str(shard))

        try:
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
            ]

            for table in tables:
                table_md5 = table[4:]  # Msg_<md5>
                chat_id = md5_to_user.get(table_md5) or table
                contact_info = contacts.get(chat_id)

                if not is_chat_allowed(chat_id, contact_info, allowlist, blocklist):
                    continue

                is_group = contact_info.get("is_group", False) if contact_info else chat_id.endswith("@chatroom")
                chat_display_name = (
                    contact_info.get("display_name", chat_id) if contact_info else chat_id
                )
                chat_kind = "group" if is_group else "single"

                query = (
                    f"SELECT local_id, server_id, local_type, real_sender_id, "
                    f"create_time, message_content, compress_content FROM [{table}] "
                    f"WHERE create_time >= ? AND create_time <= ? ORDER BY create_time"
                )
                rows = con.execute(query, (int(since_epoch), int(to_epoch))).fetchall()
                if not rows:
                    continue

                if chat_id not in chat_meta:
                    chat_meta[chat_id] = {
                        "name": chat_display_name,
                        "kind": chat_kind,
                    }

                for row in rows:
                    local_id, server_id, local_type, real_sender_id, create_time, msg_content, comp_content = row
                    decoded = decode_content(msg_content, comp_content)
                    kind, text, note = map_message_type(local_type, decoded)

                    # Sender resolution
                    sender_display = ""
                    if is_group:
                        # Group message: check if content starts with username:\n
                        match_sender = re.match(r"^([a-zA-Z0-9_\-]+):\n(.*)$", text, re.DOTALL)
                        if match_sender:
                            sender_user = match_sender.group(1)
                            text = match_sender.group(2)
                            sender_cinfo = contacts.get(sender_user)
                            sender_display = sender_cinfo.get("display_name", sender_user) if sender_cinfo else sender_user
                        elif real_sender_id and real_sender_id in n2id:
                            sender_user = n2id[real_sender_id]
                            sender_cinfo = contacts.get(sender_user)
                            sender_display = sender_cinfo.get("display_name", sender_user) if sender_cinfo else sender_user
                        else:
                            sender_display = "我"
                    else:
                        # Single chat
                        if real_sender_id and real_sender_id in n2id:
                            sender_user = n2id[real_sender_id]
                            if sender_user == chat_id:
                                sender_display = chat_display_name
                            else:
                                sender_cinfo = contacts.get(sender_user)
                                sender_display = sender_cinfo.get("display_name", sender_user) if sender_cinfo else sender_user
                        elif real_sender_id == 0:
                            sender_display = "我"
                        else:
                            sender_display = chat_display_name

                    if not sender_display:
                        sender_display = "我"
                    # ── 媒体注入 / 水位保持 ─────────────────────────────
                    media_obj = None
                    if media.get("enabled"):
                        mkey = f"{chat_id}|{local_id}"
                        asset = media["assets"].get(media["index"].get(mkey) or "")
                        chat_in_media_filter = (
                            media_chats is None or chat_id in media_chats
                        )
                        if (kind == "image" or kind == "video") and chat_id in media["allow_chats"]:
                            note = media_note(asset, kind=kind)

                        allow_kind_media = (
                            (media_filter == "all" and kind in ("image", "video"))
                            or (media_filter == "image" and kind == "image")
                            or (media_filter == "video" and kind == "video")
                        )

                        if (
                            asset
                            and asset.get("state") == "ready"
                            and chat_id in media["allow_chats"]
                            and chat_in_media_filter
                            and allow_kind_media
                        ):
                            anchor = dict(
                                media.get("anchors", {}).get(mkey)
                                or asset.get("anchor")
                                or {}
                            )
                            anchor["chat_id"] = chat_id
                            anchor["chat_name"] = chat_display_name
                            anchor["local_id"] = int(local_id)
                            anchor["ts"] = int(create_time)
                            anchor["sender"] = sender_display

                            asset_kind = asset.get("kind")
                            if asset_kind in ("image", "video"):
                                media_obj = wx_contract.build_media_object(
                                    asset=asset,
                                    kind=asset_kind,
                                    anchor=anchor,
                                    ocr_mode=ocr_mode,
                                )
                                if asset_kind == "image":
                                    if ocr_mode == "none":
                                        text = ""
                                    else:
                                        text = wx_contract.ocr_projection(asset, max_chars=2000)
                            else:
                                if media.get("inject_into_text"):
                                    head = str(asset.get("text_head") or "")[: media["max_chars"]]
                                    if head:
                                        text = head if not text else f"{text}\n\n{head}"
                        elif kind == "file" and chat_id in media["allow_chats"]:
                            mm = re.search(r"<md5>([0-9a-f]{32})</md5>", decoded or "")
                            if mm:
                                settled = (
                                    media["assets"].get(mm.group(1).lower()) or {}
                                ).get("state") in ("ready", "skipped", "failed")
                                if not settled:
                                    mext = re.search(r"<fileext>(.*?)</fileext>", decoded or "")
                                    ext = re.sub(
                                        r"[^a-z0-9]", "", (mext.group(1) if mext else "").lower()
                                    )
                                    mlen = re.search(r"<totallen>(\d+)</totallen>", decoded or "")
                                    size = int(mlen.group(1)) if mlen else 0
                                    parseable = (
                                        (not media["skip_ext"] or ext not in media["skip_ext"])
                                        and (not media["allow_ext"] or ext in media["allow_ext"])
                                        and (not media["max_bytes"] or not size
                                             or size <= media["max_bytes"])
                                    )
                                    if (parseable
                                            and create_time > now_epoch - media["pending_window"]):
                                        if hold_epoch is None or create_time < hold_epoch:
                                            hold_epoch = create_time
                        elif (kind == "voice" and media.get("voice")
                                and chat_id in media["allow_chats"]):
                            # 语音转写异步到达：未就绪且仍在等待窗口内 → 暂不推进水位
                            if not (asset and asset.get("state") == "ready"):
                                if create_time > now_epoch - media["pending_window"]:
                                    if hold_epoch is None or create_time < hold_epoch:
                                        hold_epoch = create_time
                        elif (kind == "image" and media.get("image")
                                and chat_id in media["allow_chats"]):
                            settled = (
                                asset and asset.get("state") in ("ready", "skipped", "failed")
                            )
                            if not settled:
                                if create_time > now_epoch - media["pending_window"]:
                                    if hold_epoch is None or create_time < hold_epoch:
                                        hold_epoch = create_time
                        elif (kind == "video" and media.get("video")
                                and chat_id in media["allow_chats"]):
                            settled = (
                                asset and asset.get("state") in ("ready", "skipped", "failed")
                            )
                            if not settled:
                                if create_time > now_epoch - media["pending_window"]:
                                    if hold_epoch is None or create_time < hold_epoch:
                                        hold_epoch = create_time
                    ts_str = format_rfc3339(create_time)

                    msg_dict = {
                        "ts": ts_str,
                        "sender": sender_display,
                        "kind": kind,
                        "text": text,
                        "note": note,
                    }
                    if media_obj is not None:
                        msg_dict["media"] = media_obj
                    all_candidates.append({
                        "ts": int(create_time),
                        "chat_id": chat_id,
                        "local_id": int(local_id),
                        "msg_dict": msg_dict,
                    })
        finally:
            con.close()

    # Sort all candidates strictly by (ts, chat_id, local_id) ascending
    all_candidates.sort(key=lambda x: (x["ts"], x["chat_id"], x["local_id"]))

    # Apply cursor filtering: skip elements <= cursor
    if cursor_tuple is not None:
        c_ts, c_chat, c_lid = cursor_tuple
        all_candidates = [
            c for c in all_candidates
            if (c["ts"], c["chat_id"], c["local_id"]) > (c_ts, c_chat, c_lid)
        ]

    # Apply limit pagination
    truncated = False
    next_cursor: str | None = None
    if limit > 0 and len(all_candidates) > limit:
        truncated = True
        served_candidates = all_candidates[:limit]
        last_served = served_candidates[-1]
        next_cursor = encode_cursor(last_served["ts"], last_served["chat_id"], last_served["local_id"])
    else:
        served_candidates = all_candidates
        next_cursor = None

    # Group served messages into chats_list
    chats_data: dict[str, dict[str, Any]] = {}
    for item in served_candidates:
        cid = item["chat_id"]
        if cid not in chats_data:
            meta = chat_meta.get(cid, {"name": cid, "kind": "single"})
            chats_data[cid] = {
                "chat_id": cid,
                "name": meta["name"],
                "kind": meta["kind"],
                "messages": [],
            }
        chats_data[cid]["messages"].append(item["msg_dict"])

    # Sort messages within each chat by ts (and keep chat order sorted by chat_id)
    chats_list: list[dict[str, Any]] = []
    for chat_id in sorted(chats_data.keys()):
        chat_item = chats_data[chat_id]
        chat_item["messages"].sort(key=lambda m: m["ts"])
        chats_list.append(chat_item)

    # Calculate manifest sha256 and served message count
    canonical_bytes = canonical_json_bytes(chats_list)
    manifest_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    total_served_count = len(served_candidates)

    # Calculate watermark:
    # When truncated (next_cursor is not None), do NOT advance watermark: keep previous watermark (from_ts/since)
    if next_cursor is not None:
        watermark_epoch = since_epoch
    else:
        max_message_time = max((c["ts"] for c in served_candidates), default=since_epoch)
        watermark_epoch = max_message_time if total_served_count > 0 else to_epoch
        if hold_epoch is not None:
            watermark_epoch = min(watermark_epoch, hold_epoch - 1.0)
    watermark_str = format_rfc3339(watermark_epoch)

    now_epoch = time.time()
    media_included_count = 0
    ocr_chars_count = 0
    for c in chats_list:
        for m in c.get("messages", []):
            m_obj = m.get("media")
            if m_obj is not None:
                media_included_count += 1
            if ocr_mode == "full":
                ocr_info = m_obj.get("ocr") if m_obj else None
                if isinstance(ocr_info, dict) and ocr_info.get("text"):
                    ocr_chars_count += len(str(ocr_info["text"]))
            elif ocr_mode == "head":
                if m.get("kind") == "image" and m.get("text"):
                    ocr_chars_count += len(str(m["text"]))

    response_payload = {
        "exported_at": format_rfc3339(now_epoch),
        "range": {
            "from": format_rfc3339(since_epoch),
            "to": format_rfc3339(to_epoch),
        },
        "watermark": watermark_str,
        "next_cursor": next_cursor,
        "chats": chats_list,
        "manifest": {
            "sha256": manifest_sha256,
            "message_count": total_served_count,
        },
        "payload_stats": {
            "messages": total_served_count,
            "media_included": media_included_count,
            "ocr_chars": ocr_chars_count if ocr_mode != "none" else 0,
            "truncated": truncated,
        },
    }
    return response_payload


class WeChatExportHandler(BaseHTTPRequestHandler):
    """HTTP request handler for WeChat export API."""

    def __init__(self, *args: Any, server_context: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self.server_context = server_context or {}
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        """Custom log message formatting."""
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def send_json(self, status_code: int, data: dict[str, Any]) -> None:
        """Send a JSON HTTP response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_chunk(self, data: bytes) -> None:
        """Writes a chunk in HTTP chunked transfer coding."""
        if not data:
            return
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def end_chunked(self) -> None:
        """Writes the final empty chunk terminating chunked transfer coding."""
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def check_auth(self) -> bool:
        """Validates Bearer token in Authorization header when auth is enabled.

        Auth is disabled only when the operator explicitly starts with
        ``--insecure-no-auth`` (and no token). In that case any request passes,
        which is intentionally fail-open but gated behind an explicit opt-in flag.
        """
        if not self.server_context.get("auth_enabled", True):
            return True
        expected_token = self.server_context.get("token")
        if not expected_token:
            # Defensive: auth_enabled without a token must never silently pass.
            return False
        auth_header = self.headers.get("Authorization", "")
        parts = auth_header.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == expected_token:
            return True
        return False

    def do_GET(self) -> None:
        """Handles GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        if path in ("/health", "/api/wechat/health"):
            # Intentionally UNAUTHENTICATED liveness probe (F-003): it exposes no chat
            # data, only service status. `/api/wechat/export` always requires a Bearer
            # token. This two-tier policy is documented in docs/API_SPEC.md,
            # docs/DATA_CONTRACT.md, and AGENT.md.
            self.send_json(200, {"status": "ok"})
            return

        if path == "/api/health/briefing":
            # 与导出接口同一套 Bearer 鉴权：简报含主机健康明细，绝不匿名暴露。
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            briefing_path = Path(
                self.server_context.get("briefing_path") or DEFAULT_BRIEFING_PATH
            ).expanduser()
            try:
                with open(briefing_path, "r", encoding="utf-8") as f:
                    briefing = json.load(f)
                self.send_json(200, briefing)
            except FileNotFoundError:
                self.send_json(503, {"error": "Briefing snapshot not available yet"})
            except (OSError, ValueError) as e:
                self.send_json(500, {"error": f"Failed to read briefing snapshot: {e}"})
            return

        if path == "/api/wechat/export":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            now = time.time()
            since_val = query.get("since", [None])[0]
            to_val = query.get("to", [None])[0]

            media_param = query.get("media", ["all"])[0]
            if media_param not in ("none", "image", "video", "all"):
                self.send_json(400, {
                    "error": {
                        "code": "invalid_param",
                        "message": f"Invalid media parameter: {media_param!r}",
                        "hint": "media ∈ none|image|video|all",
                    }
                })
                return

            ocr_param = query.get("ocr", ["head"])[0]
            if ocr_param not in ("none", "head", "full"):
                self.send_json(400, {
                    "error": {
                        "code": "invalid_param",
                        "message": f"Invalid ocr parameter: {ocr_param!r}",
                        "hint": "ocr ∈ none|head|full",
                    }
                })
                return

            format_param = query.get("format", ["json"])[0]
            if format_param not in ("json", "ndjson"):
                self.send_json(400, {
                    "error": {
                        "code": "invalid_param",
                        "message": f"Invalid format parameter: {format_param!r}",
                        "hint": "format ∈ json|ndjson",
                    }
                })
                return

            limit_val = query.get("limit", [None])[0]
            limit = 0
            if limit_val is not None:
                try:
                    limit = int(limit_val)
                    if limit < 0:
                        raise ValueError()
                except ValueError:
                    self.send_json(400, {
                        "error": {
                            "code": "invalid_param",
                            "message": f"Invalid limit parameter: {limit_val!r}",
                            "hint": "limit must be a non-negative integer (0 = unlimited)",
                        }
                    })
                    return

            cursor_val = query.get("cursor", [None])[0]
            cursor_tuple: tuple[int, str, int] | None = None
            if cursor_val is not None:
                try:
                    cursor_tuple = parse_cursor(cursor_val)
                except ValueError as e:
                    self.send_json(400, {
                        "error": {
                            "code": "invalid_cursor",
                            "message": f"Invalid cursor: {e}",
                            "hint": "cursor must be base64url-encoded JSON {'ts':int,'chat_id':str,'local_id':int}",
                        }
                    })
                    return

            media_chats_val = query.get("media_chats", [None])[0]
            media_chats_set: set[str] | None = None
            if media_chats_val is not None:
                media_chats_set = {c.strip() for c in media_chats_val.split(",") if c.strip()}

            try:
                since_epoch = parse_time_param(since_val, default_epoch=now - 86400.0)
                to_epoch = parse_time_param(to_val, default_epoch=now)
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return

            vault_dir = Path(
                self.server_context.get(
                    "vault_dir", "~/.local/share/wechat-vault/decrypted/current"
                )
            ).expanduser()
            allowlist = self.server_context.get("allowlist", [])
            blocklist = self.server_context.get("blocklist", [])

            try:
                result = export_wechat_data(
                    vault_dir=vault_dir,
                    since_epoch=since_epoch,
                    to_epoch=to_epoch,
                    allowlist=allowlist,
                    blocklist=blocklist,
                    server_context=self.server_context,
                    media_filter=media_param,
                    media_chats=media_chats_set,
                    ocr_mode=ocr_param,
                    limit=limit,
                    cursor_tuple=cursor_tuple,
                )
                if format_param == "ndjson":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()

                    # 1. Manifest header row
                    manifest_row = {
                        "type": "manifest",
                        "exported_at": result["exported_at"],
                        "range": result["range"],
                        "watermark": result["watermark"],
                        "manifest": result["manifest"],
                    }
                    self.write_chunk(json.dumps(manifest_row, ensure_ascii=False).encode("utf-8") + b"\n")

                    # 2. Chat and message rows
                    for chat in result.get("chats", []):
                        chat_row = {
                            "type": "chat",
                            "chat_id": chat.get("chat_id") or chat.get("name"),
                            "name": chat["name"],
                            "kind": chat["kind"],
                        }
                        self.write_chunk(json.dumps(chat_row, ensure_ascii=False).encode("utf-8") + b"\n")
                        cid = chat.get("chat_id") or chat.get("name")
                        for msg in chat.get("messages", []):
                            msg_row = dict(msg)
                            msg_row["type"] = "message"
                            msg_row["chat_id"] = cid
                            self.write_chunk(json.dumps(msg_row, ensure_ascii=False).encode("utf-8") + b"\n")

                    # 3. End row
                    end_row = {
                        "type": "end",
                        "next_cursor": result["next_cursor"],
                        "payload_stats": result["payload_stats"],
                    }
                    self.write_chunk(json.dumps(end_row, ensure_ascii=False).encode("utf-8") + b"\n")
                    self.end_chunked()
                else:
                    self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/wechat/media":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            q = query.get("q", [""])[0].strip()
            chat_id = query.get("chat_id", [""])[0].strip()
            since_val = query.get("since", [None])[0]
            until_val = query.get("until", [None])[0]
            quality = query.get("quality", [""])[0].strip()
            kind_val = query.get("kind", ["image,video"])[0].strip()
            target_kinds = {k.strip() for k in kind_val.split(",") if k.strip()}
            if not target_kinds:
                target_kinds = {"image", "video"}
            include_val = query.get("include", [""])[0].strip()
            include_context = "context" in [x.strip() for x in include_val.split(",") if x.strip()]

            limit_val = query.get("limit", [None])[0]
            offset_val = query.get("offset", ["0"])[0]

            max_limit = 50 if include_context else 100
            try:
                limit = int(limit_val) if limit_val is not None else 20
                limit = min(max(1, limit), max_limit)
                offset = max(0, int(offset_val))
            except ValueError:
                self.send_json(400, {"error": "Invalid limit or offset parameter"})
                return

            try:
                since_ts = parse_time_param(since_val, default_epoch=0.0) if since_val else 0.0
                until_ts = parse_time_param(until_val, default_epoch=float("inf")) if until_val else float("inf")
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return

            vault_dir = Path(
                self.server_context.get(
                    "vault_dir", "~/.local/share/wechat-vault/decrypted/current"
                )
            ).expanduser()
            contacts = get_cached_contacts(vault_dir)
            allowlist = self.server_context.get("allowlist", [])
            blocklist = self.server_context.get("blocklist", [])

            _vroot, _pol_path, ledger_path = get_media_paths(self.server_context)
            ledger = load_json_cached(ledger_path)
            assets = ledger.get("assets") or {}
            by_message = ledger.get("by_message") or {}
            anchors_map = ledger.get("anchors") or {}

            matches: list[dict[str, Any]] = []
            seen_instances = set()

            for mkey, aid in by_message.items():
                seen_instances.add(mkey)
                asset = assets.get(aid or "")
                if not asset or asset.get("state") != "ready":
                    continue
                akind = asset.get("kind")
                if akind not in target_kinds:
                    continue
                anchor = dict(anchors_map.get(mkey) or asset.get("anchor") or {})
                m_chat = anchor.get("chat_id") or (mkey.split("|")[0] if "|" in mkey else "")
                if not m_chat:
                    continue
                cinfo = contacts.get(m_chat)
                if not is_chat_allowed(m_chat, cinfo, allowlist, blocklist):
                    continue
                if chat_id and m_chat != chat_id:
                    continue
                ts = int(anchor.get("ts") or asset.get("create_time") or 0)
                if ts < since_ts or ts > until_ts:
                    continue
                if quality and asset.get("quality") != quality:
                    continue
                ocr_dict = asset.get("ocr") or {}
                ocr_text = str(ocr_dict.get("text") or "")
                if q and q.lower() not in ocr_text.lower():
                    continue

                anchor["chat_id"] = m_chat
                anchor["chat_name"] = resolve_chat_name(m_chat, vault_dir, contacts)
                if "|" in mkey and "local_id" not in anchor:
                    try:
                        anchor["local_id"] = int(mkey.split("|")[1])
                    except (ValueError, IndexError):
                        pass
                anchor.setdefault("ts", ts)
                anchor.setdefault("sender", "我")

                aid = asset.get("md5") or asset.get("asset_id") or ""
                fmt = asset.get("format", "jpg" if akind == "image" else "mp4")
                item: dict[str, Any] = {
                    "asset_id": aid,
                    "anchor": anchor,
                    "format": fmt,
                    "width": asset.get("width"),
                    "height": asset.get("height"),
                    "bytes": int(asset.get("bytes") or 0),
                    "url": (
                        asset.get("url")
                        or f"/api/wechat/media/{aid}/{akind}.{fmt}"
                    ),
                }
                if akind == "image":
                    item["quality"] = asset.get("quality", "full")
                    item["ocr"] = ocr_dict
                elif akind == "video":
                    item["duration_s"] = asset.get("duration_s")
                    item["poster"] = (
                        asset.get("poster")
                        or (f"/api/wechat/media/{aid}/thumb.jpg" if asset.get("thumb") else None)
                    )
                matches.append(item)
            for aid, asset in assets.items():
                if not asset or asset.get("state") != "ready":
                    continue
                akind = asset.get("kind")
                if akind not in target_kinds:
                    continue
                anchor = dict(asset.get("anchor") or {})
                m_chat = anchor.get("chat_id", "")
                if not m_chat:
                    continue
                lid = anchor.get("local_id", 0)
                mkey = f"{m_chat}|{lid}"
                if mkey in seen_instances:
                    continue
                cinfo = contacts.get(m_chat)
                if not is_chat_allowed(m_chat, cinfo, allowlist, blocklist):
                    continue
                if chat_id and m_chat != chat_id:
                    continue
                ts = int(anchor.get("ts") or asset.get("create_time") or 0)
                if ts < since_ts or ts > until_ts:
                    continue
                if quality and asset.get("quality") != quality:
                    continue
                ocr_dict = asset.get("ocr") or {}
                ocr_text = str(ocr_dict.get("text") or "")
                if q and q.lower() not in ocr_text.lower():
                    continue

                anchor["chat_id"] = m_chat
                anchor["chat_name"] = resolve_chat_name(m_chat, vault_dir, contacts)
                aid = asset.get("md5") or asset.get("asset_id") or ""
                fmt = asset.get("format", "jpg" if akind == "image" else "mp4")
                item = {
                    "asset_id": aid,
                    "anchor": anchor,
                    "format": fmt,
                    "width": asset.get("width"),
                    "height": asset.get("height"),
                    "bytes": int(asset.get("bytes") or 0),
                    "url": (
                        asset.get("url")
                        or f"/api/wechat/media/{aid}/{akind}.{fmt}"
                    ),
                }
                if akind == "image":
                    item["quality"] = asset.get("quality", "full")
                    item["ocr"] = ocr_dict
                elif akind == "video":
                    item["duration_s"] = asset.get("duration_s")
                    item["poster"] = (
                        asset.get("poster")
                        or (f"/api/wechat/media/{aid}/thumb.jpg" if asset.get("thumb") else None)
                    )
                matches.append(item)

            matches.sort(
                key=lambda x: (
                    int(x["anchor"].get("ts") or 0),
                    int(x["anchor"].get("local_id") or 0),
                ),
                reverse=True,
            )

            paged = matches[offset: offset + limit]
            if include_context:
                for item in paged:
                    anc = item["anchor"]
                    before, after = fetch_message_context(
                        vault_dir=vault_dir,
                        chat_id=anc["chat_id"],
                        target_local_id=int(anc["local_id"]),
                        target_ts=int(anc["ts"]),
                        contacts=contacts,
                    )
                    item["context"] = {"before": before, "after": after}

            self.send_json(200, {
                "items": paged,
                "total": len(matches),
                "limit": limit,
                "offset": offset,
            })
            return

        m_media_file = re.fullmatch(r"^/api/wechat/media/([0-9a-f]{32})/([^/]+)$", path)
        if m_media_file:
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            asset_id = m_media_file.group(1).lower()
            filename = m_media_file.group(2)

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in ALLOWED_MEDIA_EXTS or not re.fullmatch(r"^[a-zA-Z0-9_.-]+$", filename):
                self.send_json(400, {"error": "Invalid filename or extension"})
                return

            vault_dir = Path(
                self.server_context.get(
                    "vault_dir", "~/.local/share/wechat-vault/decrypted/current"
                )
            ).expanduser()
            blocklist = self.server_context.get("blocklist", [])
            allowlist = self.server_context.get("allowlist", [])
            contacts = get_cached_contacts(vault_dir)

            _vroot, _pol_path, ledger_path = get_media_paths(self.server_context)
            ledger = load_json_cached(ledger_path)
            assets = ledger.get("assets") or {}
            by_message = ledger.get("by_message") or {}
            anchors_map = ledger.get("anchors") or {}

            # 安全校验：若所属会话均被 blocklist 屏蔽或未在白名单，拒绝访问
            associated_chats = set()
            for mkey, aid in by_message.items():
                if aid == asset_id and "|" in mkey:
                    associated_chats.add(mkey.split("|")[0])
            for _mkey, anc in anchors_map.items():
                if anc.get("asset_id") == asset_id and anc.get("chat_id"):
                    associated_chats.add(anc["chat_id"])
            if asset_id in assets and assets[asset_id].get("anchor", {}).get("chat_id"):
                associated_chats.add(assets[asset_id]["anchor"]["chat_id"])

            if associated_chats:
                allowed_any = False
                for c in associated_chats:
                    cinfo = contacts.get(c)
                    if is_chat_allowed(c, cinfo, allowlist, blocklist):
                        allowed_any = True
                        break
                if not allowed_any:
                    self.send_json(404, {"error": "Not Found"})
                    return

            vroot_media = (_vroot / "media").resolve()
            target_path = (vroot_media / asset_id / filename).resolve()
            try:
                target_path.relative_to(vroot_media)
            except ValueError:
                self.send_json(403, {"error": "Forbidden: path traversal detected"})
                return

            if not target_path.is_file():
                asset_dir = (vroot_media / asset_id).resolve()
                if asset_dir.is_dir():
                    cands = [
                        p for p in asset_dir.iterdir()
                        if p.is_file() and p.suffix.lstrip(".").lower() in ALLOWED_MEDIA_EXTS
                    ]
                    if cands:
                        target_path = cands[0]
                        ext = target_path.suffix.lstrip(".").lower()
            if not target_path.is_file():
                self.send_json(404, {"error": "Not Found"})
                return
            sha256 = (assets.get(asset_id) or {}).get("sha256")
            if not sha256:
                try:
                    data = target_path.read_bytes()
                    sha256 = hashlib.sha256(data).hexdigest()
                except OSError:
                    self.send_json(500, {"error": "Failed to read media file"})
                    return
            else:
                data = None

            etag_header = f'"{sha256}"'
            if_none_match = self.headers.get("If-None-Match", "").strip()
            if if_none_match:
                client_tags = [t.strip(' "') for t in if_none_match.split(",")]
                if sha256 in client_tags:
                    self.send_response(304)
                    self.send_header("ETag", etag_header)
                    self.end_headers()
                    return

            if data is None:
                try:
                    data = target_path.read_bytes()
                except OSError:
                    self.send_json(500, {"error": "Failed to read media file"})
                    return

            content_types = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
                "mp4": "video/mp4",
                "mov": "video/quicktime",
            }
            ct = content_types.get(ext, "application/octet-stream")
            file_size = len(data)

            # Range support
            range_header = self.headers.get("Range", "").strip()
            m_range = re.fullmatch(r"^bytes=(\d*)-(\d*)$", range_header) if range_header else None
            is_video = ext in ("mp4", "mov")
            if m_range and file_size > 0:
                start_str, end_str = m_range.group(1), m_range.group(2)
                if not start_str and not end_str:
                    # Invalid range -> fallback to 200
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(file_size))
                    if is_video:
                        self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", etag_header)
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if not start_str:
                    # suffix bytes: bytes=-500
                    suffix_len = int(end_str)
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                elif not end_str:
                    # bytes=500-
                    start = int(start_str)
                    end = file_size - 1
                else:
                    start = int(start_str)
                    end = int(end_str)

                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                end = min(end, file_size - 1)
                part = data[start: end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(len(part)))
                if is_video:
                    self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag_header)
                self.end_headers()
                self.wfile.write(part)
                return

            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(file_size))
            if is_video:
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag_header)
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_json(404, {"error": "Not Found"})


def create_server(
    host: str,
    port: int,
    vault_dir: Path | str,
    token: str | None = None,
    auth_enabled: bool = True,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    briefing_path: str | None = None,
    media_vault_root: str | None = None,
    media_policy_path: str | None = None,
    media_ledger_path: str | None = None,
) -> HTTPServer:
    """Creates an HTTPServer instance with configured server context."""
    server_context = {
        "vault_dir": str(vault_dir),
        "token": token,
        "auth_enabled": auth_enabled,
        "allowlist": allowlist or [],
        "blocklist": blocklist or [],
        "briefing_path": briefing_path or DEFAULT_BRIEFING_PATH,
        "media_vault_root": media_vault_root,
        "media_policy_path": media_policy_path,
        "media_ledger_path": media_ledger_path,
    }
    try:
        from http.server import ThreadingHTTPServer
        server_class = ThreadingHTTPServer
    except ImportError:
        server_class = HTTPServer

    def handler_factory(*args: Any, **kwargs: Any) -> WeChatExportHandler:
        return WeChatExportHandler(*args, server_context=server_context, **kwargs)

    return server_class((host, port), handler_factory)


def load_config_file(config_path: Path) -> dict[str, Any]:
    """Loads JSON configuration file."""
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a JSON object, got {type(data).__name__}")
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Export API HTTP Service")
    parser.add_argument("--config", type=str, help="Path to JSON configuration file")
    parser.add_argument("--host", type=str, help="Host/IP to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port to bind to (default: 8765)")
    parser.add_argument("--vault-dir", type=str, help="Path to decrypted WeChat vault directory")
    parser.add_argument(
        "--token",
        type=str,
        help="Bearer token for API authorization. REQUIRED (fail-closed): the service refuses "
             "to start without this (or a 'token' in config) unless --insecure-no-auth is given.",
    )
    parser.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Run the API WITHOUT authentication. Dangerous: any client that can reach the "
             "bound port can read all exported chat data. Use only on trusted loopback/dev. "
             "Required to start when no token is configured (fail-closed default otherwise).",
    )

    args = parser.parse_args()

    config: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8765,
        "vault_dir": os.path.expanduser("~/.local/share/wechat-vault/decrypted/current"),
        "token": None,
        "allowlist": [],
        "blocklist": [],
    }

    if args.config:
        cfg_data = load_config_file(Path(args.config).expanduser())
        config.update(cfg_data)

    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.vault_dir:
        config["vault_dir"] = args.vault_dir
    if args.token:
        config["token"] = args.token

    host = config["host"]
    port = int(config["port"])
    vault_dir = Path(config["vault_dir"]).expanduser()
    token = config.get("token")
    auth_enabled = bool(token) or bool(args.insecure_no_auth)
    # When --insecure-no-auth is set with an empty token, auth is explicitly disabled.
    if args.insecure_no_auth and not token:
        auth_enabled = False
    # Fail-closed auth policy: refuse to start without a token unless the operator
    # explicitly opts out with --insecure-no-auth. A token-less server would otherwise
    # accept unauthenticated requests and expose all exported chat data.
    if not token and not args.insecure_no_auth:
        print(
            "[FATAL] No auth token configured (--token / config 'token'). "
            "Refusing to start with fail-open auth.",
            file=sys.stderr,
        )
        print(
            "[FATAL] Set a high-entropy token (e.g. `openssl rand -hex 32`) in config or "
            "`--token`, or pass `--insecure-no-auth` to run unauthenticated on trusted loopback only.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.insecure_no_auth and not token:
        print(
            "[WARN] Authentication DISABLED via --insecure-no-auth. The export endpoint is "
            "open to anyone who can reach the bound port. Bind 127.0.0.1 only.",
            file=sys.stderr,
        )
    allowlist = config.get("allowlist", [])
    blocklist = config.get("blocklist", [])

    print(f"[INFO] Starting WeChat Export API on {host}:{port}")
    print(f"[INFO] Vault directory: {vault_dir}")
    print(f"[INFO] Token auth enabled: {bool(token)}")
    if allowlist:
        print(f"[INFO] Allowlist: {allowlist}")
    if blocklist:
        print(f"[INFO] Blocklist: {blocklist}")

    server = create_server(
        host=host,
        port=port,
        vault_dir=vault_dir,
        token=token,
        auth_enabled=auth_enabled,
        allowlist=allowlist,
        blocklist=blocklist,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down WeChat Export API server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
