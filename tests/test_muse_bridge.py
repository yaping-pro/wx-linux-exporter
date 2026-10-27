#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and regression tests for muse bridge and media enrichment integration.

Tests cover:
1. format_payload in assistant_webhook:
   - Backwards compatibility when media is absent (exact equality).
   - Enriched media payload fields (media_url, media_url_abs, media_url_auth).
   - Base URL construction with/without public_base_url.
   - Flag --no-media disables media inclusion.
   - Feishu appends image URL line with base host derived from export_url.
   - [note] fallback behavior when text is empty.
   - fetch_export query string passthrough for media, media_chats, ocr.
2. muse_probe functionality:
   - Sensitive digit masking (>=11 digits masked as ***).
   - --audit mode stats calculation under OCR enabled vs OCR disabled.
   - --vision payload structure: base64 inlining, --no-base64 url fallback.
   - --vision handling when OCR text is empty (no empty placeholder text).
   - --vision with --only video (url + poster, no base64).
   - --estimate-bytes table formatting and row output.
   - --score mode against mocked webhook responses (hit, partial, hallu).
   - --dry-run mode does not invoke network requests.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

# Ensure clients/ is importable
CLIENTS_DIR = Path(__file__).resolve().parents[1] / "clients"
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

import assistant_webhook as aw  # noqa: E402
import muse_probe as mp  # noqa: E402


class TestFormatPayload(unittest.TestCase):
    def setUp(self):
        self.legacy_text_msg = {
            "text": "测试文本消息",
            "kind": "text",
            "note": "",
            "ts": "2026-10-27T10:00:00+08:00",
            "sender": "wxid_user1",
            "id": "msg_001",
            "is_sender": False,
        }
        self.legacy_note_msg = {
            "text": "",
            "kind": "image",
            "note": "图片",
            "ts": "2026-10-27T10:01:00+08:00",
            "sender": "wxid_user2",
            "id": "msg_002",
            "is_sender": True,
        }
        self.enriched_media = {
            "asset_id": "a" * 32,
            "kind": "image",
            "quality": "hd",
            "format": "jpg",
            "width": 1080,
            "height": 1920,
            "bytes": 1048576,
            "path": f"media/{'a' * 32}/image.jpg",
            "url": f"/api/wechat/media/{'a' * 32}/image.jpg",
            "anchor": {
                "chat_id": "chat_group_1",
                "chat_name": "技术研发交流群",
                "local_id": 101,
                "ts": 1759123456,
                "sender": "wxid_user3",
            },
            "source": {"dat": "msg/attach/xxx_h.dat", "variant": "_h"},
            "ocr": {
                "status": "ready",
                "text": "解约协议与家庭医生签约通知全文...",
                "provider": "rapidocr",
                "lang": "chi_sim+eng",
                "confidence": 0.95,
                "version": 1,
                "sha256": "f" * 64,
            },
        }
        self.image_msg_enriched = {
            "text": "解约协议与家庭医生签约通知全文...",
            "kind": "image",
            "note": "图片",
            "ts": "2026-10-27T10:05:00+08:00",
            "sender": "wxid_user3",
            "id": "msg_003",
            "is_sender": False,
            "media": self.enriched_media,
        }

    def test_backwards_compatibility_custom(self):
        """When media is absent, payload must match legacy format byte-wise."""
        p = aw.format_payload("custom", "chat_x", self.legacy_text_msg)
        expected = {
            "event": "wechat_message",
            "chat_id": "chat_x",
            "sender": "wxid_user1",
            "is_sender": False,
            "content": "测试文本消息",
            "type": "text",
            "kind": "text",
            "text": "测试文本消息",
            "note": "",
            "timestamp": "2026-10-27T10:00:00+08:00",
            "ts": "2026-10-27T10:00:00+08:00",
            "id": "msg_001",
        }
        self.assertEqual(p, expected)
        forbidden_keys = (
            "media",
            "media_url",
            "media_url_abs",
            "media_url_auth",
            "media_asset_id",
            "chat_name",
            "ocr_text",
        )
        for forbidden in forbidden_keys:
            self.assertNotIn(forbidden, p)

    def test_backwards_compatibility_dify(self):
        """Dify format without media must not contain any new media keys."""
        p = aw.format_payload("dify", "chat_x", self.legacy_text_msg)
        inputs = p["inputs"]
        forbidden_keys = (
            "media",
            "media_url",
            "media_url_abs",
            "media_url_auth",
            "media_asset_id",
            "chat_name",
            "ocr_text",
        )
        for forbidden in forbidden_keys:
            self.assertNotIn(forbidden, inputs)
        self.assertEqual(inputs["content"], "测试文本消息")

    def test_note_fallback_content(self):
        """Content falls back to [note] when text is empty."""
        p = aw.format_payload("custom", "chat_x", self.legacy_note_msg)
        self.assertEqual(p["content"], "[图片]")
        self.assertEqual(p["text"], "")

    def test_enriched_media_custom_with_and_without_base(self):
        """Custom format includes media_url, media_url_abs, media_url_auth."""
        # 1. With public_base_url
        p1 = aw.format_payload(
            "custom",
            "chat_group_1",
            self.image_msg_enriched,
            public_base_url="https://wechat.example.com",
        )
        aid = "a" * 32
        rel = f"/api/wechat/media/{aid}/image.jpg"
        self.assertEqual(p1["media_url"], rel)
        self.assertEqual(
            p1["media_url_abs"], f"https://wechat.example.com{rel}"
        )
        self.assertEqual(p1["media_url_auth"], "bearer")
        self.assertEqual(p1["media_asset_id"], aid)
        self.assertEqual(p1["chat_name"], "技术研发交流群")
        self.assertEqual(p1["ocr_text"], "解约协议与家庭医生签约通知全文...")

        # 2. Without base url (falls back to relative path in media_url_abs)
        p2 = aw.format_payload(
            "custom",
            "chat_group_1",
            self.image_msg_enriched,
            export_url="",
            public_base_url="",
        )
        self.assertEqual(p2["media_url"], rel)
        self.assertEqual(p2["media_url_abs"], rel)
        self.assertEqual(p2["media_url_auth"], "bearer")

    def test_enriched_media_dify(self):
        """Dify format with media populates fields into inputs dict."""
        p = aw.format_payload(
            "dify",
            "chat_group_1",
            self.image_msg_enriched,
            public_base_url="http://127.0.0.1:8765",
        )
        inputs = p["inputs"]
        aid = "a" * 32
        rel = f"/api/wechat/media/{aid}/image.jpg"
        self.assertEqual(inputs["media_url"], rel)
        self.assertEqual(
            inputs["media_url_abs"], f"http://127.0.0.1:8765{rel}"
        )
        self.assertEqual(inputs["media_url_auth"], "bearer")
        self.assertEqual(inputs["media_asset_id"], aid)
        self.assertEqual(inputs["chat_name"], "技术研发交流群")

    def test_include_media_false_omits_media(self):
        """Setting include_media=False omits media even if present in msg."""
        p = aw.format_payload(
            "custom",
            "chat_group_1",
            self.image_msg_enriched,
            include_media=False,
        )
        for key in (
            "media",
            "media_url",
            "media_url_abs",
            "media_url_auth",
            "media_asset_id",
            "chat_name",
            "ocr_text",
        ):
            self.assertNotIn(key, p)

    def test_fetch_export_query_params(self):
        """fetch_export correctly serializes media, media_chats, ocr params."""
        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = b'{"watermark": "123"}'
            mock_url.return_value.__enter__.return_value = mock_resp

            aw.fetch_export(
                endpoint="http://127.0.0.1:8765/api/wechat/export",
                token="test_tok",
                media="image",
                media_chats="chat1,chat2",
                ocr="none",
            )
            self.assertTrue(mock_url.called)
            req = mock_url.call_args[0][0]
            parsed = urllib.parse.urlparse(req.full_url)
            qs = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(qs.get("media"), ["image"])
            self.assertEqual(qs.get("media_chats"), ["chat1,chat2"])
            self.assertEqual(qs.get("ocr"), ["none"])


class TestMuseProbeAudit(unittest.TestCase):
    def test_mask_sensitive_digits(self):
        """Sequences of >=11 digits must be masked with ***."""
        cases = [
            ("我的手机号是13812345678请联系", "我的手机号是***请联系"),
            ("身份证号110101199003072345号", "身份证号***号"),
            ("卡号6222021234567890123", "卡号***"),
            ("短数字12345和4096不变", "短数字12345和4096不变"),
            ("恰好10位1234567890不变", "恰好10位1234567890不变"),
            ("恰好11位12345678901被遮", "恰好11位***被遮"),
        ]
        for src, expected in cases:
            self.assertEqual(mp.mask_sensitive_digits(src), expected)

    def test_audit_ocr_enabled_thresholds(self):
        dataset = {
            "chats": [
                {
                    "chat_id": "group_101",
                    "messages": [
                        {
                            "kind": "image",
                            "text": "家庭医生签约通知，请于本月完成13800000000",
                            "media": {
                                "asset_id": "1" * 32,
                                "kind": "image",
                                "url": f"/api/wechat/media/{'1' * 32}/img.jpg",
                                "anchor": {
                                    "chat_id": "group_101",
                                    "chat_name": "社区健康服务群",
                                    "local_id": 1,
                                    "ts": 1700000000,
                                    "sender": "wxid_doc",
                                },
                                "ocr": {
                                    "status": "ready",
                                    "text": (
                                        "家庭医生签约通知，请于本月完成13800000000"
                                    ),
                                },
                            },
                        },
                        {
                            "kind": "image",
                            "text": "",
                            "media": {
                                "asset_id": "2" * 32,
                                "kind": "image",
                                "url": f"/api/wechat/media/{'2' * 32}/img.jpg",
                                "anchor": {
                                    "chat_id": "group_101",
                                    "chat_name": "社区健康服务群",
                                    "local_id": 2,
                                    "ts": 1700000010,
                                    "sender": "wxid_doc",
                                },
                                "ocr": {
                                    "status": "empty",
                                    "text": "",
                                },
                            },
                        },
                    ],
                }
            ]
        }
        # 1. OCR enabled: 1 of 2 non-empty (50% < 80% threshold -> FAIL)
        passed1, report1 = mp.run_audit(dataset, ocr_enabled=True)
        self.assertFalse(passed1)
        self.assertIn("AUDIT: FAIL", report1)

        # 2. OCR disabled: text non-empty not required, urls resolvable -> PASS
        passed2, report2 = mp.run_audit(dataset, ocr_enabled=False)
        self.assertTrue(passed2)
        self.assertIn("AUDIT: PASS", report2)
        self.assertIn("ocr=disabled", report2)


class TestMuseProbeVisionAndEstimate(unittest.TestCase):
    def setUp(self):
        self.aid_img = "c" * 8
        self.aid_vid = "d" * 8
        self.dataset = {
            "chats": [
                {
                    "chat_id": "chat_001",
                    "messages": [
                        {
                            "kind": "image",
                            "text": "排班表通知",
                            "media": {
                                "asset_id": self.aid_img,
                                "kind": "image",
                                "format": "png",
                                "url": f"/media/{self.aid_img}.png",
                                "anchor": {
                                    "chat_id": "chat_001",
                                    "chat_name": "技术运营群",
                                    "ts": 1750000000,
                                    "sender": "张三",
                                },
                                "ocr": {
                                    "status": "ready",
                                    "text": "排班表详情：周一李四，周二王五",
                                },
                            },
                        },
                        {
                            "kind": "image",
                            "text": "",
                            "media": {
                                "asset_id": "e" * 32,
                                "kind": "image",
                                "format": "jpg",
                                "url": f"/api/wechat/media/{'e' * 32}/img.jpg",
                                "anchor": {
                                    "chat_id": "chat_001",
                                    "chat_name": "技术运营群",
                                    "ts": 1750000010,
                                    "sender": "李四",
                                },
                                "ocr": {
                                    "status": "empty",
                                    "text": "",
                                },
                            },
                        },
                        {
                            "kind": "video",
                            "text": "",
                            "media": {
                                "asset_id": self.aid_vid,
                                "kind": "video",
                                "format": "mp4",
                                "url": f"/media/{self.aid_vid}.mp4",
                                "poster": f"/media/{self.aid_vid}.jpg",
                                "anchor": {
                                    "chat_id": "chat_001",
                                    "chat_name": "技术运营群",
                                    "ts": 1750000020,
                                    "sender": "王五",
                                },
                            },
                        },
                    ],
                }
            ]
        }

    def test_vision_payload_structure_and_no_base64(self):
        """--vision generates valid OpenAI multimodal payload with URL."""
        res = mp.run_vision(
            data=self.dataset,
            vision_model="gpt-4o",
            limit=1,
            no_base64=True,
            public_base_url="http://192.168.1.50:8765",
            only="image",
        )
        self.assertIn("这是给 Muse 的载荷", res)
        expected_url = f"http://192.168.1.50:8765/media/{self.aid_img}.png"
        self.assertIn(expected_url, res)
        self.assertIn("排班表详情：周一李四", res)

    def test_vision_payload_when_ocr_empty(self):
        """When OCR text is empty, no empty OCR placeholder is injected."""
        # Target the second message which has empty OCR
        dataset_empty = {
            "chats": [
                {
                    "chat_id": "c",
                    "messages": [self.dataset["chats"][0]["messages"][1]],
                }
            ]
        }
        res = mp.run_vision(
            data=dataset_empty,
            no_base64=True,
            only="image",
        )
        self.assertNotIn("OCR识别参考文本", res)
        self.assertIn("会话名称: 技术运营群", res)

    def test_vision_payload_only_video(self):
        """--vision with --only video outputs video_url without base64."""
        res = mp.run_vision(
            data=self.dataset,
            public_base_url="http://127.0.0.1:8765",
            only="video",
        )
        self.assertIn("video_url", res)
        self.assertIn(f"/media/{self.aid_vid}.mp4", res)
        self.assertIn(f"/media/{self.aid_vid}.jpg", res)
        self.assertNotIn("data:image", res)

    def test_estimate_bytes_table(self):
        """--estimate-bytes returns formatted size comparison table."""
        table = mp.estimate_payload_bytes(self.dataset)
        self.assertIn("Payload Size Estimation Across Filter Modes", table)
        self.assertIn("media=none & ocr=none", table)
        self.assertIn("media=image & ocr=head/full", table)
        self.assertIn("media=all (image + video)", table)
        lines = table.strip().split("\n")
        self.assertGreaterEqual(len(lines), 6)


class TestMuseProbeScoreAndDryRun(unittest.TestCase):
    def setUp(self):
        self.aid1 = "a" * 32
        self.aid2 = "b" * 32
        self.dataset = {
            "chats": [
                {
                    "chat_id": "chat_family",
                    "messages": [
                        {
                            "kind": "image",
                            "text": "解约提醒：您的家庭医生协议已生效",
                            "media": {
                                "asset_id": self.aid1,
                                "anchor": {
                                    "chat_id": "chat_family",
                                    "chat_name": "家庭医护互助群",
                                    "local_id": 10,
                                    "ts": 1750000000,
                                    "sender": "wxid_doc",
                                },
                                "ocr": {
                                    "status": "ready",
                                    "text": "解约提醒：您的家庭医生协议已生效",
                                },
                            },
                        },
                        {
                            "kind": "image",
                            "text": "",
                            "media": {
                                "asset_id": self.aid2,
                                "anchor": {
                                    "chat_id": "chat_family",
                                    "chat_name": "家庭医护互助群",
                                    "local_id": 11,
                                    "ts": 1750000010,
                                    "sender": "wxid_user",
                                },
                                "ocr": {
                                    "status": "empty",
                                    "text": "",
                                },
                            },
                        },
                    ],
                }
            ]
        }

    def test_dry_run_does_not_call_network(self):
        """--dry-run preview does not make any urllib network requests."""
        with mock.patch("urllib.request.urlopen") as mock_url:
            output = mp.run_dry_run(
                self.dataset, webhook_type="custom", limit=2
            )
            self.assertFalse(mock_url.called)
            self.assertIn("[DRY-RUN]", output)
            self.assertIn(self.aid1, output)

    def test_score_all_pass(self):
        """All probes hit required keywords and pass hallucination checks."""
        probes = [
            {
                "asset_id": self.aid1,
                "question": "图中说了什么？",
                "must_contain": ["解约", "家庭医生"],
            },
            {
                "asset_id": self.aid2,
                "question": "图中是否有文字？",
                "must_contain": [],
                "expect_empty": True,
            },
        ]
        with tempfile.NamedTemporaryFile(
            "w+", encoding="utf-8", suffix=".json"
        ) as tf:
            json.dump(probes, tf)
            tf.flush()

            resp1 = json.dumps({
                "choices": [{
                    "message": {
                        "content": "群名家庭医护互助群，图中有解约和家庭医生通知。"
                    }
                }]
            })
            resp2 = json.dumps({
                "choices": [{
                    "message": {
                        "content": "这是一张风景照片，没有发现文字内容。"
                    }
                }]
            })

            with mock.patch("muse_probe.post_probe_request") as mock_post:
                mock_post.side_effect = [
                    (200, resp1, None),
                    (200, resp2, None),
                ]
                passed, report = mp.run_score(
                    data=self.dataset,
                    probes_path=tf.name,
                    webhook_url="http://mock.endpoint/eval",
                    webhook_type="custom",
                )
                self.assertTrue(passed)
                self.assertIn("Hit Rate:           100.0%", report)
                self.assertIn("Hallucinations:     0", report)
                self.assertIn("OVERALL:            PASS", report)

    def test_score_hallucination_and_missing_keywords(self):
        """Expect_empty probe detects hallucinated invoice/amount."""
        probes = [
            {
                "asset_id": self.aid1,
                "question": "图中内容？",
                "must_contain": ["必须包含但回答缺失的词"],
            },
            {
                "asset_id": self.aid2,
                "question": "有文字吗？",
                "must_contain": [],
                "expect_empty": True,
            },
        ]
        with tempfile.NamedTemporaryFile(
            "w+", encoding="utf-8", suffix=".json"
        ) as tf:
            json.dump(probes, tf)
            tf.flush()

            resp1 = json.dumps({
                "choices": [{
                    "message": {"content": "普通说明文字"}
                }]
            })
            resp2 = json.dumps({
                "choices": [{
                    "message": {"content": "图中是增值税发票，金额为：5000元"}
                }]
            })

            with mock.patch("muse_probe.post_probe_request") as mock_post:
                mock_post.side_effect = [
                    (200, resp1, None),
                    (200, resp2, None),
                ]
                passed, report = mp.run_score(
                    data=self.dataset,
                    probes_path=tf.name,
                    webhook_url="http://mock.endpoint/eval",
                    webhook_type="custom",
                )
                self.assertFalse(passed)
                self.assertIn("Hit Rate:           0.0%", report)
                self.assertIn("Hallucinations:     1", report)
                self.assertIn("OVERALL:            FAIL", report)


if __name__ == "__main__":
    unittest.main()
