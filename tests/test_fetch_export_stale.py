#!/usr/bin/env python3
"""Regression tests for clients/fetch_export.py empty-but-stale detection.

2026-11-13 incident: WeChat logged out, export served chats=[] with
watermark=range.to=now. The consumer compared watermark != last and reported
"advanced" for 261 consecutive runs. These tests pin the fix: an empty run must
become STALE once the last non-empty run is older than the freshness limit.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "clients") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "clients"))

import fetch_export  # noqa: E402

FRESHNESS_MIN = 180


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _payload(message_count, chats=None, watermark=None):
    wm = watermark or _now()
    return {
        "exported_at": _iso(_now()),
        "range": {"from": _iso(_now() - datetime.timedelta(hours=1)), "to": _iso(wm)},
        "watermark": _iso(wm),
        "next_cursor": None,
        "chats": chats or [],
        "manifest": {"sha256": "0" * 64, "message_count": message_count},
        "payload_stats": {"messages": message_count, "media_included": 0, "ocr_chars": 0, "truncated": False},
    }


def _health_legacy():
    """Older daemon: no source freshness fields (back-compat path)."""
    return {
        "status": "ok",
        "uptime_seconds": 10000,
        "uds_connected": True,
        "total_messages": 42,
    }


def _health_source(status=None, rows=None, max_ts=None):
    payload = _health_legacy()
    if status is not None:
        payload["source_status"] = status
    if rows is not None:
        payload["source_rows"] = rows
    if max_ts is not None:
        payload["source_max_ts"] = max_ts
    return payload


class FetchExportStaleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self._tmp.name) / "state.json"
        self.patches = [
            mock.patch.object(fetch_export, "STATE_FILE", str(self.state_file)),
            mock.patch.object(fetch_export, "FRESHNESS_MIN", FRESHNESS_MIN),
            mock.patch.object(fetch_export, "MAX_IDLE_MIN", 1440),
            mock.patch.object(fetch_export, "notify_fail"),
            mock.patch.object(fetch_export, "FETCH_RETRIES", 1),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def _run(self, payload, state=None, health=None):
        if state is not None:
            self.state_file.write_text(json.dumps(state), encoding="utf-8")
        health_payload = health or _health_legacy()

        def fake_fetch(url, token=None, retries=None, delays=None, budget=None):
            if url.endswith("/health"):
                return 200, json.dumps(health_payload), None
            return 200, json.dumps(payload), None

        out = io.StringIO()
        with mock.patch.object(fetch_export, "fetch", side_effect=fake_fetch), \
                contextlib.redirect_stdout(out):
            rc = fetch_export.main()
        return rc, out.getvalue()

    def test_empty_run_with_old_last_nonempty_is_stale_legacy_daemon(self):
        """老版本 daemon（无 source_* 字段）：回退 last_nonempty_at 年龄规则。"""
        old = _now() - datetime.timedelta(minutes=FRESHNESS_MIN + 1)
        rc, out = self._run(
            _payload(0),
            state={"last_watermark": _iso(_now() - datetime.timedelta(minutes=5)),
                   "last_nonempty_at": _iso(old)},
        )
        self.assertEqual(rc, 1)
        self.assertIn("RESULT   : STALE", out)
        self.assertIn("last non-empty export was", out)
        self.assertIn(old.isoformat(), out)
        # Stale runs must not overwrite the state used to prove the outage.
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_nonempty_at"], _iso(old))

    def test_empty_run_with_stale_source_status_is_stale(self):
        """0 消息 + /health source_status=stale：即便 last_nonempty 很新也必须 STALE。"""
        stale_max = (_now() - datetime.timedelta(minutes=FRESHNESS_MIN + 30)).isoformat()
        rc, out = self._run(
            _payload(0),
            state={"last_nonempty_at": _iso(_now() - datetime.timedelta(minutes=1))},
            health=_health_source(status="stale", rows=120, max_ts=stale_max),
        )
        self.assertEqual(rc, 1)
        self.assertIn("RESULT   : STALE", out)
        self.assertIn("export source is frozen", out)
        self.assertIn("source_status=stale", out)

    def test_empty_run_with_stale_source_max_ts_is_stale(self):
        """无 source_status，但 source_max_ts 超限：来源冻结同样 STALE；覆盖 'Z' 后缀解析。"""
        stale_dt = _now() - datetime.timedelta(minutes=FRESHNESS_MIN + 30)
        stale_max = _iso(stale_dt).replace("+00:00", "Z")
        rc, out = self._run(
            _payload(0),
            state={"last_nonempty_at": _iso(_now() - datetime.timedelta(minutes=1))},
            health=_health_source(rows=120, max_ts=stale_max),
        )
        self.assertEqual(rc, 1)
        self.assertIn("RESULT   : STALE", out)
        self.assertIn(stale_max, out)

    def test_empty_run_with_fresh_source_is_quiet_not_stale(self):
        """0 消息 + source_status=fresh：合法静默期 -> ok(quiet)，不因 last_nonempty 变旧而告警。"""
        old = _now() - datetime.timedelta(minutes=FRESHNESS_MIN * 4)
        fresh_max = _iso(_now()).replace("+00:00", "Z")
        rc, out = self._run(
            _payload(0),
            state={"last_watermark": _iso(_now() - datetime.timedelta(minutes=5)),
                   "last_nonempty_at": _iso(old)},
            health=_health_source(status="fresh", rows=120, max_ts=fresh_max),
        )
        self.assertEqual(rc, 0)
        self.assertIn("RESULT   : ok(quiet)", out)
        self.assertNotIn("STALE", out)
        self.assertIn(fresh_max, out)

    def test_empty_run_without_history_bootstraps_and_succeeds(self):
        rc, out = self._run(_payload(0), state={})
        self.assertEqual(rc, 0)
        self.assertNotIn("STALE", out)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        bootstrapped = datetime.datetime.fromisoformat(saved["last_nonempty_at"])
        self.assertLess(
            abs((_now() - bootstrapped).total_seconds()), 30,
            "bootstrap should be ~now",
        )

    def test_nonempty_run_advances_last_nonempty(self):
        old = _now() - datetime.timedelta(minutes=FRESHNESS_MIN + 60)
        ts = int(_now().timestamp())
        chats = [{
            "chat_id": "wxid_test",
            "name": "测试",
            "kind": "single",
            "messages": [{"local_id": 1, "ts": ts, "kind": "text", "text": "hi"}],
        }]
        rc, out = self._run(
            _payload(1, chats=chats),
            state={"last_nonempty_at": _iso(old)},
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("STALE", out)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        advanced = datetime.datetime.fromisoformat(saved["last_nonempty_at"])
        self.assertLess(abs((_now() - advanced).total_seconds()), 30)


if __name__ == "__main__":
    unittest.main()