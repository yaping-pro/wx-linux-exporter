#!/usr/bin/env python3
"""Tests for deploy/host/wechat-login-guard.

What is under test is the *decision state machine* and the safety invariant, not
the host's X server: every external command (xwininfo, ffmpeg, tesseract, xdotool)
is a stub in a throwaway PATH, and the frames they emit are synthetic.

Incident coverage (see docs/OPS.md §7):
  * 2026-11-13 / 2026-11-16 — WeChat sat on the 292x396 login panel after a host
    reboot for days (4d / 16h) until a human acted.
    -> the residual-session panel must be clicked automatically.
  * A *real* scannable QR is a human task; clicking its area can hit
    "Switch Account" / "Transfer files only".
    -> pixel veto: a bimodal, high-transition card must never be clicked, even if
       OCR also reads 进入微信.

Calibration vector (window origin +494,+202, card abs (598,278)-(684,359)):
mean 81.1, std 54.2, frac>200 0.048, frac<70 0.446, transitions/row ~3.7.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "deploy" / "host" / "wechat-login-guard"
SERVICE = ROOT / "deploy" / "systemd" / "wechat-login-guard.service"
TIMER = ROOT / "deploy" / "systemd" / "wechat-login-guard.timer"

PANE_W, PANE_H = 292, 396
CARD = (104, 76, 190, 157)
ORIGIN = (494, 202)
WHOLE_TEXT = 'WeChat \n登录'
BUTTON_LABEL_TEXT = '进入微信'


def _load_guard():
    """The script has no .py suffix, so load it by path."""
    loader = SourceFileLoader("wechat_login_guard", str(GUARD))
    spec = importlib.util.spec_from_loader("wechat_login_guard", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    loader.exec_module(module)
    return module


guard = _load_guard()


# --- synthetic frames --------------------------------------------------------
def skeleton_frame() -> bytes:
    """The gray placeholder: 45% dark block left, 55% mid-gray, ~1 transition/row."""
    frame = bytearray([200] * (PANE_W * PANE_H))
    x0, y0, x1, y1 = CARD
    split = 39
    for y in range(y0, y1):
        base = y * PANE_W
        for x in range(x0, x0 + split):
            frame[base + x] = 20
        for x in range(x0 + split, x1):
            frame[base + x] = 130
    return bytes(frame)


def real_qr_frame() -> bytes:
    """Bimodal checkerboard: frac>200 0.5, frac<70 0.5, ~85 transitions/row."""
    frame = bytearray([200] * (PANE_W * PANE_H))
    x0, y0, x1, y1 = CARD
    for y in range(y0, y1):
        base = y * PANE_W
        for x in range(x0, x1):
            frame[base + x] = 255 if (x + y) % 2 == 0 else 0
    return bytes(frame)


def stats_of(frame: bytes):
    return guard.compute_stats(frame, PANE_W, CARD)


def pane_geometry(w=PANE_W, h=PANE_H, x=ORIGIN[0], y=ORIGIN[1]):
    return guard.WindowGeometry("0x1a00003", x, y, w, h)


def label_ocr(text=BUTTON_LABEL_TEXT):
    return guard.interpret_ocr(WHOLE_TEXT, text)


# --- expectation helpers -----------------------------------------------------
def tree_line(w, h, x, y) -> str:
    return f'     0x1a00003 "Weixin": ("wechat" "Weixin")  {w}x{h}+0+0  +{x}+{y}'


class GuardHarness(unittest.TestCase):
    """Subprocess harness: stub PATH + overridable seams, as on the host."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.boot_id = self.dir / "boot_id"
        self.boot_id.write_text("boot-test-1\n")
        self.xdotool_log = self.dir / "xdotool.log"
        self.tree = self.dir / "xwininfo.txt"
        self.tree_after = self.dir / "xwininfo.after.txt"
        self.frame = self.dir / "frame.raw"
        self.ocr_whole = self.dir / "ocr.whole"
        self.ocr_button = self.dir / "ocr.button"
        self.ocr_whole.write_text(WHOLE_TEXT)
        self.ocr_button.write_text(BUTTON_LABEL_TEXT)
        self.frame.write_bytes(skeleton_frame())
        self.tree.write_text(tree_line(PANE_W, PANE_H, *ORIGIN) + "\n")
        self._stub_command("xwininfo", """
            #!/bin/sh
            n=$(cat "$XWIN_COUNT" 2>/dev/null || echo 0)
            n=$((n+1)); echo "$n" > "$XWIN_COUNT"
            if [ "$n" -ge 2 ] && [ -f "$FAKE_XWININFO_AFTER" ]; then
                cat "$FAKE_XWININFO_AFTER"
            else
                cat "$FAKE_XWININFO"
            fi
        """)
        self._stub_command("ffmpeg", """
            #!/bin/sh
            if [ -n "$FAKE_FRAME_FAIL" ]; then echo "simulated grab failure" >&2; exit 1; fi
            cat "$FAKE_FRAME"
        """)
        self._stub_command("xdotool", """
            #!/bin/sh
            printf '%s\\n' "$*" >> "$XDOTOOL_LOG"
            exit 0
        """)
        self._stub_command("tesseract", """
            #!/bin/sh
            if [ "$1" = "--list-langs" ]; then
                printf 'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (2):\\n'
                [ -n "$FAKE_NO_CHI_SIM" ] && printf 'eng\\n' || printf 'chi_sim\\neng\\n'
                exit 0
            fi
            case "$1" in
                *frame-whole.pgm) cat "$FAKE_OCR_WHOLE" ;;
                *frame-button.pgm) cat "$FAKE_OCR_BUTTON" ;;
                *) : ;;
            esac
            exit 0
        """)

    def tearDown(self):
        self._tmp.cleanup()

    def _stub_command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(body).lstrip())
        path.chmod(0o755)

    def state_path(self) -> Path:
        return self.dir / "state" / "state.json"

    def run_guard(self, *args, **overrides):
        count = self.dir / "xwin_count"
        if count.exists():
            count.unlink()
        environ = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "DISPLAY": ":99",
            "GUARD_STATE_DIR": str(self.dir / "state"),
            "GUARD_LOCK_PATH": str(self.dir / "guard.lock"),
            "GUARD_BOOT_ID_PATH": str(self.boot_id),
            "GUARD_SETTLE_SEC": "0",
            "FAKE_XWININFO": str(self.tree),
            "FAKE_XWININFO_AFTER": "",
            "FAKE_FRAME": str(self.frame),
            "FAKE_FRAME_FAIL": "",
            "FAKE_OCR_WHOLE": str(self.ocr_whole),
            "FAKE_OCR_BUTTON": str(self.ocr_button),
            "FAKE_NO_CHI_SIM": "",
            "XDOTOOL_LOG": str(self.xdotool_log),
            "XWIN_COUNT": str(count),
        }
        environ.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(
            [sys.executable, str(GUARD), *args],
            env=environ, capture_output=True, text=True, timeout=120,
        )

    def xdotool_calls(self) -> list[str]:
        if not self.xdotool_log.exists():
            return []
        return [line for line in self.xdotool_log.read_text().splitlines() if line]


# --- pure decision tests -----------------------------------------------------
class ClassifierTest(unittest.TestCase):
    def test_skeleton_vector_is_skeleton(self):
        stats = stats_of(skeleton_frame())
        self.assertAlmostEqual(stats.mean, 81.0, delta=3.0)
        self.assertAlmostEqual(stats.std, 54.0, delta=6.0)
        self.assertAlmostEqual(stats.frac_lo, 0.45, delta=0.05)
        self.assertLess(stats.frac_hi, 0.01)
        self.assertLess(stats.transitions, 2.0)
        verdict, failures = guard.classify_qr(stats)
        self.assertEqual(verdict, "SKELETON", failures)

    def test_real_qr_is_vetoed(self):
        stats = stats_of(real_qr_frame())
        self.assertGreater(stats.frac_hi, 0.30)
        self.assertGreater(stats.transitions, 10.0)
        verdict, failures = guard.classify_qr(stats)
        self.assertEqual(verdict, "REAL_QR_OR_UNKNOWN")
        joined = " ".join(failures)
        self.assertIn("frac_hi", joined)
        self.assertIn("trans", joined)

    def test_threshold_edges(self):
        # Exactly at every boundary: still SKELETON (checks use strict >=).
        ok = guard.QrStats(mean=100.0, std=79.9, frac_hi=0.149, frac_lo=0.699,
                           transitions=7.99)
        self.assertEqual(guard.classify_qr(ok)[0], "SKELETON")
        for bad in (
            guard.QrStats(mean=54.9, std=10.0, frac_hi=0.0, frac_lo=0.1, transitions=1.0),
            guard.QrStats(mean=74.0, std=80.0, frac_hi=0.0, frac_lo=0.1, transitions=1.0),
            guard.QrStats(mean=74.0, std=10.0, frac_hi=0.15, frac_lo=0.1, transitions=1.0),
            guard.QrStats(mean=74.0, std=10.0, frac_hi=0.0, frac_lo=0.70, transitions=1.0),
            guard.QrStats(mean=74.0, std=10.0, frac_hi=0.0, frac_lo=0.1, transitions=8.0),
        ):
            self.assertEqual(guard.classify_qr(bad)[0], "REAL_QR_OR_UNKNOWN")


class OcrTextTest(unittest.TestCase):
    def test_label_tolerates_ocr_noise(self):
        self.assertTrue(guard.fuzzy_contains(guard.normalize_ocr("进 入 微 信"), "进入微信"))
        self.assertTrue(guard.fuzzy_contains("x进入微伩y", "进入微信"))
        self.assertFalse(guard.fuzzy_contains("进入微", "进入微信"))
        self.assertTrue(guard.fuzzy_contains("进入网信", "进入微信"))  # one garbled char
        self.assertFalse(guard.fuzzy_contains("进人网信", "进入微信"))  # two

    def test_ocr_result_reports_veto(self):
        result = guard.interpret_ocr("请扫码登录", "进入微信")
        self.assertIn("扫码登录", result.veto_hits)
        self.assertTrue(result.label_found)


class DecideTest(unittest.TestCase):
    def test_click_when_every_gate_passes(self):
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()), label_ocr())
        self.assertTrue(decision.click)
        self.assertEqual(decision.event, "login_panel_skeleton")

    def test_logged_in_width_never_clicks(self):
        decision = guard.decide(pane_geometry(w=917, h=668, x=0, y=0),
                                stats_of(skeleton_frame()), label_ocr())
        self.assertFalse(decision.click)
        self.assertEqual(decision.event, "logged_in")

    def test_geometry_tolerance_edges(self):
        for w, h in ((300, 404), (284, 388), (292, 396)):
            decision = guard.decide(pane_geometry(w=w, h=h), stats_of(skeleton_frame()),
                                    label_ocr())
            self.assertTrue(decision.click, f"{w}x{h} should be a login panel")
        for w, h in ((301, 396), (292, 405), (283, 396), (292, 387)):
            decision = guard.decide(pane_geometry(w=w, h=h), stats_of(skeleton_frame()),
                                    label_ocr())
            self.assertFalse(decision.click, f"{w}x{h} must be unknown")
            self.assertEqual(decision.event, "unknown_geometry")

    def test_pixel_veto_beats_ocr_label(self):
        decision = guard.decide(pane_geometry(), stats_of(real_qr_frame()), label_ocr())
        self.assertFalse(decision.click)
        self.assertEqual(decision.event, "qr_like")
        self.assertTrue(decision.human)

    def test_ocr_veto_phrase_blocks_click(self):
        ocr = guard.interpret_ocr("切换账号", "进入微信")
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()), ocr)
        self.assertFalse(decision.click)
        self.assertEqual(decision.event, "ocr_veto")

    def test_missing_label_is_human_required(self):
        ocr = guard.interpret_ocr("WeChat", "")
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()), ocr)
        self.assertFalse(decision.click)
        self.assertEqual(decision.event, "ocr_no_label")
        self.assertTrue(decision.human)

    def test_ocr_failure_is_not_a_click(self):
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()), None,
                                ocr_status=guard.OCR_STATUS_FAILED)
        self.assertFalse(decision.click)
        self.assertEqual(decision.event, "ocr_failed")

    def test_degraded_mode_without_chi_sim(self):
        """chi_sim absent -> pixel classifier alone is the documented fallback."""
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()), None,
                                ocr_status=guard.OCR_STATUS_UNAVAILABLE)
        self.assertTrue(decision.click)
        real = guard.decide(pane_geometry(), stats_of(real_qr_frame()), None,
                            ocr_status=guard.OCR_STATUS_UNAVAILABLE)
        self.assertFalse(real.click)

    def test_rate_gate_after_three_clicks(self):
        state = {"boot_id": "b", "clicks": [1000.0, 2000.0], "human_required": False}
        self.assertTrue(guard.rate_gate(state, now=9000.0).allowed)
        state["clicks"] = [1000.0, 2000.0, 3000.0]
        gate = guard.rate_gate(state, now=9000.0)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "max_clicks")
        decision = guard.decide(pane_geometry(), stats_of(skeleton_frame()),
                                label_ocr(), gate)
        self.assertFalse(decision.click)
        self.assertTrue(decision.human)

    def test_min_click_interval(self):
        state = {"boot_id": "b", "clicks": [1000.0], "human_required": False}
        self.assertFalse(guard.rate_gate(state, now=1599.0).allowed)
        self.assertEqual(guard.rate_gate(state, now=1599.0).reason, "min_interval")
        self.assertTrue(guard.rate_gate(state, now=1600.0).allowed)

    def test_state_resets_on_new_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"boot_id": "old", "clicks": [1.0, 2.0, 3.0],
                                        "human_required": True}))
            fresh = guard.load_state(str(path), "new")
            self.assertEqual(fresh["clicks"], [])
            self.assertFalse(fresh["human_required"])
            same = guard.load_state(str(path), "old")
            self.assertEqual(len(same["clicks"]), 3)

    def test_state_dir_survives_a_file_squatting_on_the_fallback(self):
        """部署残留（/tmp/wechat-login-guard 是同名普通文件）不得让限流静默失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            squatted = Path(tmp) / "squat"
            squatted.write_text("leftover file")
            with mock.patch.dict(guard.os.environ, {"GUARD_STATE_DIR": str(squatted)}):
                with mock.patch.object(guard, "FALLBACK_DIR", str(Path(tmp) / "fallback")):
                    resolved = guard.resolve_state_dir()
            self.assertTrue(Path(resolved).is_dir(), resolved)
            self.assertTrue(os.access(resolved, os.W_OK))

    def test_lock_falls_back_when_parent_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            squatted = Path(tmp) / "squat"
            squatted.write_text("leftover file")
            fd = None
            try:
                fd, used = guard.acquire_lock(str(squatted / "guard.lock"), str(tmp))
                self.assertIsNotNone(fd, "lock 必须在 /run 不可写时仍能拿到")
                self.assertNotIn(str(squatted / "guard.lock"), used)
            finally:
                if fd is not None:
                    os.close(fd)

    def test_parse_geometry_prefers_absolute_origin_and_largest_window(self):
        text = "\n".join([
            '     0x0000001 "Weixin": ("wechat" "Weixin")  292x396+0+0  +494+202',
            '     0x0000002 "Weixin": ("wechat" "Weixin")  917x668+0+0  +0+0',
            '     0x0000003 "Main": ("wechat" "Main")  292x396+0+0  +0+0',
        ])
        geom = guard.parse_geometry(text)
        self.assertEqual((geom.w, geom.h, geom.x, geom.y), (917, 668, 0, 0))
        single = guard.parse_geometry(
            '     0x0000001 "Weixin": ("wechat" "Weixin")  292x396+0+0  +494+202')
        self.assertEqual((single.w, single.h, single.x, single.y), (292, 396, 494, 202))
        self.assertIsNone(guard.parse_geometry('     0x1 "Main": ("a" "Main")  10x10+0+0'))


# --- end-to-end (stub PATH) tests --------------------------------------------
class GuardScriptTest(GuardHarness):
    def test_check_never_clicks(self):
        result = self.run_guard("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("action=would_click", result.stdout)
        self.assertEqual(self.xdotool_calls(), [])
        self.assertFalse(self.state_path().exists())

    def test_check_on_logged_in_host_does_not_claim_a_click(self):
        self.tree.write_text(tree_line(917, 668, 0, 0) + "\n")
        result = self.run_guard("--check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("event=logged_in", result.stdout)
        self.assertNotIn("would_click", result.stdout)

    def test_check_never_persists_state(self):
        self.frame.write_bytes(real_qr_frame())
        result = self.run_guard("--check")
        self.assertIn("event=qr_like", result.stdout)
        self.assertIn("event=human_required", result.stdout)
        self.assertFalse(self.state_path().exists())
        self.assertEqual(self.xdotool_calls(), [])

    def test_click_restores_session(self):
        self.tree_after.write_text(tree_line(917, 668, 0, 0) + "\n")
        result = self.run_guard(FAKE_XWININFO_AFTER=self.tree_after)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.xdotool_calls()
        self.assertEqual(calls[0], f"mousemove 639 508 click 1")  # 494+145, 202+306
        self.assertEqual(calls[1], "mousemove 20 780")
        self.assertIn("event=click_restored", result.stdout)
        self.assertIn("after=917x668", result.stdout)
        state = json.loads(self.state_path().read_text())
        self.assertEqual(state["clicks"], [])

    def test_click_ineffective_exit_3(self):
        result = self.run_guard()  # geometry never changes
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("event=click_ineffective", result.stdout)
        state = json.loads(self.state_path().read_text())
        self.assertEqual(len(state["clicks"]), 1)

    def test_real_qr_never_clicks_and_alerts_once(self):
        self.frame.write_bytes(real_qr_frame())
        first = self.run_guard()
        second = self.run_guard()
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.xdotool_calls(), [])
        self.assertIn("event=qr_like", first.stdout)
        self.assertEqual(first.stdout.count("event=human_required"), 1)
        self.assertEqual(second.stdout.count("event=human_required"), 0)

    def test_capture_failure_exit_2(self):
        result = self.run_guard(FAKE_FRAME_FAIL="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("event=capture_failed", result.stdout)
        self.assertEqual(self.xdotool_calls(), [])

    def test_logged_in_host_is_a_noop(self):
        self.tree.write_text(tree_line(917, 668, 0, 0) + "\n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 0)
        self.assertIn("event=logged_in", result.stdout)
        self.assertEqual(self.xdotool_calls(), [])

    def test_no_window_is_benign(self):
        self.tree.write_text('     0x1 "Desktop": ("x" "X")  10x10+0+0\n')
        result = self.run_guard()
        self.assertEqual(result.returncode, 0)
        self.assertIn("event=no_window", result.stdout)

    def test_ocr_veto_end_to_end(self):
        self.ocr_whole.write_text("请扫码登录")
        result = self.run_guard()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.xdotool_calls(), [])
        self.assertIn("event=ocr_veto", result.stdout)

    def test_chi_sim_missing_degrades_to_pixels(self):
        self.ocr_button.write_text("")  # OCR would not find the label
        result = self.run_guard(FAKE_NO_CHI_SIM="1")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("ocr=unavailable", result.stdout)
        self.assertEqual(len(self.xdotool_calls()), 2)

    def test_rate_limit_blocks_fourth_click(self):
        state_dir = self.dir / "state"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        self.state_path().write_text(json.dumps({
            "boot_id": "boot-test-1", "human_required": False,
            "clicks": [1.0, 2.0, 3.0],
        }))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0)
        self.assertIn("event=rate_limited", result.stdout)
        self.assertEqual(self.xdotool_calls(), [])

    def test_structured_log_fields_are_present(self):
        self.tree_after.write_text(tree_line(917, 668, 0, 0) + "\n")
        result = self.run_guard(FAKE_XWININFO_AFTER=self.tree_after)
        for field in ("event=", "geometry=", "stats=(mean=", "action=", "outcome="):
            self.assertIn(field, result.stdout)


# --- static tests ------------------------------------------------------------
class StaticGuardTest(unittest.TestCase):
    def test_script_compiles(self):
        source = GUARD.read_text(encoding="utf-8")
        compile(source, str(GUARD), "exec")

    def test_script_is_stdlib_only(self):
        tree = ast.parse(GUARD.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        forbidden = {"PIL", "numpy", "scipy", "cv2", "pytesseract", "pyzbar"}
        self.assertEqual(modules & forbidden, set(),
                         f"deployed guard must stay stdlib-only, found {modules & forbidden}")

    def test_units_exist(self):
        self.assertTrue(SERVICE.is_file())
        self.assertTrue(TIMER.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)