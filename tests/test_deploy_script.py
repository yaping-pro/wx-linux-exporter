#!/usr/bin/env python3
"""Tests for scripts/deploy-snapshot.sh (snapshot-style atomic deploy).

These tests never touch a real machine. The bash script drives all remote
operations through `ssh`/`rsync`/`systemctl`/`curl`. We intercept those by
prepending a temporary stub directory to PATH, so the script's "remote"
commands actually run locally against a throwaway fake vault. This lets us
observe real side-effects (rsync/mv/systemctl invocations) without mocking
the Python `subprocess` layer (which cannot see the fork/exec done by bash).

Run stand-alone:  python3 tests/test_deploy_script.py
Or via pytest:    python3 -m pytest tests/test_deploy_script.py -v
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy-snapshot.sh"
BIN_DIR = REPO_ROOT / "bin"


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _make_stubs(stub_dir: Path) -> Path:
    """Create fake ssh/rsync/systemctl/curl/python3 that run locally and log.

    Returns the path of the STUB_LOG file the stubs append to.
    """
    log = stub_dir / "stub.log"
    log.write_text("")

    # ssh -> the script invokes: ssh -o ... -o ... <host> <cmd>
    # so the command is the LAST positional arg; run it locally via bash -c.
    # Special-case the remote vault-resolution probe so we emulate a Linux host
    # (path under /home, never /Users), proving $HOME is resolved on the remote.
    # Any resolved /home path is remapped to $FAKE_VAULT so the local stubs can
    # actually create dirs / copy files without touching macOS /home.
    _write_stub(
        stub_dir / "ssh",
        'HOMEP="/home/steven/.local/share/wechat-vault"\n'
        'cmd="${@: -1}"\n'
        'if [[ "$cmd" == *"printf %s"* && "$cmd" == *VAULT_ROOT* ]]; then\n'
        '  echo "${FAKE_VAULT:-$HOMEP}"; exit 0\n'
        "fi\n"
        'cmd="${cmd//$HOMEP/${FAKE_VAULT:-$HOMEP}}"\n'
        'exec bash -c "$cmd"\n',
    )
    # sudo -> pass-through + log (test asserts the -n flag reaches systemctl)
    # Drops the leading `sudo` and optional `-n` so the underlying command runs.
    _write_stub(
        stub_dir / "sudo",
        'LOG="${STUB_LOG:?}"\n'
        'echo "sudo:$*" >> "$LOG"\n'
        'a=("$@")\n'
        '[[ "${a[0]}" == "sudo" ]] && a=("${a[@]:1}")\n'
        '[[ "${a[0]}" == "-n" ]] && a=("${a[@]:1}")\n'
        'exec "${a[@]}"\n',
    )

    _write_stub(
        stub_dir / "rsync",
        'HOMEP="/home/steven/.local/share/wechat-vault"\n'
        'LOG="${STUB_LOG:?}"\n'
        'dest="${@: -1}"\n'
        'src="${@: -2:1}"\n'
        'dest="${dest//$HOMEP/${FAKE_VAULT:-$HOMEP}}"\n'
        'dest="${dest#*:}"\n'
        'echo rsync >> "$LOG"\n'
        'rm -rf "$dest"\n'
        'mkdir -p "$dest"\n'
        'cp -r "$src." "$dest" 2>/dev/null || true\n',
    )

    # systemctl -> log restart; is-active returns "active" unless FAIL_ACTIVE set
    _write_stub(
        stub_dir / "systemctl",
        'LOG="${STUB_LOG:?}"\n'
        'case "$1" in\n'
        '  restart) echo "systemctl:restart" >> "$LOG"; exit 0 ;;\n'
        '  is-active) if [ -n "${FAIL_ACTIVE:-}" ]; then echo "failed"; exit 3; '
        'else echo "active"; exit 0; fi ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
    )

    # curl -> emit "200" (health ok) and log "curl"
    _write_stub(stub_dir / "curl", 'LOG="${STUB_LOG:?}"; echo "200"; echo curl >> "$LOG"; exit 0\n')
    # mv -T is GNU-only (macOS BSD mv lacks it); emulate `-T src dst` as
    # "remove dst then move src -> dst", matching GNU's atomic-overwrite.
    _write_stub(
        stub_dir / "mv",
        'if [ "$1" = "-T" ]; then s="$2"; d="$3"; rm -f "$d"; exec /bin/mv "$s" "$d"; fi\n'
        'exec /bin/mv "$@"\n',
    )

    # readlink -f is missing on macOS; provide a portable shim that resolves
    # symlinks relative to the current directory (good enough for the fake vault).
    _write_stub(
        stub_dir / "readlink",
        'if [ "$1" = "-f" ]; then p="$2"; while [ -L "$p" ]; do d="$(dirname "$p")"; '
        't="$(readlink "$p")"; case "$t" in /*) p="$t";; *) p="$d/$t";; esac; done; '
        'cd "$(dirname "$p")" 2>/dev/null && echo "$(pwd)/$(basename "$p")" || echo "$p"; exit 0; fi\n'
        'exec /usr/bin/readlink "$@"\n',
    )

    # timeout -> just run the command (no real timeout needed for the stub)
    _write_stub(stub_dir / "timeout", 'shift; exec "$@"\n')

    # python3 -> short-circuit py_compile / --help so self-check is instant;
    # otherwise fall through to the real interpreter.
    _write_stub(
        stub_dir / "python3",
        'LOG="${STUB_LOG:?}"\n'
        'if [[ "$*" == *py_compile* || "$*" == *"--help"* ]]; then\n'
        '  echo "python3:$*" >> "$LOG"; exit 0\n'
        "fi\n"
        'exec /usr/bin/python3 "$@"\n',
    )
    return log


class DeploySnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Path(self.tmp.name) / "vault"
        self.fake.mkdir(parents=True, exist_ok=True)
        self.stub_dir = Path(self.tmp.name) / "stubs"
        self.stub_dir.mkdir()
        self.stub_log = _make_stubs(self.stub_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, extra_args, fail_active=False, capture=True):
        env = dict(os.environ)
        # never inherit test-only / operator control vars from the host or a
        # polluted test session env (FAIL_ACTIVE, VAULT_ROOT, FAKE_VAULT).
        for _v in ("FAIL_ACTIVE", "FAKE_VAULT"):
            env.pop(_v, None)
        # VAULT_ROOT is a real operator override; for unit tests we force the
        # "unset -> resolve on remote" path unless the test passed --vault.
        env.pop("VAULT_ROOT", None)
        env["PATH"] = f"{self.stub_dir}:{env.get('PATH', '')}"
        env["STUB_LOG"] = str(self.stub_log)
        # stubs remap the resolved /home/... vault path onto this writable dir
        env["FAKE_VAULT"] = str(self.fake)
        if fail_active:
            env["FAIL_ACTIVE"] = "1"
        argv = ["bash", str(SCRIPT)] + extra_args
        return subprocess.run(
            argv,
            env=env,
            capture_output=capture,
            text=True,
        )

    def _log_lines(self):
        return self.stub_log.read_text().splitlines()

    # 1) bash -n syntax check ------------------------------------------------
    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    # 2) --help advertises the documented flags ------------------------------
    def test_help_lists_flags(self):
        r = self._run(["--help"])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        for flag in ("--rollback", "--dry-run", "--no-restart"):
            self.assertIn(flag, r.stdout, msg=f"--help missing {flag}")

    # 3) dry-run produces NO side effects -----------------------------------
    def test_dry_run_no_side_effects(self):
        r = self._run(
            ["--dry-run", "--remote", "linux",
             "--vault", str(self.fake), "--source", str(BIN_DIR)]
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # plan steps are printed
        self.assertIn("[STEP] (dry-run) would:", r.stdout)
        self.assertIn("rsync", r.stdout)
        self.assertIn("atomic switch", r.stdout)
        # but the real binaries were never invoked
        lines = self._log_lines()
        self.assertNotIn("rsync", lines, "dry-run must not run rsync")
        self.assertFalse(
            any("restart" in l for l in lines), "dry-run must not restart"
        )
        self.assertFalse(
            any("mv -T" in l or "ln -sfn" in l for l in lines),
            "dry-run must not switch symlink",
        )
        # no symlink should have been created in the fake vault
        self.assertFalse((self.fake / "bin").exists())

    # 4) normal path: correct call ordering ---------------------------------
    def test_normal_deploy_ordering(self):
        r = self._run(
            ["--remote", "linux", "--vault", str(self.fake), "--source", str(BIN_DIR)]
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)

        out = r.stdout
        i_rsync = out.find("rsync")
        i_py = out.find("self-check on remote")
        i_switch = out.find("atomic switch")
        i_restart = out.find("systemctl restart")
        i_health = out.find("health assert")
        for name, idx in (
            ("rsync", i_rsync),
            ("py", i_py),
            ("switch", i_switch),
            ("restart", i_restart),
            ("health", i_health),
        ):
            self.assertGreaterEqual(idx, 0, f"missing step marker: {name}")
        self.assertTrue(
            i_rsync < i_py < i_switch < i_restart < i_health,
            "step order wrong:\n" + out,
        )

        # log order: rsync -> py_compile -> systemctl:restart -> (health) curl
        # The very first "curl" comes from the baseline health probe (step 1);
        # the deploy's own commands must follow that ordering. We verify the
        # deploy commands appear in order by anchoring on rsync (first deploy
        # action) and requiring everything after it is sorted.
        lines = self._log_lines()
        self.assertIn("rsync", lines)
        self.assertTrue(any("py_compile" in l for l in lines))
        self.assertIn("systemctl:restart", lines)
        # final health curl must exist and come last (after the restart)
        i_re = lines.index("systemctl:restart")
        last_curl = max(i for i, l in enumerate(lines) if l == "curl")
        self.assertGreater(last_curl, i_re, f"log order: {lines}")
        i_rs = lines.index("rsync")
        i_pr = next(i for i, l in enumerate(lines) if "py_compile" in l)
        self.assertTrue(i_rs < i_pr < i_re, f"log order: {lines}")

        # the symlink really switched to a new .deploy/<TS> snapshot
        target = os.readlink(self.fake / "bin")
        self.assertTrue(target.startswith(".deploy/"), f"unexpected target: {target}")
        self.assertTrue((self.fake / target).is_dir(), "snapshot dir missing")

    # 5) failure after switch -> auto rollback, non-zero exit ----------------
    def test_failure_triggers_rollback(self):
        # pre-seed an OLD snapshot and a bin -> OLD symlink
        old = self.fake / ".deploy" / "OLD20200101T000000Z"
        old.mkdir(parents=True)
        (old / "wechat_export_api.py").write_text("# old\n")
        os.symlink(".deploy/OLD20200101T000000Z", self.fake / "bin")
        old_abs = os.path.realpath(self.fake / "bin")

        r = self._run(
            ["--remote", "linux", "--vault", str(self.fake), "--source", str(BIN_DIR)],
            fail_active=True,
        )
        self.assertNotEqual(r.returncode, 0, "should fail when service not active")
        self.assertIn("[ROLLBACK]", r.stdout, "rollback banner missing")

        # bin must have been reverted to the OLD snapshot
        self.assertEqual(os.path.realpath(self.fake / "bin"), old_abs)

    # 6) --rollback does switch+restart+health, NOT rsync --------------------
    def test_rollback_only_switches(self):
        old = self.fake / ".deploy" / "OLD20200101T000000Z"
        old.mkdir(parents=True)
        (old / "wechat_export_api.py").write_text("# old\n")
        os.symlink(".deploy/OLD20200101T000000Z", self.fake / "bin")
        old_abs = os.path.realpath(self.fake / "bin")

        r = self._run(
            ["--rollback", "OLD20200101T000000Z",
             "--remote", "linux", "--vault", str(self.fake)]
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("rollback", r.stdout.lower())

        # no rsync happened
        self.assertNotIn("rsync", self._log_lines())
        # but a restart + health curl did
        self.assertIn("systemctl:restart", self._log_lines())
        self.assertIn("curl", self._log_lines())
        # bin still points at the OLD snapshot
        self.assertEqual(os.path.realpath(self.fake / "bin"), old_abs)

    # 7) VAULT unset -> resolved on remote, never leaks local /Users/ ----------
    def test_remote_vault_resolution_no_local_home(self):
        # No --vault: the script must ask the remote for the path. The ssh stub
        # resolves it on the "remote" (a Linux-style /home path), and the local
        # stubs remap that onto the writable fake dir. Critically, the script
        # must NOT substitute the operator's local macOS $HOME (/Users/...).
        r = self._run(["--remote", "linux", "--source", str(BIN_DIR)])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # the script must NOT substitute the operator's local macOS $HOME
        # (/Users/...) for the vault path. The rsync *source* (/Users/.../bin) is
        # legitimately printed in the plan line; only the VAULT path matters.
        # The deploy.log baseline records the resolved bin path under the vault:
        # assert it lands in the remapped fake vault (no /Users/ leak there).
        deploy_log = (self.fake / ".deploy" / "deploy.log").read_text()
        self.assertIn("bin=", deploy_log)
        self.assertNotIn("/Users/", deploy_log)
        # the healthy baseline [OK] line must report a non-empty vault bin path
        self.assertIn("baseline: bin=", r.stdout)
        self.assertNotIn("baseline: bin=/Users/", r.stdout)

    # 8) ssh/rsh failure -> [FAIL] + non-zero, never a misleading [OK] ------
    def test_remote_failure_aborts_not_ok(self):
        # Make the ssh stub fail for every command (exit 1, no output).
        (self.stub_dir / "ssh").write_text(
            "#!/usr/bin/env bash\nLOG=\"${STUB_LOG:?}\"; "
            'echo "ssh:fail" >> "$LOG"; exit 1\n'
        )
        (self.stub_dir / "ssh").chmod(0o755)
        r = self._run(
            ["--remote", "linux", "--vault", str(self.fake), "--source", str(BIN_DIR)]
        )
        self.assertNotEqual(r.returncode, 0, "must abort when remote is unreachable")
        self.assertIn("[FAIL]", r.stdout, "must log a failure on remote error")
        self.assertNotIn("baseline: active=", r.stdout,
                         "must NOT print a misleading [OK] baseline when remote failed")

    # 9) systemctl invocations carry `sudo -n` -------------------------------
    def test_systemctl_uses_sudo_n(self):
        r = self._run(
            ["--remote", "linux", "--vault", str(self.fake), "--source", str(BIN_DIR)]
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # the `sudo` stub records every invocation; both restart and is-active
        # must have been run through `sudo -n systemctl ...`
        sudo_lines = [l for l in self._log_lines() if l.startswith("sudo:")]
        self.assertTrue(any("-n systemctl restart" in l for l in sudo_lines),
                         msg=f"sudo -n restart missing: {sudo_lines}")
        self.assertTrue(any("-n systemctl is-active" in l for l in sudo_lines),
                         msg=f"sudo -n is-active missing: {sudo_lines}")
        # and the human-readable plan/step line also names sudo -n
        self.assertIn("sudo -n systemctl restart", r.stdout)


if __name__ == "__main__":
    unittest.main()
