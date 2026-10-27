#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and regression tests for Muse video vision and webhook poster delivery.

Covers:
1. --video-mode poster with local thumb.jpg -> content contains image_url with data:image/jpeg;base64,...
2. --video-mode poster when poster missing -> only URL link text, no error, no image_url
3. --video-mode url -> only URL link text, no image_url
4. --video-mode both -> both text video link and content image_url
5. Webhook poster delivery:
   - custom webhook with video+poster: media_poster_url and media_poster_url_abs present
   - dify webhook with video+poster: media_poster_url and media_poster_url_abs present in inputs
   - feishu webhook with video+poster: text contains '🎬 首帧: <abs>'
   - non-video (image) with poster or without poster: media_poster_url/media_poster_url_abs absent
   - video without poster: media_poster_url/media_poster_url_abs absent
6. Regression: old payload (image message without poster) byte-for-byte unchanged
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
import unittest

# Ensure clients/ is importable
CLIENTS_DIR = Path(__file__).resolve().parents[1] / "clients"
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

import assistant_webhook as aw  # noqa: E402
import muse_probe as mp  # noqa: E402


class TestMuseVideoVision(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.aid = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

        # Create dummy thumb.jpg in expected directory structure
        # exports/media/<aid>/thumb.jpg
        self.media_dir = self.base_dir / "media" / self.aid
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_path = self.media_dir / "thumb.jpg"
        self.dummy_jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"
        self.thumb_path.write_bytes(self.dummy_jpeg_bytes)

        self.video_msg_with_poster = {
            "id": "msg_vid_001",
            "type": "video",
            "kind": "video",
            "text": "",
            "ts": 1750000100,
            "sender": "张三",
            "media": {
                "asset_id": self.aid,
                "kind": "video",
                "format": "mp4",
                "duration_s": 42,
                "url": f"/api/wechat/media/{self.aid}/video.mp4",
                "poster": f"/api/wechat/media/{self.aid}/thumb.jpg",
                "anchor": {
                    "chat_id": "chat_001",
                    "chat_name": "测试视频群",
                    "ts": 1750000100,
                    "sender": "张三",
                },
            },
        }

        self.video_msg_no_poster = {
            "id": "msg_vid_002",
            "type": "video",
            "kind": "video",
            "text": "",
            "ts": 1750000101,
            "sender": "李四",
            "media": {
                "asset_id": "99999999999999999999999999999999",
                "kind": "video",
                "format": "mp4",
                "duration_s": 15,
                "url": "/api/wechat/media/99999999999999999999999999999999/video.mp4",
                "poster": None,
                "anchor": {
                    "chat_id": "chat_001",
                    "chat_name": "测试视频群",
                    "ts": 1750000101,
                    "sender": "李四",
                },
            },
        }

        self.dataset_with_poster = {
            "chats": [
                {
                    "chat_id": "chat_001",
                    "messages": [self.video_msg_with_poster],
                }
            ]
        }

        self.dataset_no_poster = {
            "chats": [
                {
                    "chat_id": "chat_001",
                    "messages": [self.video_msg_no_poster],
                }
            ]
        }

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_video_mode_poster_with_local_file(self):
        """--video-mode poster with local thumb.jpg outputs base64 image_url and descriptive text."""
        res = mp.run_vision(
            data=self.dataset_with_poster,
            public_base_url="http://192.168.1.50:8765",
            image_dir=str(self.base_dir),
            only="video",
            video_mode="poster",
        )
        self.assertIn("这是视频首帧（时长 42s）", res)
        self.assertIn("http://192.168.1.50:8765/api/wechat/media/a1b2c3d4e5f60718293a4b5c6d7e8f90/video.mp4", res)
        self.assertIn("data:image/jpeg;base64,", res)
        self.assertIn('"type": "image_url"', res)

    def test_video_mode_poster_missing_file_fallback(self):
        """When poster cannot be found locally, gracefully fall back to link text without failing."""
        res = mp.run_vision(
            data=self.dataset_no_poster,
            public_base_url="http://192.168.1.50:8765",
            image_dir=str(self.base_dir),
            only="video",
            video_mode="poster",
        )
        self.assertIn("无首帧，需消费端自行拉取视频抽帧", res)
        self.assertIn("http://192.168.1.50:8765/api/wechat/media/99999999999999999999999999999999/video.mp4", res)
        self.assertNotIn("data:image", res)
        self.assertNotIn('"type": "image_url"', res)

    def test_video_mode_url(self):
        """--video-mode url only outputs video link text, never image_url."""
        res = mp.run_vision(
            data=self.dataset_with_poster,
            public_base_url="http://192.168.1.50:8765",
            image_dir=str(self.base_dir),
            only="video",
            video_mode="url",
        )
        self.assertIn("视频链接：http://192.168.1.50:8765/api/wechat/media/a1b2c3d4e5f60718293a4b5c6d7e8f90/video.mp4", res)
        self.assertIn("需 Bearer 鉴权", res)
        self.assertNotIn("data:image", res)
        self.assertNotIn('"type": "image_url"', res)

    def test_video_mode_both(self):
        """--video-mode both outputs both text video link and content image_url."""
        res = mp.run_vision(
            data=self.dataset_with_poster,
            public_base_url="http://192.168.1.50:8765",
            image_dir=str(self.base_dir),
            only="video",
            video_mode="both",
        )
        self.assertIn("这是视频首帧（时长 42s）", res)
        self.assertIn("视频链接：http://192.168.1.50:8765/api/wechat/media/a1b2c3d4e5f60718293a4b5c6d7e8f90/video.mp4", res)
        self.assertIn("data:image/jpeg;base64,", res)
        self.assertIn('"type": "image_url"', res)


class TestWebhookPosterDelivery(unittest.TestCase):
    def setUp(self):
        self.base_url = "https://export.example.com"
        self.video_with_poster = {
            "id": "msg_vid_1",
            "type": "video",
            "kind": "video",
            "sender": "Alice",
            "ts": "2026-10-27T10:00:00Z",
            "media": {
                "asset_id": "abcdef1234567890abcdef1234567890",
                "kind": "video",
                "url": "/api/wechat/media/abcdef1234567890abcdef1234567890/video.mp4",
                "poster": "/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg",
                "duration_s": 30,
            },
        }

        self.video_no_poster = {
            "id": "msg_vid_2",
            "type": "video",
            "kind": "video",
            "sender": "Bob",
            "ts": "2026-10-27T10:01:00Z",
            "media": {
                "asset_id": "abcdef1234567890abcdef1234567890",
                "kind": "video",
                "url": "/api/wechat/media/abcdef1234567890abcdef1234567890/video.mp4",
                "poster": None,
            },
        }

        self.image_msg = {
            "id": "msg_img_1",
            "type": "image",
            "kind": "image",
            "sender": "Charlie",
            "ts": "2026-10-27T10:02:00Z",
            "media": {
                "asset_id": "11223344556677889900aabbccddeeff",
                "kind": "image",
                "url": "/api/wechat/media/11223344556677889900aabbccddeeff/image.jpeg",
                "format": "jpeg",
            },
        }

    def test_custom_webhook_video_with_poster(self):
        """Custom webhook output includes media_poster_url and media_poster_url_abs."""
        p = aw.format_payload(
            webhook_type="custom",
            chat_id="chat_test",
            msg=self.video_with_poster,
            public_base_url=self.base_url,
        )
        self.assertEqual(
            p.get("media_poster_url"),
            "/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg",
        )
        self.assertEqual(
            p.get("media_poster_url_abs"),
            f"{self.base_url}/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg",
        )

    def test_dify_webhook_video_with_poster(self):
        """Dify webhook inputs include media_poster_url and media_poster_url_abs."""
        p = aw.format_payload(
            webhook_type="dify",
            chat_id="chat_test",
            msg=self.video_with_poster,
            public_base_url=self.base_url,
        )
        inputs = p.get("inputs", {})
        self.assertEqual(
            inputs.get("media_poster_url"),
            "/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg",
        )
        self.assertEqual(
            inputs.get("media_poster_url_abs"),
            f"{self.base_url}/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg",
        )

    def test_feishu_webhook_video_with_poster(self):
        """Feishu webhook text line appends 🎬 首帧: <abs>."""
        p = aw.format_payload(
            webhook_type="feishu",
            chat_id="chat_test",
            msg=self.video_with_poster,
            public_base_url=self.base_url,
        )
        text = p["content"]["text"]
        expected_line = f"🎬 首帧: {self.base_url}/api/wechat/media/abcdef1234567890abcdef1234567890/thumb.jpg"
        self.assertIn(expected_line, text)
        self.assertIn("🖼 原图:", text)

    def test_non_video_omits_poster_keys(self):
        """Image message omits media_poster_url and media_poster_url_abs across all webhook types."""
        for w_type in ("custom", "dify", "feishu"):
            p = aw.format_payload(
                webhook_type=w_type,
                chat_id="chat_test",
                msg=self.image_msg,
                public_base_url=self.base_url,
            )
            if w_type == "custom":
                self.assertNotIn("media_poster_url", p)
                self.assertNotIn("media_poster_url_abs", p)
            elif w_type == "dify":
                self.assertNotIn("media_poster_url", p["inputs"])
                self.assertNotIn("media_poster_url_abs", p["inputs"])
            elif w_type == "feishu":
                self.assertNotIn("🎬 首帧", p["content"]["text"])

    def test_video_without_poster_omits_poster_keys(self):
        """Video message without poster omits media_poster_url and media_poster_url_abs."""
        for w_type in ("custom", "dify", "feishu"):
            p = aw.format_payload(
                webhook_type=w_type,
                chat_id="chat_test",
                msg=self.video_no_poster,
                public_base_url=self.base_url,
            )
            if w_type == "custom":
                self.assertNotIn("media_poster_url", p)
                self.assertNotIn("media_poster_url_abs", p)
            elif w_type == "dify":
                self.assertNotIn("media_poster_url", p["inputs"])
                self.assertNotIn("media_poster_url_abs", p["inputs"])
            elif w_type == "feishu":
                self.assertNotIn("🎬 首帧", p["content"]["text"])

    def test_backwards_compatibility_image_payload_byte_exact(self):
        """Image messages without poster generate payload byte-for-byte identical to legacy format."""
        legacy_baseline_payload = {
            "event": "wechat_message",
            "chat_id": "chat_test",
            "sender": "Charlie",
            "is_sender": False,
            "content": "",
            "type": "image",
            "kind": "image",
            "text": "",
            "note": "",
            "timestamp": "2026-10-27T10:02:00Z",
            "ts": "2026-10-27T10:02:00Z",
            "id": "msg_img_1",
            "media_url": "/api/wechat/media/11223344556677889900aabbccddeeff/image.jpeg",
            "media_url_abs": "https://export.example.com/api/wechat/media/11223344556677889900aabbccddeeff/image.jpeg",
            "media_url_auth": "bearer",
            "media_asset_id": "11223344556677889900aabbccddeeff",
            "chat_name": "chat_test",
            "media": {
                "asset_id": "11223344556677889900aabbccddeeff",
                "kind": "image",
                "url": "/api/wechat/media/11223344556677889900aabbccddeeff/image.jpeg",
                "format": "jpeg",
            },
        }

        generated = aw.format_payload(
            webhook_type="custom",
            chat_id="chat_test",
            msg=self.image_msg,
            public_base_url=self.base_url,
        )

        # Byte-for-byte JSON equality
        legacy_bytes = json.dumps(legacy_baseline_payload, sort_keys=True).encode("utf-8")
        gen_bytes = json.dumps(generated, sort_keys=True).encode("utf-8")
        self.assertEqual(legacy_bytes, gen_bytes)


if __name__ == "__main__":
    unittest.main()
