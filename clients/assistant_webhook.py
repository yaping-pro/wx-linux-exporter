#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
assistant_webhook.py - Universal Personal Assistant Webhook Bridge.

Polls WeChat Pad Stream export endpoint, normalizes new messages into
structured events, and delivers them via HTTP POST to AI Assistant Webhooks
(Dify, Feishu, Custom).

Zero third-party dependencies (Python 3 stdlib only).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ENDPOINT = (
    os.environ.get("WECHAT_EXPORT_URL", "").strip()
    or os.environ.get(
        "WX_EXPORT_ENDPOINT",
        "http://127.0.0.1:8765/api/wechat/export",
    )
)
DEFAULT_TOKEN = (
    os.environ.get("WECHAT_BEARER_TOKEN", "").strip()
    or os.environ.get("WX_DEVICE_TOKEN", "").strip()
    or os.environ.get("WX_EXPORT_TOKEN", "").strip()
    or os.environ.get("WXPAD_TOKEN", "").strip()
)
DEFAULT_PUBLIC_BASE_URL = os.environ.get("WECHAT_PUBLIC_BASE_URL", "").strip()
DEFAULT_WEBHOOK_URL = os.environ.get("ASSISTANT_WEBHOOK_URL", "")
DEFAULT_WEBHOOK_TYPE = os.environ.get(
    "ASSISTANT_WEBHOOK_TYPE", "custom"
).lower()


def parse_iso(ts_str: str) -> Optional[datetime.datetime]:
    if not ts_str:
        return None
    try:
        return datetime.datetime.fromisoformat(ts_str)
    except Exception:
        return None


def fetch_export(
    endpoint: str,
    token: str = "",
    watermark: Optional[str] = None,
    timeout: int = 15,
    media: str = "all",
    media_chats: str = "",
    ocr: str = "head",
) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
    url = endpoint
    parsed = urllib.parse.urlparse(endpoint)
    query = urllib.parse.parse_qs(parsed.query)

    if watermark:
        query["since"] = [watermark]
    if media:
        query["media"] = [media]
    if media_chats:
        query["media_chats"] = [media_chats]
    if ocr:
        query["ocr"] = [ocr]

    url = urllib.parse.urlunparse(
        parsed._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        )
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "wx-linux-exporter-assistant-bridge/1.0"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
            return status, json.loads(body), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTPError {e.code}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def derive_base_url(export_url: str) -> str:
    """Derive scheme + netloc from an export endpoint URL."""
    if not export_url:
        return ""
    try:
        parsed = urllib.parse.urlparse(export_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def format_payload(
    webhook_type: str,
    chat_id: str,
    msg: Dict[str, Any],
    include_media: bool = True,
    export_url: str = "",
    public_base_url: str = "",
) -> Dict[str, Any]:
    sender = msg.get("sender", "")
    ts = msg.get("ts", "")
    kind = msg.get("kind", msg.get("type", "text"))
    text = msg.get("text", msg.get("content", ""))
    note = msg.get("note", "")

    # Backwards-compatible content extraction: fallback to note tag
    content = text if text else (f"[{note}]" if note else "")
    msg_id = msg.get("id", "")
    is_sender = (
        msg.get("is_sender")
        if "is_sender" in msg
        else (sender in ("self", "我"))
    )

    media = msg.get("media") if include_media else None
    has_media = isinstance(media, dict) and bool(media)

    media_url: Optional[str] = None
    media_url_abs: Optional[str] = None
    media_url_auth: Optional[str] = None
    media_asset_id: Optional[str] = None
    media_poster_url: Optional[str] = None
    media_poster_url_abs: Optional[str] = None
    chat_name: Optional[str] = None
    ocr_text: Optional[str] = None

    if has_media:
        media_url = media.get("url")
        media_asset_id = media.get("asset_id")
        anchor = (
            media.get("anchor")
            if isinstance(media.get("anchor"), dict)
            else {}
        )
        chat_name = anchor.get("chat_name") or chat_id
        ocr = (
            media.get("ocr")
            if isinstance(media.get("ocr"), dict)
            else {}
        )
        ocr_text = ocr.get("text")

        base = (
            public_base_url.rstrip("/")
            or derive_base_url(export_url).rstrip("/")
        )
        if media_url:
            media_url_abs = f"{base}{media_url}" if base else media_url
            media_url_auth = "bearer"

        mkind = media.get("kind")
        poster = media.get("poster")
        if mkind == "video" and poster:
            media_poster_url = poster
            if poster.startswith("http://") or poster.startswith("https://"):
                media_poster_url_abs = poster
            else:
                media_poster_url_abs = f"{base}{poster}" if base else poster

    if webhook_type == "feishu":
        # Format as Feishu interactive / text webhook payload
        header = f"[{'Self' if is_sender else sender}] in {chat_id} ({ts})"
        text_content = f"{header}\n{content}"
        if has_media and media_url_abs:
            text_content = f"{text_content}\n🖼 原图: {media_url_abs}"
        if has_media and media_poster_url_abs:
            text_content = f"{text_content}\n🎬 首帧: {media_poster_url_abs}"
        return {
            "msg_type": "text",
            "content": {
                "text": text_content,
            },
        }

    if webhook_type == "dify":
        # Format as Dify Workflow trigger payload
        inputs: Dict[str, Any] = {
            "chat_id": chat_id,
            "sender": sender,
            "content": content,
            "msg_type": kind,
            "timestamp": ts,
            "is_sender": is_sender,
            "raw_id": msg_id,
            "kind": kind,
            "text": text,
            "note": note,
        }
        if has_media:
            if media_url is not None:
                inputs["media_url"] = media_url
            if media_url_abs is not None:
                inputs["media_url_abs"] = media_url_abs
            if media_url_auth is not None:
                inputs["media_url_auth"] = media_url_auth
            if media_poster_url is not None:
                inputs["media_poster_url"] = media_poster_url
            if media_poster_url_abs is not None:
                inputs["media_poster_url_abs"] = media_poster_url_abs
            if media_asset_id is not None:
                inputs["media_asset_id"] = media_asset_id
            if chat_name is not None:
                inputs["chat_name"] = chat_name
            inputs["media"] = media
            if ocr_text is not None:
                inputs["ocr_text"] = ocr_text
        return {
            "inputs": inputs,
            "response_mode": "streaming",
            "user": sender or chat_id,
        }

    # Default 'custom' or OpenAI-compatible format
    payload: Dict[str, Any] = {
        "event": "wechat_message",
        "chat_id": chat_id,
        "sender": sender,
        "is_sender": is_sender,
        "content": content,
        "type": kind,
        "kind": kind,
        "text": text,
        "note": note,
        "timestamp": ts,
        "ts": ts,
        "id": msg_id,
    }
    if has_media:
        if media_url is not None:
            payload["media_url"] = media_url
        if media_url_abs is not None:
            payload["media_url_abs"] = media_url_abs
        if media_url_auth is not None:
            payload["media_url_auth"] = media_url_auth
        if media_poster_url is not None:
            payload["media_poster_url"] = media_poster_url
        if media_poster_url_abs is not None:
            payload["media_poster_url_abs"] = media_poster_url_abs
        if media_asset_id is not None:
            payload["media_asset_id"] = media_asset_id
        if chat_name is not None:
            payload["chat_name"] = chat_name
        payload["media"] = media
        if ocr_text is not None:
            payload["ocr_text"] = ocr_text
    return payload

def post_webhook(
    webhook_url: str,
    payload: Dict[str, Any],
    dry_run: bool = False,
    timeout: int = 10,
) -> bool:
    if dry_run or not webhook_url:
        print(f"[DRY-RUN] Webhook POST -> {webhook_url or 'NONE'}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.getcode() < 300
    except Exception as e:
        print(f"[ERROR] Failed to post webhook: {e}", file=sys.stderr)
        return False


def process_chats(
    chats: List[Dict[str, Any]],
    webhook_url: str,
    webhook_type: str,
    dry_run: bool = False,
    ignore_self: bool = False,
    include_media: bool = True,
    export_url: str = "",
    public_base_url: str = "",
) -> int:
    sent_count = 0
    for chat in chats:
        chat_id = chat.get("chat_id", "unknown")
        messages = chat.get("messages", [])
        for msg in messages:
            is_sender = (
                msg.get("is_sender")
                if "is_sender" in msg
                else (msg.get("sender") in ("self", "我"))
            )
            if ignore_self and is_sender:
                continue
            payload = format_payload(
                webhook_type,
                chat_id,
                msg,
                include_media=include_media,
                export_url=export_url,
                public_base_url=public_base_url,
            )
            ok = post_webhook(webhook_url, payload, dry_run=dry_run)
            if ok:
                sent_count += 1
    return sent_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Forward real-time WeChat messages to AI Assistant Webhooks "
            "(Dify, Feishu, Custom)."
        )
    )
    parser.add_argument(
        "--endpoint",
        "--export-url",
        dest="endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"WeChat Exporter API endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Bearer token for daemon authentication",
    )
    parser.add_argument(
        "--public-base-url",
        default=DEFAULT_PUBLIC_BASE_URL,
        help=(
            "Public base URL for constructing absolute media URLs "
            "(default: env WECHAT_PUBLIC_BASE_URL or scheme+netloc)"
        ),
    )
    parser.add_argument(
        "--media",
        choices=["none", "image", "video", "all"],
        default="all",
        help="Media inclusion filter passed to export endpoint (default: all)",
    )
    parser.add_argument(
        "--media-chats",
        default="",
        help="Comma-separated chat_ids to filter media enrichment at export",
    )
    parser.add_argument(
        "--ocr",
        choices=["none", "head", "full"],
        default="head",
        help="OCR projection detail level passed to export (default: head)",
    )
    parser.add_argument(
        "--webhook-url",
        default=DEFAULT_WEBHOOK_URL,
        help="Target Webhook URL (default: env ASSISTANT_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--webhook-type",
        choices=["custom", "dify", "feishu"],
        default=DEFAULT_WEBHOOK_TYPE,
        help="Webhook format type: custom (default), dify, feishu",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=0,
        help="Poll interval in seconds (0 = single shot run)",
    )
    parser.add_argument(
        "--ignore-self",
        action="store_true",
        help="Ignore messages sent by self (is_sender == true)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print parsed events without sending HTTP requests to webhook",
    )
    parser.add_argument(
        "--include-media",
        dest="include_media",
        action="store_true",
        default=True,
        help="Include enriched media object in webhook payloads (default)",
    )
    parser.add_argument(
        "--no-media",
        dest="include_media",
        action="store_false",
        help="Disable media object passthrough to webhook",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.dry_run and not args.webhook_url:
        print(
            "[WARN] No --webhook-url provided and not in --dry-run mode. "
            "Messages will only be logged.",
            file=sys.stderr,
        )

    current_watermark: Optional[str] = None
    print(
        f"[assistant_webhook] Starting bridge (endpoint={args.endpoint}, "
        f"type={args.webhook_type}, dry_run={args.dry_run})"
    )

    while True:
        code, data, err = fetch_export(
            args.endpoint,
            token=args.token,
            watermark=current_watermark,
            media=args.media,
            media_chats=args.media_chats,
            ocr=args.ocr,
        )
        if err or code != 200:
            print(f"[poll] Fetch error (code={code}): {err}", file=sys.stderr)
        elif data:
            new_wm = data.get("watermark")
            chats = data.get("chats", [])
            msg_count = data.get("manifest", {}).get("message_count", 0)

            if msg_count > 0:
                count = process_chats(
                    chats,
                    args.webhook_url,
                    args.webhook_type,
                    dry_run=args.dry_run,
                    ignore_self=args.ignore_self,
                    include_media=args.include_media,
                    export_url=args.endpoint,
                    public_base_url=args.public_base_url,
                )
                print(
                    f"[poll] Forwarded {count} messages to assistant webhook "
                    f"(watermark: {new_wm})"
                )
            if new_wm:
                current_watermark = new_wm

        if args.poll_interval <= 0:
            break
        time.sleep(args.poll_interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
