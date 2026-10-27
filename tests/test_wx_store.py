"""Tests for wx_store module."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

import wx_store


class WxStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_migrate_idempotency_and_preservation(self):
        # 1. 缺 schema 时补 {"schema": 1}，保留全部未知键与既有键
        old = {
            "version": 1,
            "custom_field": "keep_me",
            "assets": {"a1": {"md5": "a1"}},
        }
        migrated = wx_store.migrate(old)
        self.assertEqual(migrated["schema"], 1)
        self.assertEqual(migrated["version"], 1)
        self.assertEqual(migrated["custom_field"], "keep_me")
        self.assertEqual(migrated["assets"], {"a1": {"md5": "a1"}})

        # 2. 幂等：再次 migrate 保持原状
        migrated_again = wx_store.migrate(migrated)
        self.assertEqual(migrated_again, migrated)

        # 3. 既有 schema 字段不被覆盖
        custom_schema = {"schema": 2, "extra": 123}
        self.assertEqual(wx_store.migrate(custom_schema)["schema"], 2)

        # 4. 非 dict 容错
        self.assertEqual(wx_store.migrate(None)["schema"], 1)

    def test_load_ledger_missing_corrupt_and_valid(self):
        # 1. 文件不存在 -> 返回默认结构
        p_missing = self.root / "missing.json"
        led1 = wx_store.load_ledger(p_missing)
        self.assertEqual(led1["schema"], 1)
        self.assertEqual(led1["version"], 1)
        self.assertEqual(led1["watermark"], 0)
        self.assertEqual(led1["assets"], {})
        self.assertEqual(led1["by_message"], {})
        self.assertEqual(led1["anchors"], {})
        self.assertEqual(led1["daily"], {})

        # 2. 文件损坏 -> 容错并返回默认结构
        p_bad = self.root / "corrupt.json"
        p_bad.write_text("{invalid json", encoding="utf-8")
        led2 = wx_store.load_ledger(p_bad)
        self.assertEqual(led2["schema"], 1)
        self.assertEqual(led2["assets"], {})

        # 3. 正常读取旧文件并自动补齐 schema 与默认键
        p_old = self.root / "old.json"
        p_old.write_text(
            json.dumps({"version": 1, "assets": {"x": 1}}), encoding="utf-8"
        )
        led3 = wx_store.load_ledger(p_old)
        self.assertEqual(led3["schema"], 1)
        self.assertEqual(led3["assets"], {"x": 1})
        self.assertEqual(led3["by_message"], {})
        self.assertEqual(led3["anchors"], {})
        self.assertEqual(led3["daily"], {})

    def test_save_ledger_atomic_and_permissions(self):
        p_target = self.root / "sub" / "ledger.json"
        data = {
            "version": 1,
            "assets": {"asset_01": {"state": "ready"}},
        }
        wx_store.save_ledger_atomic(p_target, data)
        self.assertTrue(p_target.is_file())

        # 校验自动补齐 schema
        saved = json.loads(p_target.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], 1)
        self.assertEqual(saved["assets"]["asset_01"]["state"], "ready")

        # 校验权限 0o600
        mode = stat.S_IMODE(p_target.stat().st_mode)
        self.assertEqual(mode, 0o600)

        # 校验原子性：同目录下不残留临时文件
        parent_files = [f.name for f in p_target.parent.iterdir()]
        self.assertEqual(parent_files, ["ledger.json"])

    def test_asset_for_lookup_and_fallback(self):
        ledger = {
            "schema": 1,
            "assets": {
                "aid_111": {
                    "asset_id": "aid_111",
                    "state": "ready",
                    "kind": "image",
                },
                "aid_222": {
                    "asset_id": "aid_222",
                    "state": "skipped",
                    "kind": "video",
                },
            },
            "by_message": {
                "chat_a|1": "aid_111",
            },
            "anchors": {
                "chat_b|2": {
                    "asset_id": "aid_222",
                    "chat_id": "chat_b",
                    "local_id": 2,
                },
                "chat_c|3": {
                    "chat_id": "chat_c",
                    "local_id": 3,
                    "desc": "anchor_without_asset_id",
                },
            },
        }

        # 1. 主路径：by_message 命中
        self.assertEqual(
            wx_store.asset_for(ledger, "chat_a", 1),
            {"asset_id": "aid_111", "state": "ready", "kind": "image"},
        )
        self.assertEqual(
            wx_store.asset_for(ledger, "chat_a", "1"),
            {"asset_id": "aid_111", "state": "ready", "kind": "image"},
        )

        # 2. 回退路径：by_message 缺失，但 anchors 中有 asset_id
        self.assertEqual(
            wx_store.asset_for(ledger, "chat_b", 2),
            {"asset_id": "aid_222", "state": "skipped", "kind": "video"},
        )

        # 3. 回退路径：by_message 缺失，anchors 无 asset 对应，回退 anchor
        self.assertEqual(
            wx_store.asset_for(ledger, "chat_c", 3),
            {
                "chat_id": "chat_c",
                "local_id": 3,
                "desc": "anchor_without_asset_id",
            },
        )

        # 4. 未命中
        self.assertIsNone(wx_store.asset_for(ledger, "chat_none", 999))
        self.assertIsNone(wx_store.asset_for(None, "chat_a", 1))
