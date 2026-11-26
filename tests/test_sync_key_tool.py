#!/usr/bin/env python3
"""Regression tests for the key-tool contract in ``bin/wechat-sync.sh``.

Incident coverage (2026-11-26, linux1): the sync script invoked its resolved
KEY_TOOL as ``<tool> extract --db-dir <dir> --output <file>``, but the
repo-shipped ``bin/extract_keys.py`` is a macOS frida tool whose CLI is
``--targets/--db-base``. Every 5-minute run therefore died in argparse:

    extract_keys.py: error: unrecognized arguments: extract --db-dir ... --output ...

and the failure was swallowed by ``|| true``, so the pipeline silently kept
reusing a stale ``all_keys.json``. The script must now (a) only select a tool
that advertises an ``extract`` subcommand and (b) log a warning instead of
failing invisibly.

The harness stubs every external command (``pgrep``, ``sudo``, ``chmod``,
``find``, ``python3``) through a throwaway PATH, so no WeChat / systemd / X
environment is required.
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "bin" / "wechat-sync.sh"

# Help text shapes: the wcdb family lists a subcommand line, the mac extractor does not.
WCDB_TOOL = r"""
import json, sys, argparse, pathlib
p = argparse.ArgumentParser(prog="wcdb_key_tool.py")
sub = p.add_subparsers(dest="command")
e = sub.add_parser("extract", help="提取数据库密钥")
e.add_argument("--db-dir")
e.add_argument("--output")
e.add_argument("--timeout", type=int, default=None)
args = p.parse_args()
if args.command == "extract":
    pathlib.Path(sys.argv[0] + ".calls").open("a").write(
        "extract --db-dir %s --output %s\n" % (args.db_dir, args.output))
    pathlib.Path(args.output).write_text("{}")
    print("[INFO] keys extracted (stub)")
"""

MAC_TOOL = """
import argparse
p = argparse.ArgumentParser(prog="extract_keys.py")
p.add_argument("--mode", choices=["spawn", "attach"], default="spawn")
p.add_argument("--targets", default="sns,favorite")
p.add_argument("--db-base")
p.parse_args()
print("[INFO] mac frida extractor (stub)")
"""


def write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())
    path.chmod(0o755)


class SyncKeyToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.bin = self.home / "stub-bin"
        self.bin.mkdir()
        self.vault = self.home / ".local" / "share" / "wechat-vault"
        self.keys_file = self.vault / "keys" / "all_keys.json"
        self.keys_file.parent.mkdir(parents=True)
        self.keys_file.write_text('{"message/message_0.db": "stale"}')
        self.db_dir = self.home / "Documents" / "xwechat_files" / "wxid_stub" / "db_storage"
        (self.db_dir / "message").mkdir(parents=True)
        (self.db_dir / "message" / "message_0.db").write_bytes(b"x")

        write_exe(self.bin / "pgrep", "#!/bin/sh\nexit 0\n")
        write_exe(self.bin / "sudo", '#!/bin/sh\nexec "$@"\n')
        write_exe(self.bin / "python3", f'#!/bin/sh\necho "$@" >> "{self.home}/python3.calls"\nexit 0\n')

    def tearDown(self):
        self._tmp.cleanup()

    def run_sync(self, key_tool: Path):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "PYTHON_BIN": sys.executable,
            "KEY_TOOL": str(key_tool),
            "VAULT_ROOT": str(self.vault),
        }
        return subprocess.run(
            ["bash", str(SYNC)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def log(self) -> str:
        path = self.vault / "sync.log"
        return path.read_text() if path.exists() else ""

    def test_mac_extractor_is_rejected_and_logged(self):
        tool = self.home / "extract_keys.py"
        write_exe(tool, MAC_TOOL)

        result = self.run_sync(tool)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.log()
        self.assertIn("不满足", log, log)
        self.assertIn(str(tool), log, log)
        self.assertIn("未找到满足", log, log)
        self.assertIn("沿用已有", log, log)
        # The incompatible tool must never be called with the extract contract.
        self.assertFalse((Path(str(tool) + ".calls")).exists())
        self.assertNotIn("extract --db-dir", result.stdout)

    def test_contract_compatible_tool_is_invoked(self):
        tool = self.home / "wcdb_key_tool.py"
        write_exe(tool, WCDB_TOOL)

        result = self.run_sync(tool)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = Path(str(tool) + ".calls").read_text().strip()
        self.assertEqual(
            calls, f"extract --db-dir {self.db_dir} --output {self.keys_file}"
        )
        self.assertNotIn("不满足", self.log())
        # Publish still runs after a successful key refresh.
        self.assertIn("wechat_publish.py", (self.home / "python3.calls").read_text())


if __name__ == "__main__":
    unittest.main()