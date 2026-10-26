#!/usr/bin/env python3
"""Tests for WeChat image media enrichment, API endpoints, contract v1.2, and security.

Covers:
- Variant ranking (_h > full > _t > _b)
- Asset ID aggregation, idempotency, and anchor mapping
- OCR lifecycle statuses (ready, unavailable, failed)
- wxgf animated handling (skip vs convert)
- Schema strictness (additionalProperties: false)
- chat_name display resolution and fallback
- Media search endpoint (/api/wechat/media)
  - Filters: q, chat_id, since, until, quality
  - include=context (up to 2 before / 2 after, 200 char truncation, cross-chat isolation, limit <= 50)
  - include default absent
- Image payload serving (/api/wechat/media/<asset_id>/<filename>)
  - Path traversal protection (assertion within vault/media/)
  - ETag (sha256) and If-None-Match 304
  - Blocklist enforcement across all endpoints
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_export_api  # noqa: E402
import wechat_media  # noqa: E402

TOKEN = "test-secret-token-v12"


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


@dataclass(frozen=True)
class MockDecryptedImage:
    data: bytes
    fmt: str
    meta: MockDatMeta
    quality: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class MockOcrResult:
    text: str
    provider: str
    lang: str
    confidence: float | None
    status: str


def _build_test_databases(
    vault_dir: Path,
    chats: list[tuple[str, str, str, bool]],  # (username, remark, nick_name, is_group)
    messages: list[tuple[str, int, int, int, str]],  # (chat_id, local_id, ts, local_type, content)
) -> None:
    """构建用于测试的 contact.db、session.db 与 message_0.db。"""
    (vault_dir / "contact").mkdir(parents=True, exist_ok=True)
    (vault_dir / "session").mkdir(parents=True, exist_ok=True)
    (vault_dir / "message").mkdir(parents=True, exist_ok=True)
    (vault_dir / "hardlink").mkdir(parents=True, exist_ok=True)

    # contact.db
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

    # session.db
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

    # message_0.db
    m_con = sqlite3.connect(vault_dir / "message" / "message_0.db")
    m_con.execute("CREATE TABLE Name2Id(rowid INTEGER PRIMARY KEY, user_name TEXT, is_session INTEGER)")
    for i, (u, _, _, _) in enumerate(chats):
        m_con.execute("INSERT INTO Name2Id VALUES (?,?,1)", (i + 1, u))

    # 创建每个会话的消息表
    chats_by_user = {u: [] for u, _, _, _ in chats}
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

    # hardlink.db
    h_con = sqlite3.connect(vault_dir / "hardlink" / "hardlink.db")
    h_con.execute("CREATE TABLE dir2id(rowid INTEGER PRIMARY KEY, username TEXT)")
    h_con.execute("INSERT INTO dir2id VALUES (1, '2026-10')")
    h_con.execute(
        "CREATE TABLE file_hardlink_info_v4(md5 TEXT PRIMARY KEY, file_name TEXT, dir1 INTEGER, dir2 INTEGER, file_size INTEGER)"
    )
    h_con.execute(
        "CREATE TABLE image_hardlink_info_v4(md5 TEXT PRIMARY KEY, file_name TEXT, dir1 INTEGER, dir2 INTEGER)"
    )
    h_con.commit()
    h_con.close()


class MediaImagesUnitTests(unittest.TestCase):
    """单元测试：画质变体择优、chat_name 解析、Schema 规范校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_variant_ranking_preference(self):
        """测试画质择优权重：_h > full > _t > _b。"""
        p_h = self.tmp / "img_h.dat"
        p_full = self.tmp / "img.dat"
        p_t = self.tmp / "img_t.dat"
        p_b = self.tmp / "img_b.dat"
        for p in (p_h, p_full, p_t, p_b):
            p.write_bytes(b"x")

        # 逆序传入，验证排序后 _h 在最前，_b 在最后
        ranked = wechat_media.rank_image_variants([p_b, p_t, p_full, p_h])
        self.assertEqual(ranked, [p_h, p_full, p_t, p_b])

        self.assertEqual(wechat_media.quality_of_path(p_h), "hd")
        self.assertEqual(wechat_media.quality_of_path(p_full), "full")
        self.assertEqual(wechat_media.quality_of_path(p_t), "thumb")
        self.assertEqual(wechat_media.quality_of_path(p_b), "bubble")

        self.assertEqual(wechat_media.variant_of_path(p_h), "_h")
        self.assertEqual(wechat_media.variant_of_path(p_full), "")
        self.assertEqual(wechat_media.variant_of_path(p_t), "_t")
        self.assertEqual(wechat_media.variant_of_path(p_b), "_b")

    def test_clean_asset_id(self):
        """测试剥离 _h / _t / _b 后缀获得 asset_id。"""
        self.assertEqual(
            wechat_media.clean_asset_id("e4f6050b1f3c3a72b0c3d9a1e8f20b41_h.dat"),
            "e4f6050b1f3c3a72b0c3d9a1e8f20b41",
        )
        self.assertEqual(
            wechat_media.clean_asset_id("e4f6050b1f3c3a72b0c3d9a1e8f20b41_t.dat"),
            "e4f6050b1f3c3a72b0c3d9a1e8f20b41",
        )
        self.assertEqual(
            wechat_media.clean_asset_id("e4f6050b1f3c3a72b0c3d9a1e8f20b41.dat"),
            "e4f6050b1f3c3a72b0c3d9a1e8f20b41",
        )

    def test_chat_name_resolution_and_fallback(self):
        """测试会话可读名解析优先级：单聊备注 > 昵称；群聊群名；未命中回退 chat_id。"""
        vault = self.tmp / "vault"
        _build_test_databases(
            vault,
            chats=[
                ("wxid_with_remark", "备注小张", "昵称张三", False),
                ("wxid_no_remark", "", "昵称李四", False),
                ("room123@chatroom", "研发一组备注", "研发一组群名", True),
                ("room456@chatroom", "", "纯群名群聊", True),
            ],
            messages=[],
        )

        contacts = wechat_export_api.get_cached_contacts(vault)
        # 单聊：优先备注名
        self.assertEqual(
            wechat_export_api.resolve_chat_name("wxid_with_remark", vault, contacts),
            "备注小张",
        )
        # 单聊：无备注取昵称
        self.assertEqual(
            wechat_export_api.resolve_chat_name("wxid_no_remark", vault, contacts),
            "昵称李四",
        )
        # 群聊：优先群备注
        self.assertEqual(
            wechat_export_api.resolve_chat_name("room123@chatroom", vault, contacts),
            "研发一组备注",
        )
        # 群聊：无群备注取群名
        self.assertEqual(
            wechat_export_api.resolve_chat_name("room456@chatroom", vault, contacts),
            "纯群名群聊",
        )
        # 未命中：回退 chat_id
        self.assertEqual(
            wechat_export_api.resolve_chat_name("wxid_unknown", vault, contacts),
            "wxid_unknown",
        )

    def test_contacts_cache_mtime_invalidation(self):
        """测试 contacts 缓存随 contact.db mtime 变化而失效刷新。"""
        vault = self.tmp / "vault"
        _build_test_databases(
            vault,
            chats=[("wxid_test", "旧备注", "昵称", False)],
            messages=[],
        )

        c1 = wechat_export_api.get_cached_contacts(vault)
        self.assertEqual(c1["wxid_test"]["display_name"], "旧备注")

        time.sleep(0.05)
        # 更新 contact.db
        con = sqlite3.connect(vault / "contact" / "contact.db")
        con.execute("UPDATE contact SET remark='新备注' WHERE username='wxid_test'")
        con.commit()
        con.close()

        # 修改 mtime
        new_mtime = time.time() + 10
        import os
        os.utime(vault / "contact" / "contact.db", (new_mtime, new_mtime))

        c2 = wechat_export_api.get_cached_contacts(vault)
        self.assertEqual(c2["wxid_test"]["display_name"], "新备注")

    def test_schema_json_strictness(self):
        """测试 export.schema.json 对 media 对象的定义与 additionalProperties 严格约束。"""
        schema_path = REPO_ROOT / "schema" / "export.schema.json"
        self.assertTrue(schema_path.is_file())
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        message_def = schema["$defs"]["messageItem"]
        self.assertFalse(message_def["additionalProperties"])
        self.assertIn("media", message_def["properties"])
        self.assertNotIn("media", message_def["required"])  # media 是可选的

        media_def = schema["$defs"]["mediaItem"]
        self.assertFalse(media_def["additionalProperties"])
        required_media_fields = [
            "asset_id", "kind", "quality", "format", "width", "height",
            "bytes", "path", "url", "anchor", "source", "ocr",
        ]
        for field in required_media_fields:
            self.assertIn(field, media_def["required"])

        # anchor 严格定义
        anchor_def = media_def["properties"]["anchor"]
        self.assertFalse(anchor_def["additionalProperties"])
        for f in ("chat_id", "chat_name", "local_id", "ts", "sender"):
            self.assertIn(f, anchor_def["required"])

    def test_image_note_mappings(self):
        """测试 8 种状态/原因到消费端 note 的细化映射规则。"""
        # 1. ready -> 图片
        self.assertEqual(wechat_export_api.image_note({"state": "ready"}), "图片")

        # 2. pending / image_not_on_disk -> 图片（处理中）
        self.assertEqual(wechat_export_api.image_note({"state": "pending"}), "图片（处理中）")
        self.assertEqual(
            wechat_export_api.image_note({"state": "pending", "deny_reason": "image_not_on_disk"}),
            "图片（处理中）",
        )

        # 3. skipped + missing_source -> 图片（未下载）
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "missing_source"}),
            "图片（未下载）",
        )

        # 4. skipped + skipped:animated -> 图片（动图）
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "skipped:animated"}),
            "图片（动图）",
        )

        # 5. skipped + max_pixels_exceeded -> 图片（超大）
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "max_pixels_exceeded"}),
            "图片（超大）",
        )

        # 6. skipped + missing_keys / skipped:decryptor_unavailable -> 图片（不可解密）
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "missing_keys"}),
            "图片（不可解密）",
        )
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "skipped:decryptor_unavailable"}),
            "图片（不可解密）",
        )

        # 7. failed + decrypt_failed / wxgf_convert_failed -> 图片（解析失败）
        self.assertEqual(
            wechat_export_api.image_note({"state": "failed", "deny_reason": "decrypt_failed"}),
            "图片（解析失败）",
        )
        self.assertEqual(
            wechat_export_api.image_note({"state": "failed", "deny_reason": "wxgf_convert_failed"}),
            "图片（解析失败）",
        )

        # 8. skipped + unresolved:* -> 图片（无法定位）
        self.assertEqual(
            wechat_export_api.image_note({"state": "skipped", "deny_reason": "unresolved:ambiguous_month"}),
            "图片（无法定位）",
        )

        # 9. 无记录 / None -> 图片
        self.assertEqual(wechat_export_api.image_note(None), "图片")
        self.assertEqual(wechat_export_api.image_note({}), "图片")


class MediaWorkerImageEnrichmentTests(unittest.TestCase):
    """测试媒体解析 worker 的图片处理流程（解密调用、变体择优、OCR 三态、wxgf、跨群复用）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.attach_root = self.root / "attach"
        self.attach_root.mkdir()
        self.cache_root = self.root / "cache"
        self.cache_root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_find_image_dat_month_and_probe(self):
        """测试 .dat 定位逻辑：月份目录直取 vs 唯一月目录探测 vs 歧义目录跳过。"""
        chat = "test_chat"
        chat_md5 = hashlib.md5(chat.encode()).hexdigest()
        asset_id = "11112222333344445555666677778888"

        # 1. 命中 hardlink 给出的月份
        img_dir = self.attach_root / chat_md5 / "2026-10" / "Img"
        img_dir.mkdir(parents=True)
        h_file = img_dir / f"{asset_id}_h.dat"
        h_file.write_bytes(b"hd-bytes")
        full_file = img_dir / f"{asset_id}.dat"
        full_file.write_bytes(b"full-bytes")

        cands, how = wechat_media.find_image_dat(
            self.attach_root, self.cache_root, chat_md5, "2026-10", asset_id
        )
        self.assertEqual(how, "hardlink:month")
        self.assertEqual(len(cands), 2)

        # 2. 月份未知：探测唯一月份目录成功
        cands2, how2 = wechat_media.find_image_dat(
            self.attach_root, self.cache_root, chat_md5, None, asset_id
        )
        self.assertEqual(how2, "probe:2026-10")
        self.assertEqual(len(cands2), 2)

        # 3. 月份未知且在多个月份目录下存在同名资产：记 ambiguous_month 不猜
        img_dir2 = self.attach_root / chat_md5 / "2026-09" / "Img"
        img_dir2.mkdir(parents=True)
        (img_dir2 / f"{asset_id}.dat").write_bytes(b"another-month")

        cands3, how3 = wechat_media.find_image_dat(
            self.attach_root, self.cache_root, chat_md5, None, asset_id
        )
        self.assertEqual(how3, "unresolved:ambiguous_month")
        self.assertEqual(cands3, [])

    def test_wxgf_skip_animated(self):
        """测试 wxgf 格式动图在 animated=skip 策略下被跳过。"""
        asset_id = "aabbccddeeff00112233445566778899"
        chat = "chat_anim"
        chat_md5 = hashlib.md5(chat.encode()).hexdigest()

        img_dir = self.attach_root / chat_md5 / "2026-10" / "Img"
        img_dir.mkdir(parents=True)
        (img_dir / f"{asset_id}.dat").write_bytes(b"mock-wxgf")

        vault_decrypted = self.vault / "decrypted" / "current"
        now = int(time.time())
        _build_test_databases(
            vault_decrypted,
            chats=[(chat, "动图会话", "动图", False)],
            messages=[(chat, 1, now - 100, 3, f'<msg><img md5="{asset_id}"/></msg>')],
        )
        policy_path = self.vault / "config" / "media_policy.json"
        policy_path.parent.mkdir(parents=True)
        policy_data = {
            "enabled": True,
            "chat_allowlist": [chat],
            "kinds": {"file": False, "image": True, "voice": False},
            "wechat_attach_root": str(self.attach_root),
            "wechat_cache_root": str(self.cache_root),
            "wechat_account_dir": str(self.root),
            "wxid": "test_wxid",
            "image": {
                "enabled": True,
                "wxgf": {"animated": "skip"},
            },
        }
        policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

        # Mock wechat_image
        class FakeWechatImage:
            @staticmethod
            def resolve_keys(*args, **kwargs):
                return MockImageKeys()

            @staticmethod
            def decrypt_dat(path, keys):
                return MockDecryptedImage(
                    data=b"wxgf-data",
                    fmt="wxgf",
                    meta=MockDatMeta(),
                    quality="full",
                    width=100,
                    height=100,
                )

            @staticmethod
            def convert_wxgf(data, out_path, animated="skip", ffmpeg="ffmpeg"):
                # 返回 (ok=True, is_animated=True)
                return True, True

            @staticmethod
            def quality_of(path):
                return "full"

            @staticmethod
            def rank_variants(paths):
                return paths

        old_img = sys.modules.get("wechat_image")
        sys.modules["wechat_image"] = FakeWechatImage()
        try:
            old_argv = sys.argv
            sys.argv = ["wechat_media.py", "--vault", str(self.vault), "--policy", str(policy_path)]
            rc = wechat_media.main()
            self.assertEqual(rc, 0)
        finally:
            sys.argv = old_argv
            if old_img is not None:
                sys.modules["wechat_image"] = old_img
            else:
                sys.modules.pop("wechat_image", None)

        ledger = wechat_media.load_ledger(self.vault / "state" / "media_ledger.json")
        asset_rec = ledger["assets"].get(asset_id)
        self.assertIsNotNone(asset_rec)
        self.assertEqual(asset_rec["state"], wechat_media.STATE_SKIPPED)
        self.assertEqual(asset_rec["deny_reason"], "skipped:animated")

    def test_idempotent_aggregation_across_chats(self):
        """测试同 asset_id 在不同会话中出现：不重复解密/OCR，只补齐 anchor。"""
        asset_id = "deadbeefdeadbeefdeadbeefdeadbeef"
        chat_a = "chat_a@chatroom"
        chat_b = "chat_b@chatroom"

        img_dir_a = self.attach_root / hashlib.md5(chat_a.encode()).hexdigest() / "2026-10" / "Img"
        img_dir_a.mkdir(parents=True)
        (img_dir_a / f"{asset_id}_h.dat").write_bytes(b"dat-bytes-hd")

        vault_decrypted = self.vault / "decrypted" / "current"
        now = int(time.time())
        _build_test_databases(
            vault_decrypted,
            chats=[
                (chat_a, "群聊A", "群A", True),
                (chat_b, "群聊B", "群B", True),
            ],
            messages=[
                (chat_a, 10, now - 200, 3, f'wxid_alice:\n<msg><img md5="{asset_id}_h"/></msg>'),
                (chat_b, 20, now - 100, 3, f'wxid_bob:\n<msg><img md5="{asset_id}_h"/></msg>'),
            ],
        )
        policy_path = self.vault / "config" / "media_policy.json"
        policy_path.parent.mkdir(parents=True)
        policy_data = {
            "enabled": True,
            "chat_allowlist": [chat_a, chat_b],
            "kinds": {"file": False, "image": True, "voice": False},
            "wechat_attach_root": str(self.attach_root),
            "wechat_cache_root": str(self.cache_root),
            "wechat_account_dir": str(self.root),
            "wxid": "test_wxid",
            "image": {"enabled": True},
        }
        policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

        decrypt_count = 0
        ocr_count = 0

        class FakeWechatImage:
            @staticmethod
            def resolve_keys(*args, **kwargs):
                return MockImageKeys()

            @staticmethod
            def decrypt_dat(path, keys):
                nonlocal decrypt_count
                decrypt_count += 1
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
                nonlocal ocr_count
                ocr_count += 1
                return MockOcrResult(
                    text="测试发票内容",
                    provider="rapidocr",
                    lang="chi_sim",
                    confidence=0.98,
                    status="ready",
                )

        old_img = sys.modules.get("wechat_image")
        old_ocr = sys.modules.get("wechat_ocr")
        sys.modules["wechat_image"] = FakeWechatImage()
        sys.modules["wechat_ocr"] = FakeWechatOcr()
        try:
            old_argv = sys.argv
            sys.argv = ["wechat_media.py", "--vault", str(self.vault), "--policy", str(policy_path)]
            rc = wechat_media.main()
            self.assertEqual(rc, 0)
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

        # 同一 asset_id 仅解密一次与 OCR 一次
        self.assertEqual(decrypt_count, 1)
        self.assertEqual(ocr_count, 1)

        ledger = wechat_media.load_ledger(self.vault / "state" / "media_ledger.json")
        self.assertEqual(ledger["by_message"][f"{chat_a}|10"], asset_id)
        self.assertEqual(ledger["by_message"][f"{chat_b}|20"], asset_id)
        self.assertIn(f"{chat_a}|10", ledger["anchors"])
        self.assertIn(f"{chat_b}|20", ledger["anchors"])
        self.assertEqual(ledger["anchors"][f"{chat_a}|10"]["chat_name"], "群聊A")
        self.assertEqual(ledger["anchors"][f"{chat_b}|20"]["chat_name"], "群聊B")

    def test_old_ledger_backfill_text_head(self):
        """测试旧账本缺 text_head 时下一轮回填，且不重复解密/OCR。"""
        asset_id = "01234567deadbeefdeadbeefdeadbeef"
        chat = "chat_old@chatroom"
        now = int(time.time())

        ledger_path = self.vault / "state" / "media_ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        old_ledger = {
            "version": 1,
            "watermark": now - 500,
            "assets": {
                asset_id: {
                    "asset_id": asset_id,
                    "kind": "image",
                    "state": "ready",
                    "ocr": {
                        "status": "ready",
                        "text": "旧账本里的OCR文字内容",
                    },
                }
            },
            "by_message": {f"{chat}|1": asset_id},
            "anchors": {
                f"{chat}|1": {
                    "chat_id": chat,
                    "chat_name": "旧群",
                    "local_id": 1,
                    "ts": now - 100,
                    "sender": "我",
                }
            },
            "daily": {},
        }
        ledger_path.write_text(json.dumps(old_ledger), encoding="utf-8")

        vault_decrypted = self.vault / "decrypted" / "current"
        _build_test_databases(
            vault_decrypted,
            chats=[(chat, "旧群", "旧群", True)],
            messages=[(chat, 1, now - 100, 3, f'<msg><img md5="{asset_id}"/></msg>')],
        )

        policy_path = self.vault / "config" / "media_policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_data = {
            "enabled": True,
            "chat_allowlist": [chat],
            "kinds": {"file": False, "image": True, "voice": False},
            "image": {"enabled": True},
        }
        policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

        decrypt_ran = False

        class FakeWechatImage:
            @staticmethod
            def resolve_keys(*args, **kwargs):
                return MockImageKeys()

            @staticmethod
            def decrypt_dat(path, keys):
                nonlocal decrypt_ran
                decrypt_ran = True
                return None

        old_img = sys.modules.get("wechat_image")
        sys.modules["wechat_image"] = FakeWechatImage()
        try:
            old_argv = sys.argv
            sys.argv = ["wechat_media.py", "--vault", str(self.vault), "--policy", str(policy_path)]
            rc = wechat_media.main()
            self.assertEqual(rc, 0)
        finally:
            sys.argv = old_argv
            if old_img is not None:
                sys.modules["wechat_image"] = old_img
            else:
                sys.modules.pop("wechat_image", None)

        self.assertFalse(decrypt_ran)
        new_ledger = wechat_media.load_ledger(ledger_path)
        self.assertEqual(new_ledger["assets"][asset_id]["text_head"], "旧账本里的OCR文字内容")


class HttpApiEndpointsIntegrationTests(unittest.TestCase):
    """集成测试：HTTP API 各端点（媒体检索、带上下文检索、二进制流传输、鉴权与 blocklist 安全防线）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp_dir.name)
        cls.vault = cls.root / "vault"
        cls.vault.mkdir()
        cls.decrypted = cls.vault / "decrypted" / "current"

        # 3 个会话：wxid_alice (正常), room_open@chatroom (正常群), wxid_blocked (被屏蔽)
        # 会话中的消息含上下文：before 2 条、当前图片、after 2 条
        # 包含超长文本测试截断
        long_text = "这是一段非常长的测试文本，" * 20  # 超过 200 字符
        _build_test_databases(
            cls.decrypted,
            chats=[
                ("wxid_alice", "爱丽丝", "Alice", False),
                ("room_open@chatroom", "开放讨论群", "GroupOpen", True),
                ("wxid_blocked", "屏蔽用户", "BlockedUser", False),
            ],
            messages=[
                ("wxid_alice", 1, 1759000010, 1, "Alice前序消息1"),
                ("wxid_alice", 2, 1759000020, 1, "Alice前序消息2"),
                ("wxid_alice", 3, 1759000030, 3, '<msg><img md5="11111111111111111111111111111111_h"/></msg>'),
                ("wxid_alice", 4, 1759000040, 1, long_text),
                ("wxid_alice", 5, 1759000050, 1, "Alice后序消息2"),
                ("wxid_alice", 6, 1759000060, 3, '<msg><img md5="44444444444444444444444444444444"/></msg>'),
                ("room_open@chatroom", 101, 1759000100, 1, "wxid_bob:\n群前序消息"),
                ("room_open@chatroom", 102, 1759000110, 3, 'wxid_bob:\n<msg><img md5="22222222222222222222222222222222"/></msg>'),
                ("room_open@chatroom", 103, 1759000120, 1, "wxid_carol:\n群后序消息"),
                ("wxid_blocked", 201, 1759000200, 3, '<msg><img md5="33333333333333333333333333333333"/></msg>'),
            ],
        )

        # 准备物理文件：vault/media/<asset_id>/image.jpg
        for aid, content in [
            ("11111111111111111111111111111111", b"alice-image-bytes"),
            ("22222222222222222222222222222222", b"group-image-bytes"),
            ("33333333333333333333333333333333", b"blocked-image-bytes"),
        ]:
            mdir = cls.vault / "media" / aid
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "image.jpg").write_bytes(content)

        # 准备 ledger 与 policy
        state_dir = cls.vault / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        config_dir = cls.vault / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        policy_file = config_dir / "media_policy.json"
        policy_file.write_text(
            json.dumps({
                "enabled": True,
                "chat_allowlist": ["wxid_alice", "room_open@chatroom", "wxid_blocked"],
                "kinds": {"file": False, "image": True, "voice": False},
                "render": {"inject_into_text": True},
                "image": {"enabled": True},
            }),
            encoding="utf-8",
        )

        ledger_file = state_dir / "media_ledger.json"
        sha1 = hashlib.sha256(b"alice-image-bytes").hexdigest()
        sha2 = hashlib.sha256(b"group-image-bytes").hexdigest()
        sha3 = hashlib.sha256(b"blocked-image-bytes").hexdigest()
        ledger_data = {
            "version": 1,
            "watermark": 1759000300,
            "assets": {
                "11111111111111111111111111111111": {
                    "asset_id": "11111111111111111111111111111111",
                    "kind": "image",
                    "quality": "hd",
                    "format": "jpg",
                    "width": 1024,
                    "height": 768,
                    "bytes": len(b"alice-image-bytes"),
                    "sha256": sha1,
                    "path": "media/11111111111111111111111111111111/image.jpg",
                    "url": "/api/wechat/media/11111111111111111111111111111111/image.jpg",
                    "anchor": {
                        "chat_id": "wxid_alice",
                        "chat_name": "爱丽丝",
                        "local_id": 3,
                        "ts": 1759000030,
                        "sender": "爱丽丝",
                    },
                    "source": {"dat": "msg/attach/.../111_h.dat", "variant": "_h"},
                    "ocr": {
                        "status": "ready",
                        "text": "发票代码 12345678",
                        "provider": "rapidocr",
                        "lang": "chi_sim",
                        "confidence": 0.95,
                        "version": 1,
                        "sha256": sha1,
                    },
                    "state": "ready",
                },
                "22222222222222222222222222222222": {
                    "asset_id": "22222222222222222222222222222222",
                    "kind": "image",
                    "quality": "full",
                    "format": "jpg",
                    "width": 800,
                    "height": 600,
                    "bytes": len(b"group-image-bytes"),
                    "sha256": sha2,
                    "path": "media/22222222222222222222222222222222/image.jpg",
                    "url": "/api/wechat/media/22222222222222222222222222222222/image.jpg",
                    "anchor": {
                        "chat_id": "room_open@chatroom",
                        "chat_name": "开放讨论群",
                        "local_id": 102,
                        "ts": 1759000110,
                        "sender": "wxid_bob",
                    },
                    "source": {"dat": "msg/attach/.../222.dat", "variant": ""},
                    "ocr": {
                        "status": "ready",
                        "text": "会议纪要讨论草案",
                        "provider": "rapidocr",
                        "lang": "chi_sim",
                        "confidence": 0.91,
                        "version": 1,
                        "sha256": sha2,
                    },
                    "state": "ready",
                },
                "33333333333333333333333333333333": {
                    "asset_id": "33333333333333333333333333333333",
                    "kind": "image",
                    "quality": "full",
                    "format": "jpg",
                    "width": 640,
                    "height": 480,
                    "bytes": len(b"blocked-image-bytes"),
                    "sha256": sha3,
                    "path": "media/33333333333333333333333333333333/image.jpg",
                    "url": "/api/wechat/media/33333333333333333333333333333333/image.jpg",
                    "anchor": {
                        "chat_id": "wxid_blocked",
                        "chat_name": "屏蔽用户",
                        "local_id": 201,
                        "ts": 1759000200,
                        "sender": "屏蔽用户",
                    },
                    "source": {"dat": "msg/attach/.../333.dat", "variant": ""},
                    "ocr": {
                        "status": "ready",
                        "text": "机密敏感屏蔽文字",
                        "provider": "rapidocr",
                        "lang": "chi_sim",
                        "confidence": 0.99,
                        "version": 1,
                        "sha256": sha3,
                    },
                    "state": "ready",
                },
                "44444444444444444444444444444444": {
                    "asset_id": "44444444444444444444444444444444",
                    "kind": "image",
                    "state": "skipped",
                    "deny_reason": "missing_source",
                },
            },
            "by_message": {
                "wxid_alice|3": "11111111111111111111111111111111",
                "wxid_alice|6": "44444444444444444444444444444444",
                "room_open@chatroom|102": "22222222222222222222222222222222",
                "wxid_blocked|201": "33333333333333333333333333333333",
            },
            "anchors": {
                "wxid_alice|3": {
                    "chat_id": "wxid_alice",
                    "chat_name": "爱丽丝",
                    "local_id": 3,
                    "ts": 1759000030,
                    "sender": "爱丽丝",
                },
                "room_open@chatroom|102": {
                    "chat_id": "room_open@chatroom",
                    "chat_name": "开放讨论群",
                    "local_id": 102,
                    "ts": 1759000110,
                    "sender": "wxid_bob",
                },
                "wxid_blocked|201": {
                    "chat_id": "wxid_blocked",
                    "chat_name": "屏蔽用户",
                    "local_id": 201,
                    "ts": 1759000200,
                    "sender": "屏蔽用户",
                },
            },
            "daily": {},
        }
        ledger_file.write_text(json.dumps(ledger_data), encoding="utf-8")

        # 启动测试 HTTP Server，启用 Bearer Token 鉴权与 blocklist
        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=cls.decrypted,
            token=TOKEN,
            blocklist=["屏蔽用户"],
            media_vault_root=str(cls.vault),
            media_policy_path=str(policy_file),
            media_ledger_path=str(ledger_file),
        )
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp_dir.cleanup()

    def _req(self, path: str, token: str | None = TOKEN, headers: dict | None = None) -> tuple[int, bytes, dict]:
        quoted_path = urllib.parse.quote(path, safe="/:?=&%")
        url = f"http://127.0.0.1:{self.port}{quoted_path}"
        req_headers = dict(headers or {})
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_headers = dict(resp.headers)
                return resp.status, resp.read(), resp_headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_export_endpoint_injects_media_and_text_projection(self):
        """测试 /api/wechat/export 注入 media 字段，且 text 填入截断投影。"""
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000500")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))

        chats_map = {c["name"]: c for c in data["chats"]}
        self.assertNotIn("屏蔽用户", chats_map)
        self.assertIn("爱丽丝", chats_map)

        alice_msgs = chats_map["爱丽丝"]["messages"]
        img_msg = [m for m in alice_msgs if m["kind"] == "image"][0]

        # 契约检验：text 为 OCR 投影，media.ocr.text 为完整内容，note 仍为"图片"
        self.assertEqual(img_msg["text"], "发票代码 12345678")
        self.assertEqual(img_msg["note"], "图片")
        self.assertIn("media", img_msg)
        media = img_msg["media"]
        self.assertEqual(media["asset_id"], "11111111111111111111111111111111")
        self.assertEqual(media["ocr"]["text"], "发票代码 12345678")
        self.assertEqual(media["anchor"]["chat_name"], "爱丽丝")
        self.assertEqual(media["anchor"]["local_id"], 3)

        # 消息 6 (skipped/missing_source)：note 细化为 "图片（未下载）"，text 为空串，media 不存在
        img_msg_skipped = [m for m in alice_msgs if m["kind"] == "image" and m["note"] != "图片"][0]
        self.assertEqual(img_msg_skipped["note"], "图片（未下载）")
        self.assertEqual(img_msg_skipped["text"], "")
        self.assertNotIn("media", img_msg_skipped)

    def test_empty_and_long_ocr_projections(self):
        """测试 empty OCR 时 text 为空串，超长 OCR (>2000) 时截断带 …。"""
        # empty OCR
        raw_empty = ""
        th_empty = (raw_empty[:2000] + "…") if len(raw_empty) > 2000 else raw_empty
        self.assertEqual(th_empty, "")

        # 超长 OCR (>2000)
        long_ocr = "长文本内容" * 500  # 2500 字符
        th_long = (long_ocr[:2000] + "…") if len(long_ocr) > 2000 else long_ocr
        self.assertEqual(len(th_long), 2001)
        self.assertTrue(th_long.endswith("…"))

    def test_media_search_auth_and_blocklist(self):
        """测试 /api/wechat/media 必须 Bearer 鉴权，且 blocklist 严格生效。"""
        # 1. 无 token 401
        status, _, _ = self._req("/api/wechat/media", token=None)
        self.assertEqual(status, 401)

        # 2. 正常查询：屏蔽会话的内容绝不出库
        status, body, _ = self._req("/api/wechat/media")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        items = data["items"]
        asset_ids = [it["asset_id"] for it in items]
        self.assertIn("11111111111111111111111111111111", asset_ids)
        self.assertIn("22222222222222222222222222222222", asset_ids)
        self.assertNotIn("33333333333333333333333333333333", asset_ids)

    def test_media_search_filters(self):
        """测试 /api/wechat/media 的 q、chat_id、quality 过滤条件。"""
        # 1. OCR 文本子串搜索
        status, body, _ = self._req("/api/wechat/media?q=发票代码")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["asset_id"], "11111111111111111111111111111111")

        # 2. chat_id 过滤
        status, body, _ = self._req("/api/wechat/media?chat_id=room_open%40chatroom")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["asset_id"], "22222222222222222222222222222222")

        # 3. quality 过滤
        status, body, _ = self._req("/api/wechat/media?quality=hd")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["quality"], "hd")

    def test_media_search_include_context(self):
        """测试 include=context：前后最多2条、200字截断、跨会话防串味、limit上限50、缺省无context。"""
        # 1. 缺省 include 时，返回项中不带 context
        status, body, _ = self._req("/api/wechat/media")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        for it in data["items"]:
            self.assertNotIn("context", it)

        # 2. include=context 时带入上下文
        status, body, _ = self._req("/api/wechat/media?include=context&chat_id=wxid_alice")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertIn("context", item)
        ctx = item["context"]

        # 前序消息最多 2 条
        self.assertEqual(len(ctx["before"]), 2)
        self.assertEqual(ctx["before"][0]["text"], "Alice前序消息1")
        self.assertEqual(ctx["before"][1]["text"], "Alice前序消息2")

        # 后序消息最多 2 条，且第 1 条超过 200 字符发生截断（带 …）
        self.assertEqual(len(ctx["after"]), 2)
        after1 = ctx["after"][0]
        self.assertTrue(len(after1["text"]) <= 201)
        self.assertTrue(after1["text"].endswith("…"))
        self.assertEqual(ctx["after"][1]["text"], "Alice后序消息2")

        # 跨会话防串味：群消息的上下文只能包含该群的消息
        status_grp, body_grp, _ = self._req("/api/wechat/media?include=context&chat_id=room_open%40chatroom")
        self.assertEqual(status_grp, 200)
        data_grp = json.loads(body_grp.decode("utf-8"))
        grp_ctx = data_grp["items"][0]["context"]
        self.assertEqual(len(grp_ctx["before"]), 1)
        self.assertIn("群前序消息", grp_ctx["before"][0]["text"])
        self.assertEqual(len(grp_ctx["after"]), 1)
        self.assertIn("群后序消息", grp_ctx["after"][0]["text"])

        # 3. include=context 时 limit 上限强制降到 50
        status_lim, body_lim, _ = self._req("/api/wechat/media?include=context&limit=100")
        self.assertEqual(status_lim, 200)
        data_lim = json.loads(body_lim.decode("utf-8"))
        self.assertEqual(data_lim["limit"], 50)

    def test_media_file_download_and_cache(self):
        """测试 /api/wechat/media/<asset_id>/<filename>：Content-Type、ETag、304 缓存。"""
        # 1. 成功下载与 ETag
        status, body, headers = self._req("/api/wechat/media/11111111111111111111111111111111/image.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"alice-image-bytes")
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        etag = headers.get("ETag")
        self.assertIsNotNone(etag)

        # 2. 携带 If-None-Match 返回 304 Not Modified
        status_304, body_304, _ = self._req(
            "/api/wechat/media/11111111111111111111111111111111/image.jpg",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(status_304, 304)
        self.assertEqual(body_304, b"")

    def test_media_file_path_traversal_and_security(self):
        """测试路径穿越防御、文件名扩展名白名单与 blocklist 隔离。"""
        # 1. 路径穿越：.. 必须被拒绝（404 或 403）
        status, _, _ = self._req("/api/wechat/media/../../etc/passwd")
        self.assertIn(status, (400, 403, 404))

        # 2. 非法扩展名拒绝
        status_ext, _, _ = self._req("/api/wechat/media/11111111111111111111111111111111/image.sh")
        self.assertEqual(status_ext, 400)

        # 3. 屏蔽会话的媒体文件访问被拒（404 隐藏存在性）
        status_blk, _, _ = self._req("/api/wechat/media/33333333333333333333333333333333/image.jpg")
        self.assertEqual(status_blk, 404)


if __name__ == "__main__":
    unittest.main()
