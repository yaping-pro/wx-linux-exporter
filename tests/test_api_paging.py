"""Tests for /api/wechat/export pagination, cursor continuation, and streaming ndjson."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "bin"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


from bin import wechat_export_api

TOKEN = "test_paging_token_secret"


def _build_test_databases(decrypted_dir: Path, chats: list[tuple[str, str, str, bool]], messages: list[tuple]):
    decrypted_dir.mkdir(parents=True, exist_ok=True)
    contact_dir = decrypted_dir / "contact"
    contact_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(contact_dir / "contact.db")
    con.execute(
        "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT, local_type INT, chat_room_type INT)"
    )
    for u, nick, rem, is_grp in chats:
        con.execute(
            "INSERT INTO contact VALUES (?, ?, ?, '', 1, ?)",
            (u, rem, nick, 1 if is_grp else 0),
        )
    con.commit()
    con.close()

    msg_dir = decrypted_dir / "message"
    msg_dir.mkdir(parents=True, exist_ok=True)
    msg_db = msg_dir / "message_0.db"
    msg_con = sqlite3.connect(str(msg_db))
    msg_con.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    for idx, (u, _nick, _rem, _is_grp) in enumerate(chats, start=1):
        msg_con.execute("INSERT INTO Name2Id VALUES (?, ?)", (idx, u))

    tables_created = set()
    for row in messages:
        chat_id = row[0]
        table_md5 = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        tname = f"Msg_{table_md5}"
        if tname not in tables_created:
            msg_con.execute(
                f"CREATE TABLE [{tname}] (local_id INTEGER, server_id INTEGER, local_type INTEGER, "
                f"real_sender_id INTEGER, create_time INTEGER, message_content TEXT, compress_content BLOB)"
            )
            tables_created.add(tname)
        msg_con.execute(
            f"INSERT INTO [{tname}] VALUES (?, 0, ?, 0, ?, ?, NULL)",
            (row[1], row[3], row[2], row[4]),
        )
    msg_con.commit()
    msg_con.close()


class ApiPagingStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp_dir.name)
        cls.vault = cls.root / "vault"
        cls.vault.mkdir()
        cls.decrypted = cls.vault / "decrypted" / "current"

        # Construct 6 messages across 2 chats:
        # Alice: 4 messages at times 10, 20, 30, 40
        # Bob: 2 messages at times 25, 50
        # Global ascending order by (ts, chat_id, local_id):
        # 1. (1759000010, wxid_alice, 1)
        # 2. (1759000020, wxid_alice, 2)
        # 3. (1759000025, wxid_bob,   101)
        # 4. (1759000030, wxid_alice, 3)
        # 5. (1759000040, wxid_alice, 4)
        # 6. (1759000050, wxid_bob,   102)
        _build_test_databases(
            cls.decrypted,
            chats=[
                ("wxid_alice", "爱丽丝", "Alice", False),
                ("wxid_bob", "鲍勃", "Bob", False),
            ],
            messages=[
                ("wxid_alice", 1, 1759000010, 1, "Alice msg 1"),
                ("wxid_alice", 2, 1759000020, 1, "Alice msg 2"),
                ("wxid_alice", 3, 1759000030, 1, "Alice msg 3"),
                ("wxid_alice", 4, 1759000040, 1, "Alice msg 4"),
                ("wxid_bob", 101, 1759000025, 1, "Bob msg 1"),
                ("wxid_bob", 102, 1759000050, 1, "Bob msg 2"),
            ],
        )

        cls.server = wechat_export_api.create_server(
            host="127.0.0.1",
            port=0,
            vault_dir=cls.decrypted,
            token=TOKEN,
            media_vault_root=str(cls.vault),
        )
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp_dir.cleanup()

    def _req(self, path: str, token: str | None = TOKEN, headers: dict | None = None) -> tuple[int, bytes, dict]:
        quoted_path = urllib.parse.quote(path, safe="/:?=&%")
        url = f"http://127.0.0.1:{self.port}{quoted_path}"
        req_headers = dict(headers or {})
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_headers = dict(resp.headers)
                return resp.status, resp.read(), resp_headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_invalid_limit_and_cursor(self):
        """非法 limit（负数、非数字）与非法 cursor 必须返回 400 结构化错误。"""
        # 1. 负数 limit -> 400
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&limit=-5")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))["error"]
        self.assertEqual(err["code"], "invalid_param")

        # 2. 非数字 limit -> 400
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&limit=abc")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))["error"]
        self.assertEqual(err["code"], "invalid_param")

        # 3. 非法 base64 cursor -> 400
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&cursor=!!!not_b64!!!")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))["error"]
        self.assertEqual(err["code"], "invalid_cursor")

        # 4. JSON 结构缺字段的 cursor -> 400
        bad_json_cursor = base64.urlsafe_b64encode(b'{"ts":123}').decode("ascii").rstrip("=")
        status, body, _ = self._req(f"/api/wechat/export?since=1759000000&to=1759000100&cursor={bad_json_cursor}")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))["error"]
        self.assertEqual(err["code"], "invalid_cursor")

        # 5. 非法 format -> 400
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&format=xml")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))["error"]
        self.assertEqual(err["code"], "invalid_param")

    def test_pagination_and_two_page_reconstruction(self):
        """测试 limit 截断 + next_cursor，两页拼接得到的内容与不分页一致，且顺序完全一致。"""
        # 全量不分页查询 (limit=0 或缺省)
        status, body_full, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100")
        self.assertEqual(status, 200)
        full_data = json.loads(body_full.decode("utf-8"))
        self.assertIsNone(full_data["next_cursor"])
        self.assertFalse(full_data["payload_stats"]["truncated"])
        self.assertEqual(full_data["payload_stats"]["messages"], 6)

        # 提取全部消息 [(chat_name, m["text"])]
        full_messages = []
        for c in full_data["chats"]:
            for m in c["messages"]:
                full_messages.append((c["name"], m["text"]))

        # 第 1 页：limit=4
        status, body_p1, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&limit=4")
        self.assertEqual(status, 200)
        p1_data = json.loads(body_p1.decode("utf-8"))
        self.assertTrue(p1_data["payload_stats"]["truncated"])
        self.assertEqual(p1_data["payload_stats"]["messages"], 4)
        c1 = p1_data["next_cursor"]
        self.assertIsNotNone(c1)

        # 验证第 1 页包含前 4 条全局消息：
        # Alice 1, Alice 2, Bob 1 (ts 25), Alice 3 (ts 30)
        # 水位线：因为有 next_cursor，watermark 不能提前推进，必须保持 since 对应的时间
        self.assertEqual(p1_data["watermark"], wechat_export_api.format_rfc3339(1759000000))

        # 第 2 页：cursor=c1, limit=4
        status, body_p2, _ = self._req(f"/api/wechat/export?since=1759000000&to=1759000100&limit=4&cursor={c1}")
        self.assertEqual(status, 200)
        p2_data = json.loads(body_p2.decode("utf-8"))
        self.assertFalse(p2_data["payload_stats"]["truncated"])
        self.assertEqual(p2_data["payload_stats"]["messages"], 2)
        self.assertIsNone(p2_data["next_cursor"])

        # 第 2 页没有 next_cursor，水位线正常推进至最大消息时间 (1759000050)
        self.assertEqual(p2_data["watermark"], wechat_export_api.format_rfc3339(1759000050))
        self.assertEqual(full_data["watermark"], p2_data["watermark"])

        # 拼接两页结果：将每个会话的消息合并
        paged_messages_by_chat: dict[str, list[dict]] = {}
        for page in (p1_data, p2_data):
            for c in page["chats"]:
                cname = c["name"]
                paged_messages_by_chat.setdefault(cname, []).extend(c["messages"])

        # 验证各会话内的消息完全等于全量查询
        reconstructed_full_chats = []
        for cname in sorted(paged_messages_by_chat.keys()):
            reconstructed_full_chats.append({
                "name": cname,
                "messages": [(cname, m["text"]) for m in paged_messages_by_chat[cname]],
            })

        reconstructed_messages = []
        for c in full_data["chats"]:
            cname = c["name"]
            for m in paged_messages_by_chat[cname]:
                reconstructed_messages.append((cname, m["text"]))

        self.assertEqual(reconstructed_messages, full_messages)

    def test_watermark_preservation_under_paging(self):
        """分页模式下：有 next_cursor 时 watermark 不得提前推进。"""
        # 查询 limit=2，总共有 6 条消息
        status, body, _ = self._req("/api/wechat/export?since=1759000000&to=1759000100&limit=2")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIsNotNone(data["next_cursor"])
        self.assertTrue(data["payload_stats"]["truncated"])
        # watermark 必须等于 since，而不是这 2 条消息的最大时间
        expected_since_wm = wechat_export_api.format_rfc3339(1759000000)
        self.assertEqual(data["watermark"], expected_since_wm)

    def test_ndjson_streaming_format_and_row_types(self):
        """测试 format=ndjson 的 Content-Type、分块传输以及行类型顺序规范。"""
        status, body, headers = self._req(
            "/api/wechat/export?since=1759000000&to=1759000100&limit=3&format=ndjson"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/x-ndjson")

        lines = [line.strip() for line in body.decode("utf-8").split("\n") if line.strip()]
        self.assertGreaterEqual(len(lines), 3)

        rows = [json.loads(line) for line in lines]
        # 1. 第一行必须是 manifest
        self.assertEqual(rows[0]["type"], "manifest")
        self.assertIn("exported_at", rows[0])
        self.assertIn("range", rows[0])
        self.assertIn("watermark", rows[0])
        self.assertIn("manifest", rows[0])
        self.assertNotIn("messages", rows[0])

        # 2. 中间行必须是 chat 或 message
        message_count = 0
        for r in rows[1:-1]:
            self.assertIn(r["type"], ("chat", "message"))
            if r["type"] == "chat":
                self.assertIn("chat_id", r)
                self.assertIn("name", r)
                self.assertIn("kind", r)
            elif r["type"] == "message":
                message_count += 1
                self.assertIn("chat_id", r)
                self.assertIn("ts", r)
                self.assertIn("sender", r)
                self.assertIn("kind", r)
                self.assertIn("text", r)
                self.assertIn("note", r)

        self.assertEqual(message_count, 3)

        # 3. 最后一行必须是 end
        self.assertEqual(rows[-1]["type"], "end")
        self.assertIn("next_cursor", rows[-1])
        self.assertIsNotNone(rows[-1]["next_cursor"])
        self.assertIn("payload_stats", rows[-1])
        self.assertEqual(rows[-1]["payload_stats"]["messages"], 3)
        self.assertTrue(rows[-1]["payload_stats"]["truncated"])


if __name__ == "__main__":
    unittest.main()
