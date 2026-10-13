#!/usr/bin/env python3
"""Regression test for clients/assistant_webhook.py field mapping.

Guards against the P0 bug where format_payload() read the wrong message fields
(`content`/`type`) instead of the schema-conformant `text`/`kind`/`note`, which
produced blank webhook payloads and broke `ignore_self`.

Run: python3 clients/test_assistant_webhook.py
Exit code 0 = pass, 1 = fail.
"""
import sys
from pathlib import Path

# Allow running from repo root or clients/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assistant_webhook as aw  # noqa: E402


def schema_conformant_message() -> dict:
    """A message exactly as emitted by wx-pad-exporter / wechat-export-api."""
    return {
        "text": "晚点把发票发你",
        "kind": "text",
        "note": "",
        "ts": "2026-10-13T21:04:05+08:00",
        "sender": "wxid_abc123",
        "id": "msg-0001",
        "is_sender": False,
    }


def check_custom() -> bool:
    msg = schema_conformant_message()
    p = aw.format_payload("custom", "chat_x", msg)
    assert p["content"] == "晚点把发票发你", f"content empty/wrong: {p!r}"
    assert p["type"] == "text", f"type wrong: {p!r}"
    assert p["text"] == "晚点把发票发你", f"text missing: {p!r}"
    assert p["is_sender"] is False
    return True


def check_note_fallback() -> bool:
    msg = {"text": "", "note": "图片", "kind": "image", "ts": "t", "sender": "s", "id": "i"}
    p = aw.format_payload("custom", "c", msg)
    assert p["content"] == "[图片]", f"note fallback failed: {p!r}"
    assert p["type"] == "image"
    return True


def check_feishu() -> bool:
    msg = schema_conformant_message()
    p = aw.format_payload("feishu", "chat_x", msg)
    assert p["msg_type"] == "text"
    assert "晚点把发票发你" in p["content"]["text"], f"feishu content empty: {p!r}"
    return True


def check_ignore_self() -> bool:
    chats = [{
        "chat_id": "c",
        "messages": [
            {"text": "hello", "kind": "text", "sender": "self", "ts": "t", "id": "1"},
            {"text": "hi", "kind": "text", "sender": "wxid_other", "ts": "t", "id": "2"},
        ],
    }]
    sent = aw.process_chats(chats, "http://example.invalid", "custom", dry_run=True, ignore_self=True)
    # Only the non-self message should be "sent" in dry-run.
    assert sent == 1, f"ignore_self failed, sent={sent}"
    return True


def main() -> int:
    checks = [check_custom, check_note_fallback, check_feishu, check_ignore_self]
    failed = 0
    for c in checks:
        try:
            c()
            print(f"[OK] {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {c.__name__}: {e}")
    if failed:
        print(f"\n{failed} check(s) failed.")
        return 1
    print("\nAll field-mapping regression checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
