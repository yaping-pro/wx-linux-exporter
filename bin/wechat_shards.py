from __future__ import annotations

import re
from pathlib import Path

SHARD_RE = re.compile(r"^(?:message|biz_message)_\d+\.db$")


def message_db_paths(decrypted_dir: Path) -> list[Path]:
    """返回消息分片库（message_N.db / biz_message_N.db），排除 fts/resource/media 等旁路库。"""
    d = Path(decrypted_dir) / "message"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and SHARD_RE.match(p.name))
