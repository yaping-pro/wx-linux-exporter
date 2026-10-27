"""wx_store: Ledger persistence, migration, atomic writes, and lookup.

No third-party dependencies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

LEDGER_SCHEMA = 1


def migrate(ledger: dict[str, Any]) -> dict[str, Any]:
    """缺 schema 时补 {"schema": 1}，保留未知键，幂等。"""
    if not isinstance(ledger, dict):
        return {
            "schema": LEDGER_SCHEMA,
            "version": 1,
            "watermark": 0,
            "assets": {},
            "by_message": {},
            "anchors": {},
            "daily": {},
        }
    if "schema" not in ledger:
        ledger["schema"] = LEDGER_SCHEMA
    return ledger


def load_ledger(path: Path | str) -> dict[str, Any]:
    """读取账本，若文件不存在或损坏则返回安全默认结构；自动补齐必要键。"""
    p = Path(path)
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d = migrate(d)
                d.setdefault("assets", {})
                d.setdefault("by_message", {})
                d.setdefault("anchors", {})
                d.setdefault("daily", {})
                return d
        except Exception:  # noqa: BLE001
            pass
    return {
        "schema": LEDGER_SCHEMA,
        "version": 1,
        "watermark": 0,
        "assets": {},
        "by_message": {},
        "anchors": {},
        "daily": {},
    }


def save_ledger_atomic(path: Path | str, ledger: dict[str, Any]) -> None:
    """原子写保存账本，自动确保 schema 字段存在，权限设置为 0o600。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(ledger, dict):
        migrate(ledger)
    prefix = f".{p.name}."
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=prefix, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def asset_for(
    ledger: dict[str, Any],
    chat_id: str,
    local_id: int | str,
) -> dict[str, Any] | None:
    """按 by_message['chat|local'] 取资产，缺失时回退 anchors。"""
    if not isinstance(ledger, dict):
        return None
    mkey = f"{chat_id}|{local_id}"
    assets = ledger.get("assets") or {}
    by_message = ledger.get("by_message") or {}

    # 1. 主路径：by_message -> asset_id -> assets[asset_id]
    aid = by_message.get(mkey)
    if aid and aid in assets:
        return assets[aid]

    # 2. 回退路径：anchors[mkey]
    anchors = ledger.get("anchors") or {}
    anc = anchors.get(mkey)
    if isinstance(anc, dict):
        # anchor 自身若记了 asset_id，尝试在 assets 中取
        anc_aid = anc.get("asset_id") or anc.get("md5")
        if anc_aid and anc_aid in assets:
            return assets[anc_aid]
        # 若 anchor 本身带有 asset 级描述或作为降级信息
        return anc

    return None
