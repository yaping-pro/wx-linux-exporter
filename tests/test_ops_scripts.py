#!/usr/bin/env python3
"""Regression tests for the two ops scripts that keep the redmi-wxpad export
endpoint reachable.

What is under test is the *state machine*, not the Android/Linux environment:
every external command the scripts touch (`am`, `pidof`, `getprop`, `sleep`,
`tailscale`, `curl`, `adb`, `runuser`, `timeout`) is replaced by a stub in a
throwaway PATH, so the sequencing rules can be asserted deterministically.

Incident coverage (see docs/OPS.md):
  * 2026-11-04 — phone rebooted while FBE-locked; SFA crash-looped, the device
    guard's 600s backoff then ignored the unlock for another 6 minutes.
    -> unlock gate + backoff cap
  * 2026-10-23 — tunnel process alive but tailnet path stale.
    -> process-alive-but-tun-down must still trigger a tunnel start
  * host keepalive "nudge" was `adb connect` (no-op on a connected device).
    -> nudge must issue an idempotent `am start-foreground-service`
"""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "deploy" / "device" / "sfa_watchdog.sh"
KEEPALIVE = ROOT / "deploy" / "host" / "redmi-tailnet-keepalive"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip())
    path.chmod(0o755)


class OpsHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.tun = self.dir / "tun0"
        self.am_calls = self.dir / "am_calls"
        self.sleeps = self.dir / "sleeps"
        self.adb_calls = self.dir / "adb_calls"
        self.push_log = self.dir / "push.log"

    def tearDown(self):
        self._tmp.cleanup()

    def stub(self, name: str, body: str) -> None:
        _write_exe(self.bin / name, body)

    def run_script(self, script: Path, **env):
        self.assertTrue(script.exists(), f"missing script under test: {script}")
        environ = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.dir)}
        environ.update({k: str(v) for k, v in env.items()})
        # run under bash so the host script's `set -o pipefail` works on every
        # platform (CI /bin/sh is dash); the device script is a POSIX subset.
        return subprocess.run(
            ["bash", str(script)],
            env=environ,
            capture_output=True,
            text=True,
            timeout=120,
        )

    # ---- device-side stubs -------------------------------------------------
    def watchdog_stubs(self, *, unlock_after: int, tun_up: bool, pid: str,
                       start_creates_tun: bool) -> None:
        counter = self.dir / "getstate_count"

        tun_effect = (
            f"mkdir -p {self.tun} && echo up > {self.tun}/operstate"
            if start_creates_tun
            else "true"
        )
        self.stub("am", f"""
            #!/bin/sh
            case "$1" in
              get-started-user-state)
                echo get-state >> {self.am_calls}
                n=$(cat {counter} 2>/dev/null || echo 0)
                n=$((n + 1))
                echo "$n" > {counter}
                if [ "$n" -ge {unlock_after} ]; then
                  echo RUNNING_UNLOCKED
                else
                  echo RUNNING_LOCKED
                fi
                ;;
              start-foreground-service)
                echo start >> {self.am_calls}
                {tun_effect}
                ;;
            esac
            """)
        _write_exe(self.bin / "getprop", "#!/bin/sh\n[ \"$1\" = \"sys.boot_completed\" ] && echo 1\n")
        _write_exe(self.bin / "pidof", "#!/bin/sh\necho %s\n" % pid if pid else "#!/bin/sh\nexit 1\n")
        _write_exe(self.bin / "sleep", f"#!/bin/sh\necho \"$1\" >> {self.sleeps}\n")
        _write_exe(self.bin / "pgrep", "#!/bin/sh\nexit 1\n")
        if tun_up:
            self.tun.mkdir(parents=True, exist_ok=True)
            (self.tun / "operstate").write_text("up\n")

    # ---- host-side stubs ---------------------------------------------------
    def keepalive_stubs(self, *, path_ok: bool, biz_ok: bool, tun_state: str = "") -> None:
        rc = 0 if path_ok else 1
        _write_exe(self.bin / "tailscale", f"#!/bin/sh\nexit {rc}\n")
        code = "200" if biz_ok else "000"
        _write_exe(self.bin / "curl", f"#!/bin/sh\nprintf '%s' {code}\n")
        tun_state_file = self.dir / "tun_state"
        tun_state_file.write_text(tun_state + "\n" if tun_state else "")
        _write_exe(self.bin / "adb", f"""#!/bin/sh
echo "$@" >> {self.adb_calls}
case "$*" in
  *operstate*) cat {tun_state_file} ;;
esac
""")
        _write_exe(self.bin / "logger", "#!/bin/sh\nexit 0\n")
        _write_exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        _write_exe(self.bin / "timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")
        _write_exe(self.bin / "runuser", f"#!/bin/sh\ncat >> {self.push_log}\n")
        _write_exe(self.data_push_script(), "#!/bin/sh\ncat >> /dev/null\n")

    def data_push_script(self) -> Path:
        p = self.dir / "push-report.sh"
        _write_exe(p, "#!/bin/sh\ncat >> /dev/null\n")
        return p


class SfaWatchdogTest(OpsHarness):
    def _run(self, loops: int, **env):
        defaults = dict(
            SFA_LOG_FILE=self.dir / "watchdog.log",
            SFA_PID_FILE=self.dir / "watchdog.pid",
            SFA_TUN_PATH=self.tun,
            SFA_MAX_LOOPS=loops,
            SFA_START_TIMEOUT="1",
            SFA_START_DELAY="0",
        )
        defaults.update(env)
        return self.run_script(WATCHDOG, **defaults)

    def test_does_not_touch_service_before_user_unlock(self):
        """FBE-locked boot must not start (or crash-loop) the VPN service."""
        self.watchdog_stubs(unlock_after=2, tun_up=False, pid="", start_creates_tun=True)
        res = self._run(loops=1)
        self.assertEqual(res.returncode, 0, res.stderr)

        calls = self.am_calls.read_text().split()
        self.assertEqual(calls[:2], ["get-state", "get-state"], calls)
        self.assertIn("start", calls[2:], calls)
        self.assertEqual(calls.count("start"), 1, calls)

    def test_backoff_never_exceeds_cap(self):
        """A permanently failing start must not sleep anywhere near 600s."""
        self.watchdog_stubs(unlock_after=1, tun_up=False, pid="", start_creates_tun=False)
        res = self._run(loops=8)
        self.assertEqual(res.returncode, 0, res.stderr)

        sleeps = [float(s) for s in self.sleeps.read_text().split() if s]
        self.assertGreaterEqual(len(sleeps), 5, sleeps)
        self.assertLessEqual(max(sleeps), 30, sleeps)

    def test_process_alive_but_tun_down_still_starts_tunnel(self):
        """2026-10-23 class: process survives, tunnel does not."""
        self.watchdog_stubs(unlock_after=1, tun_up=True, pid="4242", start_creates_tun=False)
        (self.tun / "operstate").write_text("down\n")
        res = self._run(loops=1)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreaterEqual(self.am_calls.read_text().split().count("start"), 1)


class KeepaliveTest(OpsHarness):
    def _run(self, fails: int, alerted: int | None = None, **env):
        state = self.dir / "run"
        state.mkdir(exist_ok=True)
        (state / "redmi-tailnet-keepalive.fails").write_text(f"{fails}\n")
        if alerted is not None:
            (state / "redmi-tailnet-keepalive.alerted").write_text(f"{alerted}\n")
        defaults = dict(
            KEEPALIVE_STATE_DIR=state,
            KEEPALIVE_PUSH=self.data_push_script(),
            NUDGE_SETTLE="0",
        )
        defaults.update(env)
        return self.run_script(KEEPALIVE, **defaults), state

    def test_nudge_starts_service_instead_of_adb_connect(self):
        """The 3rd consecutive failure must actuate, not re-connect adb."""
        self.keepalive_stubs(path_ok=False, biz_ok=False, tun_state="down")
        res, state = self._run(fails=2)
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertEqual((state / "redmi-tailnet-keepalive.fails").read_text().strip(), "3")
        calls = self.adb_calls.read_text()
        self.assertIn("shell", calls)
        self.assertIn("start-foreground-service", calls)
        self.assertIn("io.nekohasekai.sfa/.bg.VPNService", calls)
        self.assertNotIn("connect", calls)

    def test_nudge_is_skipped_when_device_tunnel_is_already_up(self):
        """Never race the device watchdog: a live tunnel means nothing to do."""
        self.keepalive_stubs(path_ok=False, biz_ok=False, tun_state="up")
        res, state = self._run(fails=2)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("start-foreground-service", self.adb_calls.read_text())

    def test_recovery_resets_state_and_pushes_once(self):
        self.keepalive_stubs(path_ok=True, biz_ok=True)
        res, state = self._run(fails=5, alerted=5)
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertEqual((state / "redmi-tailnet-keepalive.fails").read_text().strip(), "0")
        self.assertFalse((state / "redmi-tailnet-keepalive.alerted").exists())
        self.assertEqual(len(self.push_log.read_text().strip().splitlines()), 1)
        self.assertIn("恢复", self.push_log.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)