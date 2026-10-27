#!/usr/bin/env python3
"""End-to-End Pipeline TDD Test Suite for wx-linux-exporter.

Validates the full pipeline against an offline, zero-network, synthetic WeChat 4.x vault:
1. worker -> ledger: wechat_media worker resolves images, video, voice, and updates media_ledger.json.
2. API export: /api/wechat/export handles media/ocr filters, cursor pagination, streaming ndjson, 401, 404/403 anti-leak.
3. Media endpoint: /api/wechat/media/<asset_id>/<file> 200 + ETag, If-None-Match 304, video Range 206 + Content-Range.
4. Webhook bridge: clients/assistant_webhook.format_payload formats custom/dify/feishu payloads.
5. Muse probe: clients/muse_probe.run_audit returns PASS, --vision produces image_url for video posters.
6. Failure path: Read-only state directory causes worker to exit non-zero with PermissionError / [ERROR].
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "bin", REPO_ROOT / "clients"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tests.e2e.fixtures_builder import (  # noqa: E402
    build_synthetic_vault,
    BLOCKED_CHAT,
    BLOCKED_ASSET_ID,
    IMAGE_ASSET_ID,
    SYNTH_CHAT,
    TOKEN,
    VIDEO_ASSET_ID,
)
import wechat_export_api  # noqa: E402
from clients import assistant_webhook  # noqa: E402
from clients import muse_probe  # noqa: E402


class TestE2EPipeline(unittest.TestCase):
    """End-to-End pipeline regression and integration tests."""

    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls._tmp_dir.name)
        # Build synthetic vault (with complete 4.x schema including packed_info_data)
        cls.info = build_synthetic_vault(cls.tmp_path, include_packed_info_data=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp_dir.cleanup()
    def _run_worker(self, vault_dir: Path, policy_path: Path) -> subprocess.CompletedProcess[str]:
        """Run wechat_media worker CLI via subprocess.

        Subprocess execution is preferred over direct import invocation because:
        1. It tests the real CLI entrypoint exactly as systemd / crontab / docker worker runs it.
        2. Clean process isolation: lock file acquisition (fcntl.flock) and releases are OS-enforced.
        3. Memory, imported singletons, and cache states do not pollute the test runner process.
        4. Failure paths (e.g. PermissionError on read-only state dir) exit cleanly with exit code.
        """
        cmd = [
            sys.executable,
            str(REPO_ROOT / "bin" / "wechat_media.py"),
            "--vault", str(vault_dir),
            "--policy", str(policy_path),
            "--verbose",
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_01_worker_to_ledger(self):
        """1. worker -> ledger: 跑 wechat_media.main() 后断言 media_ledger.json。"""
        res = self._run_worker(self.info.vault_dir, self.info.policy_path)
        self.assertEqual(
            res.returncode,
            0,
            f"Worker failed with rc={res.returncode}:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}",
        )

        self.assertTrue(
            self.info.ledger_path.is_file(),
            f"media_ledger.json was not created at {self.info.ledger_path}",
        )

        ledger = json.loads(self.info.ledger_path.read_text(encoding="utf-8"))

        # 断言账本 schema 字段存在
        self.assertIn("schema", ledger, "media_ledger.json missing 'schema' field")

        assets = ledger.get("assets", {})

        # 图片: state=ready 且 format 正确
        img_asset = assets.get(self.info.image_asset_id)
        self.assertIsNotNone(img_asset, f"Image asset {self.info.image_asset_id} missing from ledger")
        self.assertEqual(img_asset.get("state"), "ready", f"Image asset not ready: {img_asset}")
        self.assertIn(
            img_asset.get("format"),
            ("jpg", "jpeg", "png", "webp"),
            f"Unexpected format: {img_asset.get('format')}",
        )

        # 视频: state=ready 且 duration_s > 0
        vid_asset = assets.get(self.info.video_asset_id)
        self.assertIsNotNone(vid_asset, f"Video asset {self.info.video_asset_id} missing from ledger")
        self.assertEqual(vid_asset.get("state"), "ready", f"Video asset not ready: {vid_asset}")
        self.assertIsNotNone(vid_asset.get("duration_s"))
        self.assertGreater(vid_asset.get("duration_s", 0), 0)

        # 语音: kind=voice (若微信自带转写为空则 state=pending 或 skipped)
        voice_assets = [a for a in assets.values() if a.get("kind") == "voice"]
        self.assertTrue(len(voice_assets) > 0, "No voice asset recorded in ledger")
        v_asset = voice_assets[0]
        self.assertEqual(v_asset.get("kind"), "voice")
        self.assertIn(v_asset.get("state"), ("pending", "skipped", "ready"))

    def test_02_api_export_all_and_filters(self):
        """2. API 导出: /api/wechat/export 各种参数、分页、ndjson 与鉴权。"""
        # Ensure worker has run first
        self._run_worker(self.info.vault_dir, self.info.policy_path)

        server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=self.info.decrypted_dir,
            token=self.info.token,
            auth_enabled=True,
            media_vault_root=str(self.info.vault_dir),
            media_policy_path=str(self.info.policy_path),
            media_ledger_path=str(self.info.ledger_path),
            blocklist=[BLOCKED_CHAT],
        )
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        base_url = f"http://127.0.0.1:{port}"
        auth_hdr = {"Authorization": f"Bearer {self.info.token}"}

        try:
            # 2.1 GET /api/wechat/export?media=all&ocr=full -> 5 条消息、图片带 media、视频带 poster 或 duration_s
            export_url = (
                f"{base_url}/api/wechat/export"
                f"?since={self.info.base_time}"
                f"&to={self.info.base_time + 100}"
                f"&media=all&ocr=full"
            )
            req = urllib.request.Request(export_url, headers=auth_hdr)
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))

            chats = data.get("chats", [])
            synth_chats = [c for c in chats if c.get("chat_id") == SYNTH_CHAT]
            self.assertEqual(len(synth_chats), 1, f"Expected 1 chat for {SYNTH_CHAT}")
            messages = synth_chats[0].get("messages", [])
            self.assertEqual(len(messages), 5, f"Expected 5 messages, got {len(messages)}")

            # 图片消息 (local_type=3 -> kind=image) 带 media (asset_id/url/anchor.chat_name)
            img_msgs = [m for m in messages if m.get("kind") == "image"]
            self.assertEqual(len(img_msgs), 1)
            img_m = img_msgs[0]
            self.assertIn("media", img_m)
            self.assertEqual(img_m["media"].get("asset_id"), self.info.image_asset_id)
            self.assertTrue(bool(img_m["media"].get("url")))
            self.assertEqual(img_m["media"].get("anchor", {}).get("chat_name"), "合成测试群")
            self.assertEqual(img_m.get("note"), "图片", f"Expected image note '图片', got {img_m.get('note')!r}")
            # 视频消息 (local_type=43 -> kind=video) 带 poster 或 duration_s
            vid_msgs = [m for m in messages if m.get("kind") == "video"]
            self.assertEqual(len(vid_msgs), 1)
            vid_m = vid_msgs[0]
            self.assertIn("media", vid_m)
            self.assertTrue(
                vid_m["media"].get("poster") or (vid_m["media"].get("duration_s", 0) > 0),
                f"Video media object missing poster/duration_s: {vid_m['media']}",
            )
            self.assertEqual(vid_m.get("note"), "视频", f"Expected video note '视频', got {vid_m.get('note')!r}")
            # 2.2 media=none&ocr=none -> 无 media 键、note 保留、payload_stats.ocr_chars==0
            no_media_url = (
                f"{base_url}/api/wechat/export"
                f"?since={self.info.base_time}"
                f"&to={self.info.base_time + 100}"
                f"&media=none&ocr=none"
            )
            req_no_m = urllib.request.Request(no_media_url, headers=auth_hdr)
            with urllib.request.urlopen(req_no_m) as resp:
                self.assertEqual(resp.status, 200)
                no_m_data = json.loads(resp.read().decode("utf-8"))

            for c in no_m_data.get("chats", []):
                for m in c.get("messages", []):
                    self.assertNotIn("media", m, "media key should not be present when media=none")
                    self.assertTrue(bool(m.get("note") or m.get("text")), f"note or text missing: {m}")

            self.assertEqual(
                no_m_data.get("payload_stats", {}).get("ocr_chars"),
                0,
                "ocr_chars should be 0 when ocr=none",
            )

            # 2.3 limit=2 -> next_cursor 非空，用 cursor 取第二页，两页拼起来 == 不分页结果的 messages 序列
            p1_url = (
                f"{base_url}/api/wechat/export"
                f"?since={self.info.base_time}"
                f"&to={self.info.base_time + 100}"
                f"&limit=2"
            )
            with urllib.request.urlopen(urllib.request.Request(p1_url, headers=auth_hdr)) as resp:
                p1_data = json.loads(resp.read().decode("utf-8"))

            cursor1 = p1_data.get("next_cursor")
            self.assertIsNotNone(cursor1, "Expected next_cursor in p1")

            p2_url = (
                f"{base_url}/api/wechat/export"
                f"?since={self.info.base_time}"
                f"&to={self.info.base_time + 100}"
                f"&cursor={urllib.parse.quote(cursor1)}"
            )
            with urllib.request.urlopen(urllib.request.Request(p2_url, headers=auth_hdr)) as resp:
                p2_data = json.loads(resp.read().decode("utf-8"))

            p1_msgs = [m for c in p1_data.get("chats", []) for m in c.get("messages", [])]
            p2_msgs = [m for c in p2_data.get("chats", []) for m in c.get("messages", [])]
            combined_msgs = p1_msgs + p2_msgs

            unpaginated_msgs = [m for c in data.get("chats", []) for m in c.get("messages", [])]
            self.assertEqual(
                len(combined_msgs),
                len(unpaginated_msgs),
                f"Combined page length {len(combined_msgs)} != unpaginated {len(unpaginated_msgs)}",
            )
            for m_comb, m_unpag in zip(combined_msgs, unpaginated_msgs):
                self.assertEqual(m_comb["ts"], m_unpag["ts"])
                self.assertEqual(m_comb["kind"], m_unpag["kind"])

            # 2.4 format=ndjson -> 首行 type=manifest、末行 type=end
            ndjson_url = (
                f"{base_url}/api/wechat/export"
                f"?since={self.info.base_time}"
                f"&to={self.info.base_time + 100}"
                f"&format=ndjson"
            )
            with urllib.request.urlopen(urllib.request.Request(ndjson_url, headers=auth_hdr)) as resp:
                self.assertEqual(resp.status, 200)
                raw_lines = resp.read().decode("utf-8").strip().split("\n")
                nd_rows = [json.loads(line) for line in raw_lines if line.strip()]

            self.assertTrue(len(nd_rows) >= 2)
            self.assertEqual(nd_rows[0].get("type"), "manifest")
            self.assertEqual(nd_rows[-1].get("type"), "end")

            # 2.5 401: 无 Bearer 时 /api/wechat/export 与 /api/wechat/media/<id>/<file> 均 401
            try:
                urllib.request.urlopen(export_url)
                self.fail("Expected 401 for unauthenticated export")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            media_unauth_url = f"{base_url}/api/wechat/media/{self.info.image_asset_id}/image.png"
            try:
                urllib.request.urlopen(media_unauth_url)
                self.fail("Expected 401 for unauthenticated media access")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 2.6 404/403: blocklist 会话的媒体不可取（anti-leak）
            blocked_media_url = f"{base_url}/api/wechat/media/{BLOCKED_ASSET_ID}/image.png"
            try:
                urllib.request.urlopen(urllib.request.Request(blocked_media_url, headers=auth_hdr))
                self.fail("Expected 404/403 for blocked chat media")
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (403, 404))

        finally:
            server.shutdown()
            server.server_close()
            srv_thread.join(timeout=3)

    def test_03_media_endpoints(self):
        """3. 媒体端点: 200 + ETag、If-None-Match 304、视频 Range 206 + Content-Range。"""
        self._run_worker(self.info.vault_dir, self.info.policy_path)

        server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=self.info.decrypted_dir,
            token=self.info.token,
            auth_enabled=True,
            media_vault_root=str(self.info.vault_dir),
            media_policy_path=str(self.info.policy_path),
            media_ledger_path=str(self.info.ledger_path),
        )
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        base_url = f"http://127.0.0.1:{port}"
        auth_hdr = {"Authorization": f"Bearer {self.info.token}"}

        try:
            # 3.1 GET /api/wechat/media/<asset_id>/image.png 200 + ETag
            img_url = f"{base_url}/api/wechat/media/{self.info.image_asset_id}/image.png"
            req = urllib.request.Request(img_url, headers=auth_hdr)
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                etag = resp.headers.get("ETag")
                self.assertIsNotNone(etag, "Expected ETag header in response")
                content = resp.read()
                self.assertTrue(len(content) > 0)

            # 3.2 带 If-None-Match -> 304
            req_304 = urllib.request.Request(img_url, headers={**auth_hdr, "If-None-Match": etag})
            try:
                with urllib.request.urlopen(req_304) as resp:
                    self.assertEqual(resp.status, 304)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 304)

            # 3.3 视频 Range: bytes=0-15 -> 206 + Content-Range
            vid_url = f"{base_url}/api/wechat/media/{self.info.video_asset_id}/video.mp4"
            req_range = urllib.request.Request(vid_url, headers={**auth_hdr, "Range": "bytes=0-15"})
            with urllib.request.urlopen(req_range) as resp:
                self.assertEqual(resp.status, 206)
                cr = resp.headers.get("Content-Range")
                self.assertIsNotNone(cr, "Expected Content-Range header in response")
                self.assertTrue(cr.startswith("bytes 0-15/"))
                range_bytes = resp.read()
                self.assertEqual(len(range_bytes), 16)

        finally:
            server.shutdown()
            server.server_close()
            srv_thread.join(timeout=3)

    def test_04_webhook_bridge(self):
        """4. webhook 桥: clients/assistant_webhook.format_payload 对合成消息产出 custom/dify/feishu 载荷。"""
        # Synthetic message payload with media
        msg_img = {
            "id": "msg_001",
            "sender": "synth_sender_01",
            "ts": "2026-10-27T12:00:00+08:00",
            "kind": "image",
            "text": "微信导出测试",
            "note": "图片",
            "media": {
                "kind": "image",
                "asset_id": self.info.image_asset_id,
                "url": f"/api/wechat/media/{self.info.image_asset_id}/image.png",
                "anchor": {
                    "chat_name": "合成测试群",
                    "sender": "张三",
                },
                "ocr": {
                    "text": "微 信 导 出 测试 2026",
                },
            },
        }

        msg_vid = {
            "id": "msg_002",
            "sender": "synth_sender_01",
            "ts": "2026-10-27T12:00:10+08:00",
            "kind": "video",
            "text": "",
            "note": "视频",
            "media": {
                "kind": "video",
                "asset_id": self.info.video_asset_id,
                "url": f"/api/wechat/media/{self.info.video_asset_id}/video.mp4",
                "poster": f"/api/wechat/media/{self.info.video_asset_id}/thumb.jpg",
                "anchor": {
                    "chat_name": "合成测试群",
                    "sender": "张三",
                },
            },
        }

        export_url = "http://127.0.0.1:8765/api/wechat/export"

        # 4.1 Custom payload
        custom_payload = assistant_webhook.format_payload(
            webhook_type="custom",
            chat_id=SYNTH_CHAT,
            msg=msg_img,
            export_url=export_url,
        )
        self.assertEqual(
            custom_payload.get("media_url_abs"),
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.image_asset_id}/image.png",
        )
        self.assertEqual(custom_payload.get("chat_name"), "合成测试群")
        self.assertEqual(custom_payload.get("ocr_text"), "微 信 导 出 测试 2026")

        custom_vid = assistant_webhook.format_payload(
            webhook_type="custom",
            chat_id=SYNTH_CHAT,
            msg=msg_vid,
            export_url=export_url,
        )
        self.assertEqual(
            custom_vid.get("media_poster_url_abs"),
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.video_asset_id}/thumb.jpg",
        )

        # 4.2 Dify payload
        dify_payload = assistant_webhook.format_payload(
            webhook_type="dify",
            chat_id=SYNTH_CHAT,
            msg=msg_img,
            export_url=export_url,
        )
        inputs = dify_payload.get("inputs", {})
        self.assertEqual(
            inputs.get("media_url_abs"),
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.image_asset_id}/image.png",
        )
        self.assertEqual(inputs.get("chat_name"), "合成测试群")
        self.assertEqual(inputs.get("ocr_text"), "微 信 导 出 测试 2026")

        dify_vid = assistant_webhook.format_payload(
            webhook_type="dify",
            chat_id=SYNTH_CHAT,
            msg=msg_vid,
            export_url=export_url,
        )
        self.assertEqual(
            dify_vid.get("inputs", {}).get("media_poster_url_abs"),
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.video_asset_id}/thumb.jpg",
        )

        # 4.3 Feishu payload
        feishu_payload = assistant_webhook.format_payload(
            webhook_type="feishu",
            chat_id=SYNTH_CHAT,
            msg=msg_img,
            export_url=export_url,
        )
        feishu_text = feishu_payload.get("content", {}).get("text", "")
        self.assertIn(
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.image_asset_id}/image.png",
            feishu_text,
        )

        feishu_vid = assistant_webhook.format_payload(
            webhook_type="feishu",
            chat_id=SYNTH_CHAT,
            msg=msg_vid,
            export_url=export_url,
        )
        feishu_vid_text = feishu_vid.get("content", {}).get("text", "")
        self.assertIn(
            f"http://127.0.0.1:8765/api/wechat/media/{self.info.video_asset_id}/thumb.jpg",
            feishu_vid_text,
        )

    def test_05_probe_audit_and_vision(self):
        """5. probe: clients/muse_probe.run_audit 返回 PASS；--vision 对视频消息产出 image_url。"""
        # Ensure worker has run
        export_data = wechat_export_api.export_wechat_data(
            vault_dir=self.info.decrypted_dir,
            since_epoch=self.info.base_time,
            to_epoch=self.info.base_time + 100,
            media_filter="all",
            ocr_mode="full",
            blocklist=[BLOCKED_CHAT],
            server_context={
                "vault_dir": str(self.info.decrypted_dir),
                "media_vault_root": str(self.info.vault_dir),
                "media_policy_path": str(self.info.policy_path),
                "media_ledger_path": str(self.info.ledger_path),
                "blocklist": [BLOCKED_CHAT],
            },
        )

        # 5.1 run_audit 返回 PASS
        passed, report = muse_probe.run_audit(
            data=export_data,
            ocr_enabled=False,  # offline synthetic audit
        )
        self.assertTrue(passed, f"muse_probe.run_audit failed:\n{report}")
        self.assertIn("AUDIT: PASS", report)

        # 5.2 --vision 对视频消息产出 image_url (poster 存在时)
        vision_output = muse_probe.run_vision(
            data=export_data,
            only="video",
            image_dir=str(self.info.vault_dir / "media"),
            input_file=None,
        )

        parsed_payload: dict | None = None
        s_idx = vision_output.find("{")
        e_idx = vision_output.rfind("}")
        if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
            try:
                parsed_payload = json.loads(vision_output[s_idx : e_idx + 1])
            except Exception:
                pass
        self.assertIsNotNone(parsed_payload, f"Failed to extract JSON payload from vision output:\n{vision_output}")
        messages = parsed_payload.get("messages", [])
        self.assertTrue(len(messages) > 0)
        content_items = messages[0].get("content", [])
        img_items = [it for it in content_items if it.get("type") == "image_url"]
        self.assertEqual(len(img_items), 1, f"Expected 1 image_url item in vision payload: {content_items}")
        url_val = img_items[0].get("image_url", {}).get("url", "")
        self.assertTrue(url_val.startswith("data:image/"), f"Expected data URL for image_url: {url_val[:40]}")

    def test_06_failure_path_readonly_state(self):
        """6. 失败路径: 把 state 目录设为只读后跑 worker，退出码非 0 且 stderr 含 PermissionError 或 [ERROR]。"""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("chmod read-only restriction is bypassed by superuser (geteuid == 0)")
        state_dir = self.info.vault_dir / "state"
        original_mode = state_dir.stat().st_mode
        try:
            # Set state dir read-only
            state_dir.chmod(0o555)

            # Ensure any lock file cannot be opened for writing
            lock_file = state_dir / "media.lock"
            if lock_file.exists():
                lock_file.chmod(0o444)

            res = self._run_worker(self.info.vault_dir, self.info.policy_path)
            self.assertNotEqual(
                res.returncode,
                0,
                f"Worker unexpectedly succeeded on read-only state dir! stdout:\n{res.stdout}\nstderr:\n{res.stderr}",
            )
            has_error_sig = (
                "PermissionError" in res.stderr
                or "[ERROR]" in res.stderr
                or "Permission denied" in res.stderr
                or "PermissionError" in res.stdout
                or "[ERROR]" in res.stdout
            )
            self.assertTrue(
                has_error_sig,
                f"stderr/stdout missing PermissionError or [ERROR]:\nstderr:\n{res.stderr}\nstdout:\n{res.stdout}",
            )
        finally:
            state_dir.chmod(original_mode)
            lock_file = state_dir / "media.lock"
            if lock_file.exists():
                lock_file.chmod(0o644)


if __name__ == "__main__":
    unittest.main()
