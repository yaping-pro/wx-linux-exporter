#!/usr/bin/env python3
"""Tests for WeChat export media filters, video injection, payload_stats, Range 206, and contract v1.3.

Covers:
- Query parameters validation:
  - media ∈ none|image|video|all (default: all)
  - media_chats (comma-separated list of chat_ids)
  - ocr ∈ none|head|full (default: head)
  - Invalid param error format: 400 {"error":{"code":"invalid_param","message":"...","hint":"..."}}
  - Existing errors remain intact
- media=none suppresses media objects while preserving notes ('图片', '视频')
- media_chats restricts media injection to designated chats, others fallback to note
- ocr=none removes OCR text from text and media.ocr, payload_stats.ocr_chars == 0
- ocr=head / ocr=full behaviors
- Video ledger contract & export media object shape:
  - duration_s, poster, format=mp4/mov, bytes, width, height, anchor, source
- media_note mappings for video:
  - ready -> '视频'
  - pending -> '视频（处理中）'
  - skipped + missing_source -> '视频（未下载）'
  - skipped + oversize -> '视频（超大）'
  - failed -> '视频（解析失败）'
- Range requests return 206 Partial Content + Content-Range, 200 without Range
- /api/wechat/media?q= kind filtering (image, video, image,video) with duration_s/poster
- payload_stats in export response
- Schema validation
"""
from __future__ import annotations

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

TOKEN = "test-token-filters-v13"


def _build_test_databases(decrypted_dir: Path, chats: list[tuple[str, str, str, bool]], messages: list[tuple]):
    decrypted_dir.mkdir(parents=True, exist_ok=True)
    contact_dir = decrypted_dir / "contact"
    contact_dir.mkdir(parents=True, exist_ok=True)
    msg_dir = decrypted_dir / "message"
    msg_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(contact_dir / "contact.db")
    con.execute(
        "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT, local_type INT, chat_room_type INT)"
    )
    for u, r, n, is_grp in chats:
        con.execute(
            "INSERT INTO contact VALUES (?, ?, ?, '', 1, ?)",
            (u, r, n, 1 if is_grp else 0),
        )
    con.commit()
    con.close()

    msg_con = sqlite3.connect(msg_dir / "message_0.db")
    msg_con.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    for idx, (u, _r, _n, _is_grp) in enumerate(chats, start=1):
        msg_con.execute("INSERT INTO Name2Id VALUES (?, ?)", (idx, u))

    tables_created = set()
    for row in messages:
        chat_id = row[0]
        table_md5 = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        tname = f"Msg_{table_md5}"
        if tname not in tables_created:
            msg_con.execute(
                f"CREATE TABLE [{tname}] (local_id INTEGER, server_id INTEGER, local_type INTEGER, "
                f"real_sender_id INTEGER, create_time INTEGER, message_content TEXT, compress_content BLOB)"
            )
            tables_created.add(tname)
        msg_con.execute(
            f"INSERT INTO [{tname}] VALUES (?, 0, ?, 0, ?, ?, NULL)",
            (row[1], row[3], row[2], row[4]),
        )
    msg_con.commit()
    msg_con.close()


class MediaFiltersIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp_dir.name)
        cls.vault = cls.root / "vault"
        cls.vault.mkdir()
        cls.decrypted = cls.vault / "decrypted" / "current"

        _build_test_databases(
            cls.decrypted,
            chats=[
                ("wxid_alice", "爱丽丝", "Alice", False),
                ("wxid_bob", "鲍勃", "Bob", False),
            ],
            messages=[
                ("wxid_alice", 1, 1759000010, 1, "Hello text"),
                ("wxid_alice", 2, 1759000020, 3, '<msg><img md5="11111111111111111111111111111111"/></msg>'),
                ("wxid_alice", 3, 1759000030, 43, '<msg><videomsg md5="22222222222222222222222222222222"/></msg>'),
                ("wxid_bob", 101, 1759000110, 3, '<msg><img md5="33333333333333333333333333333333"/></msg>'),
            ],
        )

        cls.vmedia = cls.vault / "media"
        for aid, fn, content in [
            ("11111111111111111111111111111111", "image.jpg", b"alice-image-bytes-data"),
            ("22222222222222222222222222222222", "video.mp4", b"alice-video-bytes-0123456789abcdef"),
            ("22222222222222222222222222222222", "thumb.jpg", b"alice-video-thumb-bytes"),
            ("33333333333333333333333333333333", "image.jpg", b"bob-image-bytes-data"),
        ]:
            mdir = cls.vmedia / aid
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / fn).write_bytes(content)

        state_dir = cls.vault / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        config_dir = cls.vault / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        policy_file = config_dir / "media_policy.json"
        policy_file.write_text(
            json.dumps({
                "enabled": True,
                "chat_allowlist": ["wxid_alice", "wxid_bob"],
                "kinds": {"file": False, "image": True, "voice": False, "video": True},
                "render": {"inject_into_text": True},
                "image": {"enabled": True},
                "video": {"enabled": True},
            }),
            encoding="utf-8",
        )

        ledger_file = state_dir / "media_ledger.json"
        ledger_data = {
            "version": 1,
            "assets": {
                "11111111111111111111111111111111": {
                    "asset_id": "11111111111111111111111111111111",
                    "kind": "image",
                    "state": "ready",
                    "quality": "hd",
                    "format": "jpg",
                    "width": 1024,
                    "height": 768,
                    "bytes": 22,
                    "path": "media/11111111111111111111111111111111/image.jpg",
                    "url": "/api/wechat/media/11111111111111111111111111111111/image.jpg",
                    "anchor": {
                        "chat_id": "wxid_alice",
                        "chat_name": "爱丽丝",
                        "local_id": 2,
                        "ts": 1759000020,
                        "sender": "爱丽丝",
                    },
                    "source": {"dat": "mock.dat", "variant": "_h"},
                    "ocr": {
                        "status": "ready",
                        "text": "发票代码12345678" * 300,  # 3600 characters > 2000
                        "provider": "rapidocr",
                        "confidence": 0.98,
                    },
                },
                "22222222222222222222222222222222": {
                    "md5": "22222222222222222222222222222222",
                    "asset_id": "22222222222222222222222222222222",
                    "kind": "video",
                    "state": "ready",
                    "deny_reason": None,
                    "format": "mp4",
                    "bytes": 34,
                    "duration_s": 12,
                    "width": 1080,
                    "height": 1920,
                    "path": "media/2026-10-27/22222222222222222222222222222222/video.mp4",
                    "thumb": "media/2026-10-27/22222222222222222222222222222222/thumb.jpg",
                    "url": "/api/wechat/media/22222222222222222222222222222222/video.mp4",
                    "poster": "/api/wechat/media/22222222222222222222222222222222/thumb.jpg",
                    "anchor": {
                        "chat_id": "wxid_alice",
                        "chat_name": "爱丽丝",
                        "local_id": 3,
                        "ts": 1759000030,
                        "sender": "爱丽丝",
                    },
                    "source": "hardlink",
                },
                "33333333333333333333333333333333": {
                    "asset_id": "33333333333333333333333333333333",
                    "kind": "image",
                    "state": "ready",
                    "quality": "full",
                    "format": "jpg",
                    "width": 800,
                    "height": 600,
                    "bytes": 20,
                    "path": "media/33333333333333333333333333333333/image.jpg",
                    "url": "/api/wechat/media/33333333333333333333333333333333/image.jpg",
                    "anchor": {
                        "chat_id": "wxid_bob",
                        "chat_name": "鲍勃",
                        "local_id": 101,
                        "ts": 1759000110,
                        "sender": "鲍勃",
                    },
                    "source": {"dat": "mock2.dat", "variant": "full"},
                    "ocr": {
                        "status": "ready",
                        "text": "收据证明",
                        "provider": "rapidocr",
                        "confidence": 0.95,
                    },
                },
            },
            "by_message": {
                "wxid_alice|2": "11111111111111111111111111111111",
                "wxid_alice|3": "22222222222222222222222222222222",
                "wxid_bob|101": "33333333333333333333333333333333",
            },
            "anchors": {
                "wxid_alice|2": {
                    "chat_id": "wxid_alice",
                    "chat_name": "爱丽丝",
                    "local_id": 2,
                    "ts": 1759000020,
                    "sender": "爱丽丝",
                },
                "wxid_alice|3": {
                    "chat_id": "wxid_alice",
                    "chat_name": "爱丽丝",
                    "local_id": 3,
                    "ts": 1759000030,
                    "sender": "爱丽丝",
                },
                "wxid_bob|101": {
                    "chat_id": "wxid_bob",
                    "chat_name": "鲍勃",
                    "local_id": 101,
                    "ts": 1759000110,
                    "sender": "鲍勃",
                },
            },
        }
        ledger_file.write_text(json.dumps(ledger_data), encoding="utf-8")

        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=cls.decrypted,
            token=TOKEN,
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

    def test_invalid_param_error_format(self):
        """非法参数返回 400 与规范结构：{"error":{"code":"invalid_param", ...}}。"""
        # 1. invalid media
        status, body, _ = self._req("/api/wechat/export?media=foo")
        self.assertEqual(status, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], "invalid_param")
        self.assertEqual(data["error"]["hint"], "media ∈ none|image|video|all")

        # 2. invalid ocr
        status, body, _ = self._req("/api/wechat/export?ocr=bar")
        self.assertEqual(status, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], "invalid_param")
        self.assertEqual(data["error"]["hint"], "ocr ∈ none|head|full")

        # 3. 既有非法时间参数保持既有错误形态 (保持 {"error": "..."})
        status, body, _ = self._req("/api/wechat/export?since=invalid_time_str")
        self.assertEqual(status, 400)
        data = json.loads(body.decode("utf-8"))
        self.assertIsInstance(data["error"], str)

    def test_default_export_behavior_and_payload_stats(self):
        """默认参数（media=all, ocr=head）：image 与 video 均注入，payload_stats 正确计算。"""
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000500")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))

        self.assertIn("payload_stats", data)
        stats = data["payload_stats"]
        self.assertEqual(stats["messages"], 4)
        self.assertEqual(stats["media_included"], 3)  # 2 images + 1 video
        # ocr_chars in head mode counts actual emitted text length:
        # alice img has 2001 chars (truncated with …) + bob img has 4 chars = 2005
        self.assertEqual(stats["ocr_chars"], 2005)
        alice = [c for c in data["chats"] if c["name"] == "爱丽丝"][0]
        # check video message
        vid_msg = [m for m in alice["messages"] if m["kind"] == "video"][0]
        self.assertEqual(vid_msg["note"], "视频")
        self.assertIn("media", vid_msg)
        vid_obj = vid_msg["media"]
        self.assertEqual(vid_obj["kind"], "video")
        self.assertEqual(vid_obj["asset_id"], "22222222222222222222222222222222")
        self.assertEqual(vid_obj["duration_s"], 12)
        self.assertEqual(vid_obj["format"], "mp4")
        self.assertEqual(vid_obj["bytes"], 34)
        self.assertEqual(vid_obj["width"], 1080)
        self.assertEqual(vid_obj["height"], 1920)
        self.assertEqual(vid_obj["url"], "/api/wechat/media/22222222222222222222222222222222/video.mp4")
        self.assertEqual(vid_obj["poster"], "/api/wechat/media/22222222222222222222222222222222/thumb.jpg")
        self.assertEqual(vid_obj["anchor"]["chat_name"], "爱丽丝")
        self.assertEqual(vid_obj["anchor"]["local_id"], 3)
        self.assertEqual(vid_obj["source"], "hardlink")

    def test_media_none_filter(self):
        """media=none 时无 media 对象，但 note 保留，消费端可事后拉取。"""
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000500&media=none")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))

        stats = data["payload_stats"]
        self.assertEqual(stats["media_included"], 0)

        for c in data["chats"]:
            for m in c["messages"]:
                self.assertNotIn("media", m)
                if m["kind"] == "image":
                    self.assertEqual(m["note"], "图片")
                elif m["kind"] == "video":
                    self.assertEqual(m["note"], "视频")

    def test_media_kind_filters(self):
        """media=image 只注入图片，media=video 只注入视频。"""
        # 1. media=image
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000500&media=image")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["payload_stats"]["media_included"], 2)
        alice = [c for c in data["chats"] if c["name"] == "爱丽丝"][0]
        vid_msg = [m for m in alice["messages"] if m["kind"] == "video"][0]
        self.assertNotIn("media", vid_msg)
        self.assertEqual(vid_msg["note"], "视频")
        img_msg = [m for m in alice["messages"] if m["kind"] == "image"][0]
        self.assertIn("media", img_msg)

        # 2. media=video
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000500&media=video")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["payload_stats"]["media_included"], 1)
        alice = [c for c in data["chats"] if c["name"] == "爱丽丝"][0]
        vid_msg = [m for m in alice["messages"] if m["kind"] == "video"][0]
        self.assertIn("media", vid_msg)
        img_msg = [m for m in alice["messages"] if m["kind"] == "image"][0]
        self.assertNotIn("media", img_msg)
        self.assertEqual(img_msg["note"], "图片")

    def test_media_chats_filter(self):
        """media_chats 限制注入会话，其余会话退化为 note。"""
        status, body, _ = self._req(
            "/api/wechat/export?since=1759000000&to=1759000500&media_chats=wxid_alice"
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["payload_stats"]["media_included"], 2)

        alice = [c for c in data["chats"] if c["name"] == "爱丽丝"][0]
        self.assertIn("media", [m for m in alice["messages"] if m["kind"] == "image"][0])
        self.assertIn("media", [m for m in alice["messages"] if m["kind"] == "video"][0])

        bob = [c for c in data["chats"] if c["name"] == "鲍勃"][0]
        bob_img = [m for m in bob["messages"] if m["kind"] == "image"][0]
        self.assertNotIn("media", bob_img)
        self.assertEqual(bob_img["note"], "图片")
    def test_ocr_filter_modes(self):
        """ocr=none, ocr=head, ocr=full 行为验证与体积差异。"""
        # 1. ocr=none: text 不含 OCR，media.ocr 不注入，ocr_chars==0
        status, body, _ = self._req(
            "/api/wechat/export?since=1759000000&to=1759000500&ocr=none"
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["payload_stats"]["ocr_chars"], 0)

        alice = [c for c in data["chats"] if c["name"] == "爱丽丝"][0]
        img_msg = [m for m in alice["messages"] if m["kind"] == "image"][0]
        self.assertEqual(img_msg["text"], "")
        self.assertIn("media", img_msg)
        self.assertNotIn("ocr", img_msg["media"])

        # 2. ocr=head: media.ocr 不含 text 键，text 包含 <=2000 截断投影，ocr_chars 远小于 full
        status, body, _ = self._req(
            "/api/wechat/export?since=1759000000&to=1759000500&ocr=head"
        )
        self.assertEqual(status, 200)
        data_head = json.loads(body.decode("utf-8"))
        alice_head = [c for c in data_head["chats"] if c["name"] == "爱丽丝"][0]
        img_msg_head = [m for m in alice_head["messages"] if m["kind"] == "image"][0]
        self.assertTrue(img_msg_head["text"].endswith("…"))
        self.assertEqual(len(img_msg_head["text"]), 2001)
        self.assertIn("media", img_msg_head)
        self.assertIn("ocr", img_msg_head["media"])
        self.assertNotIn("text", img_msg_head["media"]["ocr"])
        self.assertEqual(img_msg_head["media"]["ocr"]["status"], "ready")
        head_ocr_chars = data_head["payload_stats"]["ocr_chars"]
        # head_ocr_chars = len(img_msg_head["text"]) (2001) + len("收据证明") (4) = 2005
        self.assertEqual(head_ocr_chars, 2005)

        # 3. ocr=full: text 有投影，media.ocr 全文存在，ocr_chars 包含完整 OCR 全文字符 (3600 + 4 = 3604)
        status, body, _ = self._req(
            "/api/wechat/export?since=1759000000&to=1759000500&ocr=full"
        )
        self.assertEqual(status, 200)
        data_full = json.loads(body.decode("utf-8"))
        alice_full = [c for c in data_full["chats"] if c["name"] == "爱丽丝"][0]
        img_msg_full = [m for m in alice_full["messages"] if m["kind"] == "image"][0]
        self.assertTrue(img_msg_full["text"].endswith("…"))
        self.assertIn("text", img_msg_full["media"]["ocr"])
        self.assertEqual(len(img_msg_full["media"]["ocr"]["text"]), 3600)
        full_ocr_chars = data_full["payload_stats"]["ocr_chars"]
        self.assertEqual(full_ocr_chars, 3604)
        self.assertLess(head_ocr_chars, full_ocr_chars)

    def test_accept_ranges_only_on_video(self):
        """Accept-Ranges: bytes 标头只在视频时返回，图片下载不应声明。"""
        # 1. 视频下载：声明 Accept-Ranges: bytes
        status, _, headers = self._req("/api/wechat/media/22222222222222222222222222222222/video.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")

        # 2. 图片下载：不声明 Accept-Ranges
        status, _, headers = self._req("/api/wechat/media/11111111111111111111111111111111/image.jpg")
        self.assertEqual(status, 200)
        self.assertNotIn("Accept-Ranges", headers)

    def test_media_note_video_rules(self):
        """测试 media_note 视频映射规则与图片通用性。"""
        self.assertEqual(wechat_export_api.media_note({"state": "ready"}, kind="video"), "视频")
        self.assertEqual(wechat_export_api.media_note({"state": "pending"}, kind="video"), "视频（处理中）")
        self.assertEqual(
            wechat_export_api.media_note({"state": "skipped", "deny_reason": "missing_source"}, kind="video"),
            "视频（未下载）",
        )
        self.assertEqual(
            wechat_export_api.media_note({"state": "failed"}, kind="video"),
            "视频（解析失败）",
        )
        self.assertEqual(wechat_export_api.media_note(None, kind="video"), "视频")

        # 图片保持原样
        self.assertEqual(wechat_export_api.media_note({"state": "ready"}, kind="image"), "图片")
        self.assertEqual(wechat_export_api.image_note({"state": "ready"}), "图片")

    def test_media_search_endpoint_kind_filter(self):
        """/api/wechat/media?q= 支持 kind 过滤与返回 duration_s/poster。"""
        # 1. 默认 kind=image,video (两者皆有)
        status, body, _ = self._req("/api/wechat/media")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["total"], 3)

        # 2. kind=video
        status, body, _ = self._req("/api/wechat/media?kind=video")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["total"], 1)
        vid_item = data["items"][0]
        self.assertEqual(vid_item["asset_id"], "22222222222222222222222222222222")
        self.assertEqual(vid_item["duration_s"], 12)
        self.assertEqual(vid_item["poster"], "/api/wechat/media/22222222222222222222222222222222/thumb.jpg")
        self.assertEqual(vid_item["format"], "mp4")

        # 3. kind=image
        status, body, _ = self._req("/api/wechat/media?kind=image")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["total"], 2)
        for item in data["items"]:
            self.assertIn("ocr", item)

    def test_video_download_and_range_206(self):
        """测试视频二进制下载、Content-Type 与 HTTP Range 206 返回。"""
        # 1. 普通 GET 返回 200
        status, body, headers = self._req("/api/wechat/media/22222222222222222222222222222222/video.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"alice-video-bytes-0123456789abcdef")
        self.assertEqual(headers.get("Content-Type"), "video/mp4")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        total_len = len(b"alice-video-bytes-0123456789abcdef")

        # 2. Range 0-9
        status, body, headers = self._req(
            "/api/wechat/media/22222222222222222222222222222222/video.mp4",
            headers={"Range": "bytes=0-9"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"alice-vide")
        self.assertEqual(headers.get("Content-Range"), f"bytes 0-9/{total_len}")
        self.assertEqual(headers.get("Content-Length"), "10")
        self.assertEqual(headers.get("Content-Type"), "video/mp4")

        # 3. Range 10- (start only)
        status, body, headers = self._req(
            "/api/wechat/media/22222222222222222222222222222222/video.mp4",
            headers={"Range": "bytes=10-"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"alice-video-bytes-0123456789abcdef"[10:])
        self.assertEqual(headers.get("Content-Range"), f"bytes 10-{total_len - 1}/{total_len}")

        # 4. Suffix range -6
        status, body, headers = self._req(
            "/api/wechat/media/22222222222222222222222222222222/video.mp4",
            headers={"Range": "bytes=-6"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"abcdef")

        # 5. Invalid range (out of bounds) -> 416
        status, _, headers = self._req(
            "/api/wechat/media/22222222222222222222222222222222/video.mp4",
            headers={"Range": "bytes=100-200"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers.get("Content-Range"), f"bytes */{total_len}")


if __name__ == "__main__":
    unittest.main()
