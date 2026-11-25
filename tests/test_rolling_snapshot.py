#!/usr/bin/env python3
"""Consumer-visible tests for the rolling export snapshot feature.

Covers:
(a) `--generate-snapshot` worker mode writes an atomic JSON snapshot and exits,
    even when no auth token is configured.
(b) `/api/wechat/export/rolling` auth (401), availability (503), and byte-exact
    streaming (200 + X-Snapshot-Generated-At).
(c) fcntl.flock mutual exclusion: concurrent generation never leaves a partial
    or duplicate snapshot.
(d) Worker exit-code policy (real CLI subprocesses): 0 for a written snapshot and
    for benign skips (lock held, vault not ready), non-zero with an [ERROR] line
    for real failures, so `ExecStartPost=` never paints a healthy sync red.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_ROOT, _ROOT / "bin", _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from bin import wechat_export_api  # noqa: E402


def _load_paging_helper():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "paging_fixture", _TESTS_DIR / "test_api_paging.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._build_test_databases


_build_test_databases = _load_paging_helper()

TOKEN = "rolling-test-token"


def _build_vault(root: Path) -> Path:
    decrypted = root / "vault" / "decrypted" / "current"
    _build_test_databases(
        decrypted,
        chats=[("wxid_alice", "爱丽丝", "Alice", False)],
        messages=[
            ("wxid_alice", 1, 1759000010, 1, "Alice msg 1"),
            ("wxid_alice", 2, 1759000020, 1, "Alice msg 2"),
        ],
    )
    return decrypted


class GenerateSnapshotWorkerTests(unittest.TestCase):
    def _run_worker(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(_ROOT / "bin" / "wechat_export_api.py"),
                "--generate-snapshot",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_worker_exits_zero_without_token_and_writes_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_dir": str(decrypted)}), encoding="utf-8")

            env = dict(os.environ)
            env["WECHAT_ROLLING_SNAPSHOT_PATH"] = str(out)
            env["WECHAT_ROLLING_SNAPSHOT_LOCK"] = str(lock)
            proc = self._run_worker(
                "--config", str(cfg), "--snapshot-output", str(out), env=env
            )
            self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")

            self.assertTrue(out.is_file(), "snapshot output was not written")
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("generated_at", data)
            self.assertRegex(data["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
            self.assertIn("since", data)
            self.assertIn("to", data)
            self.assertIn("payload_stats", data)
            # Atomic write leaves no temp files behind.
            leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp_")]
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")
            self.assertIn("Rolling snapshot generated", proc.stderr)

    def test_lock_contention_exits_zero_logs_skip_and_keeps_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps({"vault_dir": str(decrypted), "snapshot_lock": str(lock)}),
                encoding="utf-8",
            )
            # A previous run's snapshot: a skipping run must neither delete nor
            # overwrite it (and must not leave a partial replacement).
            out.write_text('{"sentinel": "previous run"}', encoding="utf-8")

            holder = os.open(str(lock), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                proc = self._run_worker("--config", str(cfg), "--snapshot-output", str(out))
            finally:
                os.close(holder)

            self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertRegex(proc.stderr, r"\[INFO\] Another snapshot generation is in progress")
            self.assertIn("skipping", proc.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), '{"sentinel": "previous run"}')
            leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp_")]
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_vault_not_ready_exits_zero_with_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Decrypted directory exists but has no `message` subdir: WeChat is not
            # logged in / not decrypted yet, so the decrypt sync itself skipped.
            decrypted = root / "vault" / "decrypted" / "current"
            decrypted.mkdir(parents=True)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps({"vault_dir": str(decrypted), "snapshot_lock": str(lock)}),
                encoding="utf-8",
            )

            proc = self._run_worker("--config", str(cfg), "--snapshot-output", str(out))

            self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertRegex(proc.stderr, r"\[WARN\] Vault not ready")
            self.assertFalse(out.exists(), "no snapshot should be produced when the vault is not ready")

    def test_unwritable_output_path_exits_nonzero_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            blocker = root / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            out = blocker / "rolling.json"  # parent is a regular file: mkdir fails
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_dir": str(decrypted)}), encoding="utf-8")

            proc = self._run_worker("--config", str(cfg), "--snapshot-output", str(out))

            self.assertNotEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertRegex(proc.stderr, r"\[ERROR\] Rolling snapshot generation failed")
            self.assertFalse(out.exists())

    def test_export_failure_exits_nonzero_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps({"vault_dir": str(decrypted), "snapshot_lock": str(lock)}),
                encoding="utf-8",
            )
            # A NaN window reaches export_wechat_data() and makes it raise while
            # building the range: this is the "export raised" failure class, run
            # through the real CLI so main()'s exit-code handling is observable.
            proc = self._run_worker(
                "--config", str(cfg), "--snapshot-output", str(out), "--window-hours", "nan"
            )

            self.assertNotEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertRegex(proc.stderr, r"\[ERROR\] Rolling snapshot generation failed")
            self.assertFalse(out.exists(), "a failed export must not publish a snapshot")

    def test_corrupt_config_exits_nonzero_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "rolling.json"
            cfg = root / "config.json"
            cfg.write_text("{not valid json", encoding="utf-8")

            proc = self._run_worker("--config", str(cfg), "--snapshot-output", str(out))

            self.assertNotEqual(proc.returncode, 0, f"stderr={proc.stderr}\nstdout={proc.stdout}")
            self.assertRegex(proc.stderr, r"\[ERROR\] Rolling snapshot generation failed")
            self.assertIn("Failed to load config", proc.stderr)
            self.assertFalse(out.exists())


class GenerateSnapshotFailureTests(unittest.TestCase):
    def test_export_exception_propagates_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_dir": str(decrypted)}), encoding="utf-8")

            with mock.patch.object(
                wechat_export_api, "export_wechat_data", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    wechat_export_api.generate_rolling_snapshot(
                        config_path=cfg, output_path=out, lock_path=lock
                    )
            self.assertFalse(out.exists(), "a failed export must not publish a snapshot")

            # The finally: block must still release the lock, so a later run works.
            status, payload = wechat_export_api.generate_rolling_snapshot(
                config_path=cfg, output_path=out, lock_path=lock
            )
            self.assertEqual(status, wechat_export_api.SNAPSHOT_OK)
            self.assertIsNotNone(payload)
            self.assertTrue(out.is_file())


class RollingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp_dir.name)
        cls.snapshot = cls.root / "rolling.json"
        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=cls.root,
            token=TOKEN,
            snapshot_path=str(cls.snapshot),
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
        cls.tmp_dir.cleanup()

    def _get(self, token: str | None = TOKEN) -> tuple[int, bytes, dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/wechat/export/rolling", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_missing_snapshot_returns_503(self) -> None:
        if self.snapshot.exists():
            self.snapshot.unlink()
        status, body, _ = self._get()
        self.assertEqual(status, 503, body)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "snapshot_unavailable")

    def test_missing_auth_returns_401(self) -> None:
        self.snapshot.write_bytes(b'{"generated_at": "2026-11-25T19:00:00+08:00"}')
        status, body, _ = self._get(token=None)
        self.assertEqual(status, 401, body)

    def test_existing_snapshot_streams_byte_exact_with_header(self) -> None:
        payload = {
            "generated_at": "2026-11-25T19:15:04+08:00",
            "since": "2026-11-25T13:15:04+08:00",
            "to": "2026-11-25T19:15:04+08:00",
            "messages": 890,
            "padding": "x" * 4096,
        }
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.snapshot.write_bytes(raw)
        status, body, headers = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body, raw, "streamed body must equal on-disk snapshot bytes")
        self.assertEqual(headers.get("X-Snapshot-Generated-At"), payload["generated_at"])
        self.assertEqual(int(headers.get("Content-Length", "0")), len(raw))


class FlockMutualExclusionTests(unittest.TestCase):
    def test_concurrent_generation_skips_and_never_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decrypted = _build_vault(root)
            out = root / "rolling.json"
            lock = root / ".snapshot.lock"
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"vault_dir": str(decrypted)}), encoding="utf-8")

            entered = threading.Event()
            release = threading.Event()
            real_export = wechat_export_api.export_wechat_data

            def slow_export(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(10), "first generator was never released")
                return real_export(*args, **kwargs)

            results: dict[str, tuple[str, object]] = {}

            def run(name: str) -> None:
                results[name] = wechat_export_api.generate_rolling_snapshot(
                    config_path=cfg, output_path=out, lock_path=lock
                )

            with mock.patch.object(wechat_export_api, "export_wechat_data", slow_export):
                first = threading.Thread(target=run, args=("first",), daemon=True)
                first.start()
                self.assertTrue(entered.wait(5), "first generator never entered export")
                # While the lock is held and no snapshot exists yet, a competing
                # generator must bail out rather than write a second/partial file.
                second = threading.Thread(target=run, args=("second",), daemon=True)
                second.start()
                second.join(timeout=5)
                self.assertFalse(second.is_alive(), "second generator blocked on flock")
                self.assertEqual(
                    results.get("second"),
                    (wechat_export_api.SNAPSHOT_SKIP_LOCKED, None),
                    "second generator should report a benign lock skip",
                )
                self.assertFalse(out.exists(), "snapshot appeared before os.replace()")
                release.set()
                first.join(timeout=10)
                self.assertFalse(first.is_alive(), "first generator did not finish")

            self.assertEqual(results.get("first")[0], wechat_export_api.SNAPSHOT_OK)
            self.assertIsNotNone(results.get("first")[1])
            self.assertTrue(out.is_file())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertRegex(data["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
            # Exactly one generated_at key/value (no concatenated halves).
            self.assertEqual(
                len(re.findall(r'"generated_at"', out.read_text(encoding="utf-8"))), 1
            )
            leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp_")]
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
