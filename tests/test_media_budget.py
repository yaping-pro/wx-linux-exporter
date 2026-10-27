#!/usr/bin/env python3
"""Tests for per-kind daily budget accounting in the media worker.

Background (production bug, 2026-10-27): file/image/video all charged one shared
`daily["parsed"]` counter. Images are decrypted locally for free but consumed the
`daily_file_budget: 30` meant to meter paid MinerU cloud calls, so a normal image day
pinned the counter at 30/30 and file parsing went no-op for the rest of the day.
Budgets are now per-kind.

These tests are written to FAIL if the bug is reintroduced:
1. file full + image empty -> image still processes (cross-kind isolation)
2. image budget=2 -> 3rd image is deferred with deny_reason
3. legacy policy (daily_file_budget only) -> maps to file budget, image gets default
4. legacy ledger without by_kind -> read as 0, no crash, no backfill
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_media  # noqa: E402


@dataclass(frozen=True)
class MockImageKeys:
    aes_key: bytes = b"0123456789abcdef"
    xor_key: int = 0x0C
    code: int = 1234
    source: str = "mock"


@dataclass(frozen=True)
class MockDatMeta:
    version: str = "v2"
    aes_size: int = 16
    xor_size: int = 16
    aligned: int = 0


@dataclass
class MockDecryptedImage:
    data: bytes
    fmt: str
    meta: MockDatMeta
    quality: str
    width: int | None
    height: int | None


def _build_test_databases(
    vault_dir: Path,
    chats: list[tuple[str, str, str, bool]],
    messages: list[tuple[str, int, int, int, str]],
) -> None:
    (vault_dir / "contact").mkdir(parents=True, exist_ok=True)
    (vault_dir / "session").mkdir(parents=True, exist_ok=True)
    (vault_dir / "message").mkdir(parents=True, exist_ok=True)
    (vault_dir / "hardlink").mkdir(parents=True, exist_ok=True)

    c_con = sqlite3.connect(vault_dir / "contact" / "contact.db")
    c_con.execute(
        "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, "
        "alias TEXT, encrypt_username TEXT, remark TEXT, nick_name TEXT, chat_room_type INTEGER)"
    )
    for i, (u, r, n, is_grp) in enumerate(chats):
        c_con.execute(
            "INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)",
            (i + 1, u, 1, "", "", r, n, 1 if is_grp else 0),
        )
    c_con.commit()
    c_con.close()

    s_con = sqlite3.connect(vault_dir / "session" / "session.db")
    s_con.execute(
        "CREATE TABLE SessionTable(username TEXT PRIMARY KEY, display_name TEXT, "
        "last_timestamp INTEGER, unread_count INTEGER)"
    )
    for u, r, n, _ in chats:
        s_con.execute(
            "INSERT INTO SessionTable VALUES (?,?,?,?)",
            (u, r or n or u, int(time.time()), 0),
        )
    s_con.commit()
    s_con.close()

    m_con = sqlite3.connect(vault_dir / "message" / "message_0.db")
    m_con.execute("CREATE TABLE Name2Id(rowid INTEGER PRIMARY KEY, user_name TEXT, is_session INTEGER)")
    for i, (u, _, _, _) in enumerate(chats):
        m_con.execute("INSERT INTO Name2Id VALUES (?,?,1)", (i + 1, u))

    chats_by_user: dict[str, list] = {u: [] for u, _, _, _ in chats}
    for m in messages:
        c_id, lid, ts, ltype, content = m
        if c_id in chats_by_user:
            chats_by_user[c_id].append((lid, ts, ltype, content))

    for u, msg_list in chats_by_user.items():
        tbl = "Msg_" + hashlib.md5(u.encode("utf-8")).hexdigest()
        m_con.execute(
            f"CREATE TABLE [{tbl}](local_id INTEGER, server_id INTEGER, local_type INTEGER, "
            f"real_sender_id INTEGER, create_time INTEGER, message_content BLOB, compress_content BLOB)"
        )
        for lid, ts, ltype, content in msg_list:
            m_con.execute(
                f"INSERT INTO [{tbl}] VALUES (?, 1, ?, 1, ?, ?, NULL)",
                (lid, ltype, ts, content.encode("utf-8")),
            )
    m_con.commit()
    m_con.close()


class FakeWechatImage:
    @staticmethod
    def resolve_keys(*args, **kwargs):
        return MockImageKeys()

    @staticmethod
    def decrypt_dat(path, keys):
        return MockDecryptedImage(
            data=b"\xff\xd8\xff\xe0jpeg-data",
            fmt="jpg",
            meta=MockDatMeta(),
            quality="hd",
            width=800,
            height=600,
        )

    @staticmethod
    def quality_of(path):
        return "hd"

    @staticmethod
    def rank_variants(paths):
        return paths


class FakeWechatOcr:
    @staticmethod
    def ocr_image(data, providers=("rapidocr",), timeout=60):
        return None


class MediaBudgetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.vault = self.root / "vault"
        self.vault.mkdir(parents=True)
        self.attach_root = self.root / "attach"
        self.attach_root.mkdir(parents=True)
        self.cache_root = self.root / "cache"
        self.cache_root.mkdir(parents=True)
        self.chat = "budget_chat"
        self.chat_md5 = hashlib.md5(self.chat.encode()).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_image(self, asset_id: str, month: str = "2026-10") -> None:
        img_dir = self.attach_root / self.chat_md5 / month / "Img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / f"{asset_id}.dat").write_bytes(b"mock-dat")

    def _run_media(self, policy_data: dict) -> int:
        policy_path = self.vault / "config" / "media_policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        full_policy = {
            "enabled": True,
            "chat_allowlist": [self.chat],
            "kinds": {"file": False, "image": True, "voice": False, "video": False},
            "wechat_attach_root": str(self.attach_root),
            "wechat_cache_root": str(self.cache_root),
            "wechat_account_dir": str(self.root),
            "wxid": "test_wxid",
            "image": {"enabled": True, "ocr": {"enabled": False}},
        }
        full_policy.update(policy_data)
        policy_path.write_text(json.dumps(full_policy), encoding="utf-8")

        old_img = sys.modules.get("wechat_image")
        old_ocr = sys.modules.get("wechat_ocr")
        sys.modules["wechat_image"] = FakeWechatImage()
        sys.modules["wechat_ocr"] = FakeWechatOcr()
        try:
            old_argv = sys.argv
            sys.argv = ["wechat_media.py", "--vault", str(self.vault), "--policy", str(policy_path)]
            return wechat_media.main()
        finally:
            sys.argv = old_argv
            if old_img is not None:
                sys.modules["wechat_image"] = old_img
            else:
                sys.modules.pop("wechat_image", None)
            if old_ocr is not None:
                sys.modules["wechat_ocr"] = old_ocr
            else:
                sys.modules.pop("wechat_ocr", None)

    def test_file_exhaustion_does_not_block_image(self):
        """Cross-kind isolation: file budget is exhausted, images MUST still process.

        Under the old bug, images read `daily['parsed'] >= budget` (where budget was
        the file budget), so if 30 files were parsed today, NO images could ever run.
        This test seeds a ledger where today's file budget is full (30/30) and asserts
        that an incoming image is still decrypted and marked ready.
        """
        asset_id = "aaa11122233344455566677788899900"
        self._setup_image(asset_id)

        now = int(time.time())
        vault_decrypted = self.vault / "decrypted" / "current"
        _build_test_databases(
            vault_decrypted,
            chats=[(self.chat, "群", "群", True)],
            messages=[(self.chat, 1, now - 100, 3, f'<msg><img md5="{asset_id}"/></msg>')],
        )

        today = time.strftime("%Y-%m-%d")
        ledger_path = self.vault / "state" / "media_ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Seed: file budget is completely exhausted, image budget untouched.
        # parsed=30 simulates the legacy aggregate field.
        initial_ledger = {
            "version": 1,
            "assets": {},
            "by_message": {},
            "anchors": {},
            "daily": {
                today: {
                    "parsed": 30,
                    "skipped": 0,
                    "bytes": 1000000,
                    "by_kind": {"file": 30, "image": 0, "video": 0},
                }
            },
        }
        ledger_path.write_text(json.dumps(initial_ledger), encoding="utf-8")

        rc = self._run_media({
            "daily_budget": {"file": 30, "image": 2000, "video": 50},
        })
        self.assertEqual(rc, 0)

        ledger = wechat_media.load_ledger(ledger_path)
        # Image MUST be ready despite file budget being full.
        asset = ledger["assets"].get(asset_id)
        self.assertIsNotNone(asset, "asset must be processed")
        self.assertEqual(asset["state"], wechat_media.STATE_READY)
        # by_kind must count image independently; file counter unchanged.
        today_daily = ledger["daily"][today]
        self.assertEqual(today_daily["by_kind"]["file"], 30)
        self.assertEqual(today_daily["by_kind"]["image"], 1)

    def test_image_budget_cap_defers_excess(self):
        """Image budget cap: 3rd image is deferred when budget.image=2.

        Asserts that:
        - Image 1 and 2 become STATE_READY
        - Image 3 is recorded in `budget_blocked` with deny_reason='image_budget_exhausted'
        - by_kind['image'] stops at 2 (the cap)
        """
        id1 = "11111111111111111111111111111111"
        id2 = "22222222222222222222222222222222"
        id3 = "33333333333333333333333333333333"
        for a in (id1, id2, id3):
            self._setup_image(a)

        now = int(time.time())
        vault_decrypted = self.vault / "decrypted" / "current"
        _build_test_databases(
            vault_decrypted,
            chats=[(self.chat, "群", "群", True)],
            messages=[
                # Chronological: 3 then 2 then 1 (iteration is DESC so 1 runs first)
                (self.chat, 1, now - 300, 3, f'<msg><img md5="{id1}"/></msg>'),
                (self.chat, 2, now - 200, 3, f'<msg><img md5="{id2}"/></msg>'),
                (self.chat, 3, now - 100, 3, f'<msg><img md5="{id3}"/></msg>'),
            ],
        )

        rc = self._run_media({
            "daily_budget": {"file": 30, "image": 2, "video": 50},
            "max_items_per_run": 10,
        })
        self.assertEqual(rc, 0)

        ledger = wechat_media.load_ledger(self.vault / "state" / "media_ledger.json")
        today = time.strftime("%Y-%m-%d")
        today_daily = ledger["daily"][today]
        self.assertEqual(today_daily["by_kind"]["image"], 2)

        # First two processed (descending ct: local_id 3 then 2 run first)
        ready_assets = [k for k, v in ledger["assets"].items() if v.get("state") == wechat_media.STATE_READY]
        self.assertEqual(len(ready_assets), 2)

        # 3rd is deferred, recorded with deny_reason
        blocked = ledger.get("budget_blocked", {})
        self.assertGreaterEqual(len(blocked), 1)
        reasons = [e.get("deny_reason") for e in blocked.values()]
        self.assertIn("image_budget_exhausted", reasons)

    def test_legacy_policy_compatibility(self):
        """Legacy policy with only `daily_file_budget` maps to file budget properly."""
        p = {"daily_file_budget": 42}
        budgets = wechat_media.resolve_kind_budgets(p)
        self.assertEqual(budgets["file"], 42)
        self.assertEqual(budgets["image"], 2000)
        self.assertEqual(budgets["video"], 50)

    def test_legacy_ledger_compatibility(self):
        """Legacy ledger without `by_kind` is read as 0, never crashes, no backfill."""
        daily = {"parsed": 30, "skipped": 5, "bytes": 1000}
        self.assertEqual(wechat_media.kind_used(daily, "file"), 0)
        self.assertEqual(wechat_media.kind_used(daily, "image"), 0)
        self.assertEqual(wechat_media.kind_used(daily, "video"), 0)
        # After charge, by_kind is created and aggregate is maintained
        wechat_media.charge_kind(daily, "file", amount=1, nbytes=500)
        self.assertEqual(wechat_media.kind_used(daily, "file"), 1)
        self.assertEqual(daily["parsed"], 31)
        self.assertEqual(daily["bytes"], 1500)


if __name__ == "__main__":
    unittest.main()
