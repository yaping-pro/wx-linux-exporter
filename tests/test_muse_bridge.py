#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and regression tests for muse bridge and media enrichment integration.

Tests cover:
1. format_payload in assistant_webhook:
   - Backwards compatibility when media is absent (exact equality).
   - Enriched media payload fields present for custom, dify, and feishu.
   - Flag --no-media disables media inclusion.
   - Feishu appends image URL line with base host derived from export_url.
   - [note] fallback behavior when text is empty.
2. muse_probe functionality:
   - Sensitive digit masking (>=11 digits masked as ***).
   - --audit mode stats calculation and pass/fail thresholds.
   - --score mode against mocked webhook responses (hit, partial, hallu).
   - --dry-run mode does not invoke network requests.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
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

    def test_enriched_media_custom(self):
        """Custom format with media includes all required fields."""
        p = aw.format_payload(
            "custom", "chat_group_1", self.image_msg_enriched
        )
        self.assertEqual(
            p["media_url"], f"/api/wechat/media/{'a' * 32}/image.jpg"
        )
        self.assertEqual(p["media_asset_id"], "a" * 32)
        self.assertEqual(p["chat_name"], "技术研发交流群")
        self.assertEqual(p["ocr_text"], "解约协议与家庭医生签约通知全文...")
        self.assertEqual(p["media"], self.enriched_media)

    def test_enriched_media_dify(self):
        """Dify format with media populates fields into inputs dict."""
        p = aw.format_payload(
            "dify", "chat_group_1", self.image_msg_enriched
        )
        inputs = p["inputs"]
        self.assertEqual(
            inputs["media_url"], f"/api/wechat/media/{'a' * 32}/image.jpg"
        )
        self.assertEqual(inputs["media_asset_id"], "a" * 32)
        self.assertEqual(inputs["chat_name"], "技术研发交流群")
        self.assertEqual(
            inputs["ocr_text"], "解约协议与家庭医生签约通知全文..."
        )
        self.assertEqual(inputs["media"], self.enriched_media)

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
            "media_asset_id",
            "chat_name",
            "ocr_text",
        ):
            self.assertNotIn(key, p)

    def test_chat_name_fallback_to_chat_id(self):
        """If anchor.chat_name is missing, chat_name falls back to chat_id."""
        msg_without_chatname = dict(self.image_msg_enriched)
        media_copy = dict(self.enriched_media)
        media_copy["anchor"] = {"chat_id": "fallback_id"}
        msg_without_chatname["media"] = media_copy

        p = aw.format_payload("custom", "fallback_id", msg_without_chatname)
        self.assertEqual(p["chat_name"], "fallback_id")

    def test_feishu_media_url_formatting(self):
        """Feishu payload appends image URL with host from export_url."""
        # 1. With export_url providing host
        p1 = aw.format_payload(
            "feishu",
            "chat_group_1",
            self.image_msg_enriched,
            export_url="http://192.168.1.100:8765/api/wechat/export",
        )
        text1 = p1["content"]["text"]
        aid_str = "a" * 32
        exp1 = (
            f"http://192.168.1.100:8765/api/wechat/media/{aid_str}/image.jpg"
        )
        self.assertIn(f"🖼 原图: {exp1}", text1)

        # 2. Without export_url (fallback to relative URL)
        p2 = aw.format_payload(
            "feishu",
            "chat_group_1",
            self.image_msg_enriched,
            export_url="",
        )
        text2 = p2["content"]["text"]
        exp2 = f"/api/wechat/media/{'a' * 32}/image.jpg"
        self.assertIn(f"🖼 原图: {exp2}", text2)


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

    def test_audit_passes_on_valid_dataset(self):
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
        # In this dataset: 2 images, 1 text non-empty (50% < 80% -> FAIL)
        passed, report = mp.run_audit(dataset)
        self.assertFalse(passed)
        self.assertIn("AUDIT: FAIL", report)

        # Add 3 images to make 4 out of 5 non-empty (80%) -> PASS
        for i in range(3):
            dataset["chats"][0]["messages"].append({
                "kind": "image",
                "text": f"文字内容 {i}",
                "media": {
                    "asset_id": f"{i+3}" * 32,
                    "kind": "image",
                    "url": f"/api/wechat/media/{i+3}/image.jpg",
                    "anchor": {
                        "chat_id": "group_101",
                        "chat_name": "社区健康服务群",
                        "local_id": i + 3,
                        "ts": 1700000020 + i,
                        "sender": "wxid_doc",
                    },
                    "ocr": {"status": "ready", "text": f"文字内容 {i}"},
                },
            })

        passed, report = mp.run_audit(dataset)
        self.assertTrue(passed)
        self.assertIn("AUDIT: PASS", report)
        self.assertIn("***", report)  # Sensitive phone number was masked


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
