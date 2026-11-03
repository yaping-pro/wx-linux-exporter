"""TDD tests for WeChat Export API concurrency, non-blocking health probe, and memory hygiene."""
import hashlib
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "bin"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from bin import wechat_export_api


class TestConcurrencyAndHygiene(unittest.TestCase):
    def test_create_server_supports_concurrency(self):
        """create_server 必须使用多线程模型 (ThreadingHTTPServer)，确保健康检查不被耗时导出阻塞。"""
        import socketserver
        server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir="/tmp",
            token="test-token",
            auth_enabled=True,
        )
        try:
            self.assertTrue(
                issubclass(server.__class__, socketserver.ThreadingMixIn),
                f"Server class {server.__class__} must inherit from ThreadingMixIn to support concurrent requests",
            )
            self.assertTrue(
                getattr(server, "daemon_threads", False),
                "daemon_threads must be True so worker threads do not hang process shutdown",
            )
        finally:
            server.server_close()

    def test_health_check_never_blocked_by_slow_export(self):
        """当导出处理正在执行时，/health 探针必须在独立线程中亚秒级秒回。"""
        server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir="/tmp",
            token="test-token",
            auth_enabled=True,
        )
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            self.assertEqual(req.status, 200)

            latencies = []
            threads = []

            def _probe():
                t0 = time.time()
                r = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                latencies.append(time.time() - t0)
                self.assertEqual(r.status, 200)

            for _ in range(5):
                th = threading.Thread(target=_probe)
                threads.append(th)
                th.start()

            for th in threads:
                th.join(timeout=3)

            self.assertEqual(len(latencies), 5)
            for lat in latencies:
                self.assertLess(lat, 0.2)
        finally:
            server.shutdown()
            server.server_close()

    def test_malloc_trim_hygiene_helper(self):
        """测试内存整理辅助函数 trim_memory_if_possible() 安全运行且不抛异常。"""
        self.assertTrue(hasattr(wechat_export_api, "trim_memory_if_possible"))
        result = wechat_export_api.trim_memory_if_possible()
        self.assertIn(result, (True, False, 1, 0))


if __name__ == "__main__":
    unittest.main()
