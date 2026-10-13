#!/usr/bin/env python3
"""WeChat Export API - Standalone HTTP service for exporting WeChat message archives.

Conforms to the Muse WeChat Export specification.
"""
from __future__ import annotations

import argparse
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
) -> dict[str, Any]:
    """Exports decrypted WeChat messages within the specified time range."""
    contacts = load_contacts(vault_dir)

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
    # Group results by chat_id
    chats_data: dict[str, dict[str, Any]] = {}
    total_message_count = 0
    max_message_time = since_epoch

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

                if chat_id not in chats_data:
                    chats_data[chat_id] = {
                        "name": chat_display_name,
                        "kind": chat_kind,
                        "messages": [],
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

                    ts_str = format_rfc3339(create_time)
                    if create_time > max_message_time:
                        max_message_time = create_time

                    chats_data[chat_id]["messages"].append({
                        "ts": ts_str,
                        "sender": sender_display,
                        "kind": kind,
                        "text": text,
                        "note": note,
                    })
                    total_message_count += 1
        finally:
            con.close()

    # Sort messages within each chat by ts
    chats_list: list[dict[str, Any]] = []
    for chat_id in sorted(chats_data.keys()):
        chat_item = chats_data[chat_id]
        chat_item["messages"].sort(key=lambda m: m["ts"])
        chats_list.append(chat_item)

    # Calculate manifest sha256
    canonical_bytes = canonical_json_bytes(chats_list)
    manifest_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

    watermark_epoch = max_message_time if total_message_count > 0 else to_epoch
    watermark_str = format_rfc3339(watermark_epoch)

    now_epoch = time.time()
    response_payload = {
        "exported_at": format_rfc3339(now_epoch),
        "range": {
            "from": format_rfc3339(since_epoch),
            "to": format_rfc3339(to_epoch),
        },
        "watermark": watermark_str,
        "chats": chats_list,
        "manifest": {
            "sha256": manifest_sha256,
            "message_count": total_message_count,
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

        if path == "/api/wechat/export":
            if not self.check_auth():
                self.send_json(401, {"error": "Unauthorized"})
                return

            now = time.time()
            since_val = query.get("since", [None])[0]
            to_val = query.get("to", [None])[0]

            try:
                since_epoch = parse_time_param(since_val, default_epoch=now - 86400.0)
                to_epoch = parse_time_param(to_val, default_epoch=now)
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return

            vault_dir = Path(self.server_context.get("vault_dir", "~/.local/share/wechat-vault/decrypted/current")).expanduser()
            allowlist = self.server_context.get("allowlist", [])
            blocklist = self.server_context.get("blocklist", [])

            try:
                result = export_wechat_data(
                    vault_dir=vault_dir,
                    since_epoch=since_epoch,
                    to_epoch=to_epoch,
                    allowlist=allowlist,
                    blocklist=blocklist,
                )
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
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
) -> HTTPServer:
    """Creates an HTTPServer instance with configured server context."""
    server_context = {
        "vault_dir": str(vault_dir),
        "token": token,
        "auth_enabled": auth_enabled,
        "allowlist": allowlist or [],
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
