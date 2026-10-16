#!/usr/bin/env python3
"""回归测试：会话过滤（allowlist / blocklist）必须真正生效。

守护一个真实缺陷：`create_server()` 构造 `server_context` 时漏掉了 `blocklist`，
导致任何 blocklist 被静默忽略——被屏蔽的会话会照常出现在导出结果里。
由于 `main()` 同样经由 `create_server`，该缺陷对生产部署同样生效。

测试只依赖标准库，起一个真实的回环 HTTP 服务做端到端断言（不 mock 内部函数），
这样任何"参数在某层被丢掉"的问题都会被抓到。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_export_api  # noqa: E402

TOKEN = "test-token-not-a-real-secret"
SINCE = 1790700000
TO = 1790800000


def _build_vault(root: Path, chats: list[tuple[str, str]]) -> None:
    """chats: [(username, remark)] —— remark 即导出中的展示名。"""
    (root / "contact").mkdir(parents=True)
    (root / "message").mkdir(parents=True)

    con = sqlite3.connect(root / "contact" / "contact.db")
    con.execute(
        "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, "
        "alias TEXT, encrypt_username TEXT, remark TEXT, nick_name TEXT, chat_room_type INTEGER)"
    )
    con.executemany(
        "INSERT INTO contact VALUES (?,?,?,?,?,?,?,?)",
        [(i + 1, u, 1, "", "", r, r, 0) for i, (u, r) in enumerate(chats)],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(root / "message" / "message_0.db")
    con.execute("CREATE TABLE Name2Id(rowid INTEGER PRIMARY KEY, user_name TEXT, is_session INTEGER)")
    con.executemany(
        "INSERT INTO Name2Id VALUES (?,?,?)",
        [(i + 1, u, 1) for i, (u, _r) in enumerate(chats)],
    )
    for u, _r in chats:
        table = "Msg_" + hashlib.md5(u.encode()).hexdigest()
        con.execute(
            f"CREATE TABLE [{table}](local_id INTEGER, server_id INTEGER, local_type INTEGER, "
            f"real_sender_id INTEGER, create_time INTEGER, message_content BLOB, compress_content BLOB)"
        )
        con.execute(
            f"INSERT INTO [{table}] VALUES (1, 1, 1, 1, {SINCE + 100}, ?, NULL)",
            (f"hello from {u}".encode(),),
        )
    con.commit()
    con.close()


class ChatFilteringRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="wx-filter-test-")
        cls.vault = Path(cls.tmp)
        _build_vault(cls.vault, [("wxid_keep", "Keep Me"), ("wxid_blocked", "Blocked One")])
        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=cls.vault,
            token=TOKEN,
            blocklist=["Blocked One"],
        )
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _export(self) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/wechat/export?since={SINCE}&to={TO}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8"))

    def test_blocklist_is_actually_applied(self):
        """守护 create_server 丢掉 blocklist 的回归。"""
        names = [c["name"] for c in self._export()["chats"]]
        self.assertNotIn("Blocked One", names, "blocklist 未生效：被屏蔽会话仍出现在导出中")
        self.assertIn("Keep Me", names, "未被屏蔽的会话不应消失")

    def test_unauthenticated_export_is_rejected(self):
        """回归：鉴权默认 fail-closed，无 token 必须 401。"""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/wechat/export?since={SINCE}&to={TO}"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("无 token 的导出请求本应被拒绝")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


if __name__ == "__main__":
    unittest.main()
