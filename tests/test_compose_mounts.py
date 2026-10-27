#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_compose_mounts.py - Regression test asserting Docker Compose mount permission matrix.

Ensures worker has read-write access to state/media/exports/staging,
while read-only constraints protect decrypted vaults and configurations.
Guards against silent permission failures swallowed by loop exit codes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


class TestComposeMounts(unittest.TestCase):
    """Test docker-compose.yml volume permissions and command specifications."""

    @classmethod
    def setUpClass(cls):
        if not COMPOSE_FILE.exists():
            raise unittest.SkipTest(f"{COMPOSE_FILE} not found")
        cls.raw_text = COMPOSE_FILE.read_text(encoding="utf-8")
        if _HAS_YAML:
            cls.compose_data = yaml.safe_load(cls.raw_text)
        else:
            cls.compose_data = None

    def _get_services(self):
        if self.compose_data:
            return self.compose_data.get("services", {})
        # Simple fallback parsing when PyYAML is unavailable
        services = {}
        curr_svc = None
        for line in self.raw_text.splitlines():
            m_svc = re.match(r"^ {2}([a-zA-Z0-9_-]+):", line)
            if m_svc:
                curr_svc = m_svc.group(1)
                services[curr_svc] = {"volumes": []}
            elif curr_svc and re.match(r"^ {6}- ['\"]?(.*?)['\"]?$", line):
                m_vol = re.match(r"^ {6}- ['\"]?(.*?)['\"]?$", line)
                if m_vol:
                    services[curr_svc]["volumes"].append(m_vol.group(1))
        return services

    def test_worker_volume_permissions(self):
        """Worker must mount writable directories with :rw and input with :ro."""
        services = self._get_services()
        self.assertIn("worker", services, "worker service must exist in compose")
        worker_vols = services["worker"].get("volumes", [])
        self.assertTrue(worker_vols, "worker volumes must not be empty")

        rw_expected = {"/vault/state", "/vault/media", "/vault/exports", "/vault/staging"}
        ro_expected = {"/vault/decrypted", "/vault/config"}

        observed_rw = set()
        observed_ro = set()

        for vol in worker_vols:
            # Split by ':' from the right to safely handle ':' in ${VAR:?msg}
            rparts = vol.rsplit(":", 2)
            self.assertEqual(len(rparts), 3, f"Volume '{vol}' must have host:container:mode structure")
            host_path, container_path, mode = rparts

            # Host side must strictly require explicit variable definition
            self.assertTrue(
                host_path.startswith("${WECHAT_VAULT_HOST_DIR:?"),
                f"Worker host volume '{host_path}' must use ${{WECHAT_VAULT_HOST_DIR:? ...}} to prevent silent root dir creation"
            )

            if mode == "rw":
                observed_rw.add(container_path)
            elif mode == "ro":
                observed_ro.add(container_path)

        self.assertEqual(
            observed_rw, rw_expected,
            f"Worker writable volumes mismatch: got {observed_rw}, expected {rw_expected}"
        )
        self.assertEqual(
            observed_ro, ro_expected,
            f"Worker read-only volumes mismatch: got {observed_ro}, expected {ro_expected}"
        )

    def test_api_volume_permissions(self):
        """API service must mount all directories with :ro and omit exports/staging."""
        services = self._get_services()
        self.assertIn("api", services, "api service must exist in compose")
        api_vols = services["api"].get("volumes", [])
        self.assertTrue(api_vols, "api volumes must not be empty")

        for vol in api_vols:
            rparts = vol.rsplit(":", 2)
            self.assertEqual(len(rparts), 3, f"Volume '{vol}' must have host:container:mode structure")
            host_path, container_path, mode = rparts

            self.assertEqual(
                mode, "ro",
                f"API volume '{vol}' must be strictly read-only (:ro)"
            )
            self.assertNotIn(
                container_path, {"/vault/exports", "/vault/staging"},
                f"API should not mount internal staging/exports: '{container_path}'"
            )
    def test_worker_command_visibility_and_error_handling(self):
        """Worker command must expose exit code and must not swallow errors with || true."""
        services = self._get_services()
        self.assertIn("worker", services)
        worker = services["worker"]
        cmd = worker.get("command", "")
        if isinstance(cmd, list):
            cmd = " ".join(cmd)

        self.assertNotIn(
            "|| true", cmd,
            "Worker command must not swallow errors with '|| true'"
        )
        self.assertIn(
            "exit=", cmd,
            "Worker command must log exit code (e.g. exit=$$?) to keep errors visible"
        )


if __name__ == "__main__":
    unittest.main()
