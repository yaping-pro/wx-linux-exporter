#!/usr/bin/env python3
"""systemd 单元静态守卫：定时器必须在重启后仍然有自己的下一次触发。

背景（2026-11-08 生产事故）：主机上的 `wechat-decrypt.timer` 被追加了一个 drop-in
`interval.conf`，内容为：

    [Timer]
    OnUnitActiveSec=
    OnUnitActiveSec=5min

`OnUnitActiveSec=` 这类**空赋值是列表重置**：systemd 会清空该 timer 的**全部**
触发项（连同 `OnCalendar` 与 `OnBootSec`）。于是重启后 `next_elapse=0 / infinity`，
timer 永久不再触发，vault 索引静默冻结 23 小时。本测试把这两条约束固化下来。

只用标准库（`unittest`）：CI 里逐个 `python3 tests/test_*.py` 直接执行，
不保证安装 pytest。pytest 也可直接收集本文件。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

# 这些键是列表型触发项：写成空值会重置整个计时器列表。
RESET_KEYS = (
    "OnActiveSec",
    "OnBootSec",
    "OnStartupSec",
    "OnUnitActiveSec",
    "OnUnitInactiveSec",
    "OnCalendar",
)


def _units(suffix: str) -> list[Path]:
    return sorted(p for p in SYSTEMD_DIR.glob(f"*{suffix}") if p.is_file())


def _assignments(text: str) -> list[tuple[str, str]]:
    """返回 (key, value) 列表，忽略注释与 section 头。"""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        out.append((key.strip(), value.strip()))
    return out


class SystemdTimerGuardTest(unittest.TestCase):
    def test_discover_timers(self) -> None:
        self.assertTrue(_units(".timer"), f"{SYSTEMD_DIR} 下没有任何 *.timer 单元")

    def test_timer_never_resets_its_trigger_list(self) -> None:
        """空赋值 `OnXxxSec=` / `OnCalendar=` 会清空全部触发项，禁止出现在仓库单元里。"""
        for unit in _units(".timer"):
            with self.subTest(unit=unit.name):
                offenders = [
                    f"{key}="
                    for key, value in _assignments(unit.read_text(encoding="utf-8"))
                    if key in RESET_KEYS and value == ""
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{unit.name} 含有空值触发项 {offenders}；systemd 会因此清空整个计时器列表（含 OnCalendar/OnBootSec），"
                    f"重启后 timer 将再也不触发。",
                )

    def test_timer_is_reboot_proof(self) -> None:
        """每个 timer 至少需要一个不依赖上一次激活的触发项，否则重启后可能永不触发。"""
        for unit in _units(".timer"):
            with self.subTest(unit=unit.name):
                keys = {key for key, _ in _assignments(unit.read_text(encoding="utf-8"))}
                self.assertTrue(
                    keys & {"OnCalendar", "OnBootSec", "OnStartupSec"},
                    f"{unit.name} 只有 OnUnitActiveSec/OnUnitInactiveSec 这类相对触发项，重启后没有激活基准"
                    f"（next_elapse=infinity），会永久静默。请改用 OnCalendar（推荐）或补一个 OnBootSec。",
                )

    def test_timer_points_at_a_service(self) -> None:
        """timer 必须能解析出配套的 service：显式 Unit= 或同名 .service。"""
        for unit in _units(".timer"):
            with self.subTest(unit=unit.name):
                text = unit.read_text(encoding="utf-8")
                explicit = re.search(r"^Unit\s*=\s*(\S+)", text, re.MULTILINE)
                target = explicit.group(1) if explicit else unit.with_suffix(".service").name
                self.assertTrue(
                    (SYSTEMD_DIR / target).is_file(),
                    f"{unit.name} 指向的 {target} 在 deploy/systemd 中不存在",
                )


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)