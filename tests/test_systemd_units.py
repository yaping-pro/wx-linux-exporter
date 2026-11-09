"""systemd 单元静态守卫：定时器必须在重启后仍然有自己的下一次触发。

背景（2026-11-08 生产事故）：主机上的 `wechat-decrypt.timer` 被追加了一个 drop-in
`interval.conf`，内容为：

    [Timer]
    OnUnitActiveSec=
    OnUnitActiveSec=5min

`OnUnitActiveSec=` 这类**空赋值是列表重置**：systemd 会清空该 timer 的**全部**
触发项（连同 `OnCalendar` 与 `OnBootSec`）。于是重启后 `next_elapse=0 / infinity`，
timer 永久不再触发，vault 索引静默冻结 23 小时。本测试把这两条约束固化下来。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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


def _unit_id(path: Path) -> str:
    return path.name


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


@pytest.mark.parametrize("unit", _units(".timer"), ids=_unit_id)
def test_timer_never_resets_its_trigger_list(unit: Path) -> None:
    """空赋值 `OnXxxSec=` / `OnCalendar=` 会清空全部触发项，禁止出现在仓库单元里。"""
    offenders = [
        f"{key}="
        for key, value in _assignments(unit.read_text(encoding="utf-8"))
        if key in RESET_KEYS and value == ""
    ]
    assert not offenders, (
        f"{unit.name} 含有空值触发项 {offenders}；systemd 会因此清空整个计时器列表"
        "（含 OnCalendar/OnBootSec），重启后 timer 将再也不触发。"
    )


@pytest.mark.parametrize("unit", _units(".timer"), ids=_unit_id)
def test_timer_is_reboot_proof(unit: Path) -> None:
    """每个 timer 至少需要一个不依赖上一次激活的触发项，否则重启后可能永不触发。"""
    keys = {key for key, _ in _assignments(unit.read_text(encoding="utf-8"))}
    assert keys & {"OnCalendar", "OnBootSec", "OnStartupSec"}, (
        f"{unit.name} 只有 OnUnitActiveSec/OnUnitInactiveSec 这类相对触发项，"
        "重启后没有激活基准（next_elapse=infinity），会永久静默。"
        "请改用 OnCalendar（推荐）或补一个 OnBootSec。"
    )


@pytest.mark.parametrize("unit", _units(".timer"), ids=_unit_id)
def test_timer_points_at_a_service(unit: Path) -> None:
    """timer 必须能解析出配套的 service：显式 Unit= 或同名 .service。"""
    text = unit.read_text(encoding="utf-8")
    explicit = re.search(r"^Unit\s*=\s*(\S+)", text, re.MULTILINE)
    target = explicit.group(1) if explicit else unit.with_suffix(".service").name
    assert (SYSTEMD_DIR / target).is_file(), f"{unit.name} 指向的 {target} 在 deploy/systemd 中不存在"