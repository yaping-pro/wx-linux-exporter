"""Tests for wx_contract module."""
from __future__ import annotations

import unittest

import wx_contract


class WxContractTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(wx_contract.CONTRACT_VERSION, "1.3.0")
        self.assertEqual(
            wx_contract.KIND_PREFIX, {"image": "图片", "video": "视频"}
        )

    def test_media_note_full_branch_table(self):
        # 1. asset is None / empty dict
        self.assertEqual(wx_contract.media_note(None, "image"), "图片")
        self.assertEqual(wx_contract.media_note(None, "video"), "视频")
        self.assertEqual(wx_contract.media_note({}, "image"), "图片")
        self.assertEqual(wx_contract.media_note({}, "video"), "视频")

        # 2. state == 'ready'
        self.assertEqual(
            wx_contract.media_note({"state": "ready"}, "image"), "图片"
        )
        self.assertEqual(
            wx_contract.media_note({"state": "ready"}, "video"), "视频"
        )

        # 3. state == 'pending' or deny_reason == 'image_not_on_disk'
        self.assertEqual(
            wx_contract.media_note({"state": "pending"}, "image"),
            "图片（处理中）",
        )
        self.assertEqual(
            wx_contract.media_note({"state": "pending"}, "video"),
            "视频（处理中）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "pending", "deny_reason": "image_not_on_disk"},
                "image",
            ),
            "图片（处理中）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "image_not_on_disk"},
                "image",
            ),
            "图片（处理中）",
        )

        # 4. state == 'skipped'
        # 4a. missing_source
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "missing_source"}, "image"
            ),
            "图片（未下载）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "missing_source"}, "video"
            ),
            "视频（未下载）",
        )

        # 4b. skipped:animated
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "skipped:animated"},
                "image",
            ),
            "图片（动图）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "skipped:animated"},
                "video",
            ),
            "视频（动图）",
        )

        # 4c. oversize / max_pixels_exceeded
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "oversize"}, "image"
            ),
            "图片（超大）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "max_pixels_exceeded"},
                "image",
            ),
            "图片（超大）",
        )

        # 4d. missing_keys / skipped:decryptor_unavailable / ...
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "missing_keys"}, "image"
            ),
            "图片（不可解密）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {
                    "state": "skipped",
                    "deny_reason": "skipped:decryptor_unavailable",
                },
                "image",
            ),
            "图片（不可解密）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {
                    "state": "skipped",
                    "deny_reason": "encrypted_video_unverified",
                },
                "video",
            ),
            "视频（不可解密）",
        )

        # 4e. unresolved:*
        self.assertEqual(
            wx_contract.media_note(
                {
                    "state": "skipped",
                    "deny_reason": "unresolved:ambiguous_month",
                },
                "image",
            ),
            "图片（无法定位）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "unresolved:not_found"},
                "video",
            ),
            "视频（无法定位）",
        )

        # 4f. other skipped reason
        self.assertEqual(
            wx_contract.media_note(
                {"state": "skipped", "deny_reason": "unknown_reason"}, "image"
            ),
            "图片",
        )

        # 5. state == 'failed'
        self.assertEqual(
            wx_contract.media_note({"state": "failed"}, "image"),
            "图片（解析失败）",
        )
        self.assertEqual(
            wx_contract.media_note(
                {"state": "failed", "deny_reason": "decrypt_failed"}, "image"
            ),
            "图片（解析失败）",
        )
        self.assertEqual(
            wx_contract.media_note({"state": "failed"}, "video"),
            "视频（解析失败）",
        )

    def test_ocr_projection_truncation_and_fallback(self):
        # 1. 优先使用已有的 text_head
        asset_with_th = {
            "text_head": "已有投影内容",
            "ocr": {"status": "ready", "text": "全量内容"},
        }
        self.assertEqual(
            wx_contract.ocr_projection(asset_with_th), "已有投影内容"
        )

        # 2. text_head 缺失，从 ocr.text 投影，<=2000 原样
        asset_short = {"ocr": {"status": "ready", "text": "发票代码 123456"}}
        self.assertEqual(
            wx_contract.ocr_projection(asset_short), "发票代码 123456"
        )

        # 3. text_head 缺失，超长 >2000 截断并附加 '…'
        asset_long = {"ocr": {"status": "ready", "text": "测" * 2500}}
        proj = wx_contract.ocr_projection(asset_long, max_chars=2000)
        self.assertEqual(len(proj), 2001)
        self.assertEqual(proj, "测" * 2000 + "…")

        # 4. ocr 状态非 ready 或 text 为空
        self.assertEqual(
            wx_contract.ocr_projection(
                {"ocr": {"status": "skipped", "text": "ignored"}}
            ),
            "",
        )
        self.assertEqual(
            wx_contract.ocr_projection(
                {"ocr": {"status": "ready", "text": ""}}
            ),
            "",
        )
        self.assertEqual(wx_contract.ocr_projection({}), "")

    def test_build_media_object_image_ocr_modes(self):
        anchor = {
            "chat_id": "wxid_alice",
            "chat_name": "爱丽丝",
            "local_id": 2,
            "ts": 1759000020,
            "sender": "爱丽丝",
        }
        asset = {
            "asset_id": "11111111111111111111111111111111",
            "kind": "image",
            "quality": "hd",
            "format": "jpg",
            "width": 1024,
            "height": 768,
            "bytes": 22,
            "path": "media/11111111111111111111111111111111/image.jpg",
            "url": (
                "/api/wechat/media/11111111111111111111111111111111/image.jpg"
            ),
            "anchor": anchor,
            "source": {"dat": "mock.dat", "variant": "_h"},
            "ocr": {
                "status": "ready",
                "text": "完整OCR文本内容",
                "provider": "rapidocr",
                "confidence": 0.98,
                "lang": "zh",
                "version": 1,
                "sha256": "abcdef",
            },
        }

        # 1. ocr_mode = 'none' -> 绝不含 ocr 键
        obj_none = wx_contract.build_media_object(
            asset, kind="image", anchor=anchor, ocr_mode="none"
        )
        self.assertNotIn("ocr", obj_none)
        self.assertEqual(
            obj_none["asset_id"], "11111111111111111111111111111111"
        )
        self.assertEqual(obj_none["kind"], "image")
        self.assertEqual(obj_none["quality"], "hd")
        self.assertEqual(obj_none["format"], "jpg")
        self.assertEqual(obj_none["width"], 1024)
        self.assertEqual(obj_none["height"], 768)
        self.assertEqual(obj_none["bytes"], 22)
        self.assertEqual(
            obj_none["path"],
            "media/11111111111111111111111111111111/image.jpg",
        )
        self.assertEqual(
            obj_none["url"],
            "/api/wechat/media/11111111111111111111111111111111/image.jpg",
        )
        self.assertEqual(obj_none["anchor"], anchor)
        self.assertEqual(
            obj_none["source"], {"dat": "mock.dat", "variant": "_h"}
        )

        # 2. ocr_mode = 'head' -> ocr 键存在，但不含 text 字段
        obj_head = wx_contract.build_media_object(
            asset, kind="image", anchor=anchor, ocr_mode="head"
        )
        self.assertIn("ocr", obj_head)
        self.assertNotIn("text", obj_head["ocr"])
        self.assertEqual(obj_head["ocr"]["status"], "ready")
        self.assertEqual(obj_head["ocr"]["provider"], "rapidocr")
        self.assertEqual(obj_head["ocr"]["confidence"], 0.98)
        self.assertEqual(obj_head["ocr"]["lang"], "zh")
        self.assertEqual(obj_head["ocr"]["version"], 1)
        self.assertEqual(obj_head["ocr"]["sha256"], "abcdef")

        # 3. ocr_mode = 'full' -> ocr 键存在，且包含完整 text 字段
        obj_full = wx_contract.build_media_object(
            asset, kind="image", anchor=anchor, ocr_mode="full"
        )
        self.assertIn("ocr", obj_full)
        self.assertEqual(obj_full["ocr"]["text"], "完整OCR文本内容")
        self.assertEqual(obj_full["ocr"]["status"], "ready")

    def test_build_media_object_video_shape(self):
        anchor = {
            "chat_id": "wxid_alice",
            "chat_name": "爱丽丝",
            "local_id": 3,
            "ts": 1759000030,
            "sender": "爱丽丝",
        }
        v_path = "media/2026-10-27/22222222222222222222222222222222/video.mp4"
        t_path = "media/2026-10-27/22222222222222222222222222222222/thumb.jpg"
        asset = {
            "md5": "22222222222222222222222222222222",
            "kind": "video",
            "format": "mp4",
            "bytes": 34,
            "duration_s": 12,
            "width": 1080,
            "height": 1920,
            "path": v_path,
            "thumb": t_path,
            "url": (
                "/api/wechat/media/22222222222222222222222222222222/video.mp4"
            ),
            "poster": (
                "/api/wechat/media/22222222222222222222222222222222/thumb.jpg"
            ),
            "anchor": anchor,
            "source": "hardlink",
        }
        obj = wx_contract.build_media_object(
            asset, kind="video", anchor=anchor
        )
        self.assertEqual(obj["asset_id"], "22222222222222222222222222222222")
        self.assertEqual(obj["kind"], "video")
        self.assertEqual(
            obj["url"],
            "/api/wechat/media/22222222222222222222222222222222/video.mp4",
        )
        self.assertEqual(
            obj["poster"],
            "/api/wechat/media/22222222222222222222222222222222/thumb.jpg",
        )
        self.assertEqual(obj["duration_s"], 12)
        self.assertEqual(obj["format"], "mp4")
        self.assertEqual(obj["bytes"], 34)
        self.assertEqual(obj["width"], 1080)
        self.assertEqual(obj["height"], 1920)
        self.assertEqual(obj["anchor"], anchor)
        self.assertEqual(obj["source"], "hardlink")
        self.assertEqual(obj["path"], v_path)

    def test_build_media_object_video_fallback_poster_and_url(self):
        anchor = {"chat_id": "wxid_bob", "local_id": 10}
        asset = {
            "asset_id": "vid123",
            "kind": "video",
            "thumb": "has_thumb.jpg",
        }
        obj = wx_contract.build_media_object(
            asset, kind="video", anchor=anchor
        )
        self.assertEqual(obj["asset_id"], "vid123")
        self.assertEqual(obj["url"], "/api/wechat/media/vid123/video.mp4")
        self.assertEqual(obj["poster"], "/api/wechat/media/vid123/thumb.jpg")
        self.assertNotIn("path", obj)
