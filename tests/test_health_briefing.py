#!/usr/bin/env python3
"""回归测试：/api/health/briefing 必须复用 Bearer 鉴权并原样转发快照。

守护两个真实风险：
1) 新端点误走匿名分支 —— 健康简报含主机明细，未带 token 必须 401。
2) 端点自行采集或改写内容 —— 它只能读取 generate-report.py 写下的快照，
   返回的 markdown 必须与 Telegram 推送原文逐字一致。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_export_api  # noqa: E402

TOKEN = "test-token-not-a-real-secret"


class HealthBriefingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="wx-briefing-test-")
        cls.briefing_path = Path(cls.tmp) / "briefing.json"
        cls.payload = {
            "generated_at": "2026-10-17T09:01:02+08:00",
            "host": "linux1",
            "status": "green",
            "markdown": "🟢 【家服巡检简报】全绿健康\n**状态**: 全系统核心指标全部正常，零异常",
            "metrics": {"load_1": 0.52, "systemd_failed": 0},
            "alerts": [],
        }
        cls.briefing_path.write_text(
            json.dumps(cls.payload, ensure_ascii=False), encoding="utf-8"
        )
        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=Path(cls.tmp) / "vault",
            token=TOKEN,
            briefing_path=str(cls.briefing_path),
        )
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_unauthenticated_briefing_is_rejected(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/health/briefing")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("无 token 的简报请求本应被拒绝")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_briefing_is_returned_verbatim(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health/briefing",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body, self.payload)

    def test_missing_snapshot_reports_503(self):
        self.briefing_path.unlink()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health/briefing",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("快照缺失时不应返回 200")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)


if __name__ == "__main__":
    unittest.main()
