#!/usr/bin/env python3
"""Regression tests for deploy/host/wxpad-export-toggle.

The toggle flips the redmi-wxpad *monitoring* layer (qinglong self-check,
host keepalive probe, phone tunnel guard) while never touching the export
capability itself (wx_daemon and friends). What we assert here is the issued
command set — the state machine — not the Android/systemd side-effects: every
external command (`systemctl`, `adb`, `python3`, `logger`) is replaced by a
stub in a throwaway PATH, so the sequencing is deterministic.

Run stand-alone:  python3 tests/test_export_toggle.py
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOGGLE = ROOT / "deploy" / "host" / "wxpad-export-toggle"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


class ToggleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.adb_log = self.dir / "adb.log"
        self.sysctl_log = self.dir / "sysctl.log"
        self.ql_log = self.dir / "ql.log"
        self._stubs()

    def tearDown(self):
        self._tmp.cleanup()

    def _stubs(self):
        # adb: log argv; answer the state queries the script makes.
        _write_exe(self.bin / "adb", f"""#!/bin/sh
echo "$@" >> "{self.adb_log}"
cmd="$*"
case "$cmd" in
  *"test -e"*)
    case "$cmd" in
      *.disabled*) [ "$STUB_SFA_STATE" = disabled ] && echo y ;;
      *)           [ "$STUB_SFA_STATE" = enabled ] && echo y ;;
    esac ;;
  *"sfa_watchdog.pid"*) [ -n "$STUB_SFA_PID" ] && echo "$STUB_SFA_PID" ;;
  *"pidof wx_daemon"*)  echo 1234 ;;
esac
exit 0
""")
        _write_exe(self.bin / "systemctl", f"""#!/bin/sh
echo "$@" >> "{self.sysctl_log}"
case "$1" in
  is-enabled) echo enabled ;;
  is-active)  echo active ;;
esac
exit 0
""")
        # python3 stub: ignore the heredoc, log the mode arg, emit a fake line.
        _write_exe(self.bin / "python3", f"""#!/bin/sh
echo "$@" >> "{self.ql_log}"
echo "qinglong : id=8 name=wx_export_check isDisabled=0"
exit 0
""")
        _write_exe(self.bin / "logger", "#!/bin/sh\nexit 0\n")

    def run_toggle(self, mode: str, *, sfa_state: str, sfa_pid: str = ""):
        self.assertTrue(TOGGLE.exists(), f"missing script under test: {TOGGLE}")
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "STUB_SFA_STATE": sfa_state,
            "STUB_SFA_PID": sfa_pid,
        }
        return subprocess.run(
            ["bash", str(TOGGLE), mode], env=env, capture_output=True, text=True, timeout=120
        )

    def _read(self, p: Path) -> str:
        return p.read_text() if p.exists() else ""

    def test_down_stops_all_three(self):
        r = self.run_toggle("down", sfa_state="enabled", sfa_pid="9999")
        self.assertEqual(r.returncode, 0, r.stderr)
        adb, sysctl, ql = self._read(self.adb_log), self._read(self.sysctl_log), self._read(self.ql_log)
        self.assertIn("disable --now redmi-tailnet-keepalive.timer", sysctl)
        self.assertIn("mv /data/adb/service.d/sfa_watchdog.sh /data/adb/service.d/sfa_watchdog.sh.disabled", adb)
        self.assertIn("kill 9999", adb)
        self.assertIn("-M", adb)  # kill must go through the global-namespace su
        self.assertIn(" disable", ql)

    def test_up_starts_all_three(self):
        r = self.run_toggle("up", sfa_state="disabled", sfa_pid="")
        self.assertEqual(r.returncode, 0, r.stderr)
        adb, sysctl, ql = self._read(self.adb_log), self._read(self.sysctl_log), self._read(self.ql_log)
        self.assertIn("enable --now redmi-tailnet-keepalive.timer", sysctl)
        self.assertIn("mv /data/adb/service.d/sfa_watchdog.sh.disabled /data/adb/service.d/sfa_watchdog.sh", adb)
        self.assertIn("setsid nohup sh /data/adb/service.d/sfa_watchdog.sh", adb)
        self.assertIn(" enable", ql)

    def test_down_is_idempotent_when_already_down(self):
        r = self.run_toggle("down", sfa_state="disabled", sfa_pid="")
        self.assertEqual(r.returncode, 0, r.stderr)
        adb = self._read(self.adb_log)
        self.assertNotIn("mv ", adb)          # nothing to rename
        self.assertNotIn("kill ", adb)        # nothing to kill

    def test_status_is_read_only(self):
        r = self.run_toggle("status", sfa_state="enabled", sfa_pid="9999")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("host     :", r.stdout)
        self.assertIn("phone    :", r.stdout)
        self.assertIn("qinglong :", r.stdout)
        adb = self._read(self.adb_log)
        self.assertNotIn("mv ", adb)
        self.assertNotIn("kill ", adb)
        self.assertNotIn("--now", self._read(self.sysctl_log))

    def test_unknown_mode_usage(self):
        r = self.run_toggle("bogus", sfa_state="enabled")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
