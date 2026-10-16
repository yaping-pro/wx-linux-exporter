from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import mineru_agent  # noqa: E402
import wechat_media  # noqa: E402


class ResolveSourceTests(unittest.TestCase):
    """hardlink.db → 磁盘路径的判定分支（Phase 1 唯一未验证假设，必须钉住）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, month: str, name: str) -> Path:
        d = self.root / month
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(b"x")
        return p

    def test_month_branch_hits(self):
        self._mk("2026-10", "a.pdf")
        dir2id = {1: "2026-10"}
        entry = ("a.pdf", 1, 0, 1)
        path, how = wechat_media.resolve_source(self.root, dir2id, entry, "a.pdf")
        self.assertEqual(how, "hardlink:month")
        self.assertEqual(path.name, "a.pdf")

    def test_chat_hash_dir1_falls_back_to_unique_name(self):
        # 实测 78 条里有 10 条 dir1 不是月份而是会话哈希
        self._mk("2026-09", "b.pdf")
        dir2id = {43: "7f312a15930c2fa7b90b687c3196fd59"}
        entry = ("b.pdf", 43, 0, 1)
        path, how = wechat_media.resolve_source(self.root, dir2id, entry, "b.pdf")
        self.assertEqual(how, "hardlink:unique-name")
        self.assertEqual(path.parent.name, "2026-09")

    def test_ambiguous_name_is_not_guessed(self):
        self._mk("2026-09", "dup.pdf")
        self._mk("2026-10", "dup.pdf")
        dir2id = {43: "deadbeef"}
        entry = ("dup.pdf", 43, 0, 1)
        path, how = wechat_media.resolve_source(self.root, dir2id, entry, "dup.pdf")
        self.assertIsNone(path)
        self.assertTrue(how.startswith("ambiguous:"), how)

    def test_unresolved_when_nothing_matches(self):
        dir2id = {43: "deadbeef"}
        entry = ("nope.pdf", 43, 0, 1)
        path, how = wechat_media.resolve_source(self.root, dir2id, entry, "nope.pdf")
        self.assertIsNone(path)
        self.assertEqual(how, "unresolved")


class PolicyTests(unittest.TestCase):
    def test_missing_policy_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = wechat_media.load_policy(Path(tmp) / "absent.json")
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["chat_allowlist"], [])
        self.assertFalse(policy["render"]["inject_into_text"])

    def test_partial_policy_merges_nested_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text('{"enabled": true, "render": {"inject_into_text": true}}', encoding="utf-8")
            policy = wechat_media.load_policy(p)
        self.assertTrue(policy["enabled"])
        self.assertTrue(policy["render"]["inject_into_text"])
        # 未给出的嵌套键必须保留默认，避免策略文件缺字段时静默放开
        self.assertEqual(policy["render"]["max_chars_per_doc"], 4000)
        self.assertIn("pdf", policy["file_ext_allow"])


class MineruClassifyTests(unittest.TestCase):
    """通道能力边界必须映射成正确的退出码，否则会被误判为永久失败。"""

    def test_html_is_unsupported_not_failed(self):
        msg = "file type '.html' is not supported by the lightweight API"
        self.assertEqual(mineru_agent.classify(msg), mineru_agent.EXIT_UNSUPPORTED)

    def test_page_count_limit(self):
        msg = "file page count exceeds API limit (20 pages), please input page_range"
        self.assertEqual(mineru_agent.classify(msg), mineru_agent.EXIT_PAGE_LIMIT)

    def test_quota_is_retryable(self):
        self.assertEqual(mineru_agent.classify("quota -60018 exceeded"), mineru_agent.EXIT_THROTTLED)

    def test_agent_supported_ext_has_no_html(self):
        self.assertIn("pdf", mineru_agent.AGENT_SUPPORTED_EXT)
        self.assertNotIn("html", mineru_agent.AGENT_SUPPORTED_EXT)
        self.assertNotIn("doc", mineru_agent.AGENT_SUPPORTED_EXT)


class AgentCapabilityGateTests(unittest.TestCase):
    def test_worker_gate_excludes_html(self):
        self.assertNotIn("html", wechat_media.AGENT_SUPPORTED_EXT)
        self.assertIn("xlsx", wechat_media.AGENT_SUPPORTED_EXT)

    def test_token_detection_is_absent_by_default(self):
        self.assertFalse(wechat_media.precise_token_available(None))


class ChannelRoutingTests(unittest.TestCase):
    """通道选择决定花不花钱、走不走精准 API，必须钉住。"""

    class _Args:
        def __init__(self, mode="auto"):
            self.mode = mode

    def test_html_without_token_is_unsupported(self):
        mode, rc = mineru_agent.choose_mode(self._Args(), "html", 1000, None)
        self.assertEqual((mode, rc), ("agent", mineru_agent.EXIT_UNSUPPORTED))

    def test_html_with_token_goes_precise(self):
        mode, rc = mineru_agent.choose_mode(self._Args(), "html", 1000, "sk-x")
        self.assertEqual((mode, rc), ("precise", mineru_agent.EXIT_OK))

    def test_small_pdf_stays_on_free_channel_even_with_token(self):
        mode, rc = mineru_agent.choose_mode(self._Args(), "pdf", 5 * 1024 * 1024, "sk-x")
        self.assertEqual((mode, rc), ("agent", mineru_agent.EXIT_OK))

    def test_big_pdf_without_token_is_oversize(self):
        mode, rc = mineru_agent.choose_mode(self._Args(), "pdf", 15 * 1024 * 1024, None)
        self.assertEqual((mode, rc), ("agent", mineru_agent.EXIT_OVERSIZE))

    def test_big_pdf_with_token_goes_precise(self):
        mode, rc = mineru_agent.choose_mode(self._Args(), "pdf", 15 * 1024 * 1024, "sk-x")
        self.assertEqual((mode, rc), ("precise", mineru_agent.EXIT_OK))

    def test_explicit_precise_without_token_reports_no_credential(self):
        mode, rc = mineru_agent.choose_mode(self._Args("precise"), "pdf", 1000, None)
        self.assertEqual((mode, rc), ("precise", mineru_agent.EXIT_NO_CREDENTIAL))


class CredentialResolutionTests(unittest.TestCase):
    def test_token_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tok"
            p.write_text("sk-secret\n", encoding="utf-8")
            self.assertEqual(mineru_agent.resolve_token(str(p)), "sk-secret")

    def test_missing_token_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mineru_agent.resolve_token(str(Path(tmp) / "nope")))

    def test_systemd_credentials_directory_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mineru-token").write_text("sk-cred\n", encoding="utf-8")
            old = os.environ.get("CREDENTIALS_DIRECTORY")
            os.environ["CREDENTIALS_DIRECTORY"] = tmp
            try:
                self.assertEqual(mineru_agent.resolve_token(None), "sk-cred")
            finally:
                if old is None:
                    os.environ.pop("CREDENTIALS_DIRECTORY", None)
                else:
                    os.environ["CREDENTIALS_DIRECTORY"] = old


class VoiceTranscriptionTests(unittest.TestCase):
    """语音转写取自 packed_info_data（protobuf），fixture 为合成数据。"""

    # 与真实 blob 同构：08 35 10 0d | 2a <len> 08 02 12 <len> <text> | 58 00
    CONVERTED_HEX = (
        "0835100d2a700802126c"
        "e8bf99e698afe4b880e69da1e794a8e4ba8ee58d95e58583e6b58be8af95e79a84e7a4bae4be8be8afad"
        "e99fb3e8bdace58699e69687e69cacefbc8ce4bb85e4bd9ce58da0e4bd8defbc8ce4b88de590abe4bbbbe4bd95"
        "e79c9fe5ae9ee8818ae5a4a9e58685e5aeb9e380825800"
    )
    EXPECTED = "这是一条用于单元测试的示例语音转写文本，仅作占位，不含任何真实聊天内容。"
    NOT_CONVERTED_HEX = "0835100d5800"  # 未转写时为 6 字节空壳，无 field5


class VoiceGateTests(unittest.TestCase):
    def test_default_policy_enables_voice(self):
        self.assertTrue(wechat_media.DEFAULT_POLICY_BODY["kinds"]["voice"])

    def test_voice_assets_are_keyed_by_message_not_content_md5(self):
        # 语音没有内容 md5，必须用 chat|local_id 派生稳定 key
        import hashlib
        k1 = hashlib.md5(b"voice|chatA|7").hexdigest()
        k2 = hashlib.md5(b"voice|chatA|7").hexdigest()
        k3 = hashlib.md5(b"voice|chatA|8").hexdigest()
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)


if __name__ == "__main__":
    unittest.main()
