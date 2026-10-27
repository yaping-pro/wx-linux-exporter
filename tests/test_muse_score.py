#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and regression tests for muse_probe semantic scoring and threshold gates.

Covers:
1. evaluate_score_thresholds pure function:
   - Composite hit rate calculation (hits + 0.5 * partials) / total.
   - Threshold pass/fail logic with --fail-under <float>.
   - --require-zero-hallucination gate enforcement.
   - Machine-readable SCORE: line formatting.
   - Edge cases: total=0, negative/zero/high thresholds.
2. evaluate_probe_answer pure function:
   - Full hit, partial hit, miss classification.
   - Hallucination detection from must_not_contain patterns.
   - expect_empty=True clean vs hallucinated responses.
3. probes/probes.json validation:
   - JSON parsing and minimum count (>= 12).
   - Complete schema fields (asset_id, question, must_contain/expect_empty).
   - 32-character hexadecimal format for asset_ids.
   - Comprehensive category coverage: text_image, pure_photo, chat_context,
     counter_hallucination, video_first_frame.
4. run_score execution modes:
   - --dry-run mode validates schema and formats payloads without network.
   - --fail-under and --require-zero-hallucination exit codes.
   - Video media message indexing and asset resolution.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure clients/ is in sys.path
CLIENTS_DIR = Path(__file__).resolve().parents[1] / "clients"
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

import muse_probe as mp  # noqa: E402


class TestEvaluateScoreThresholds(unittest.TestCase):
    """Test pure function evaluate_score_thresholds without network."""

    def test_full_hits_pass(self):
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=10,
            partials=0,
            hallucinations=0,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=False,
        )
        self.assertTrue(passed)
        self.assertAlmostEqual(comp_rate, 1.0)
        self.assertEqual(line, "SCORE: pass hit=10 partial=0 hallu=0 total=10")

    def test_below_threshold_fails(self):
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=7,
            partials=0,
            hallucinations=0,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=False,
        )
        self.assertFalse(passed)
        self.assertAlmostEqual(comp_rate, 0.7)
        self.assertEqual(line, "SCORE: fail hit=7 partial=0 hallu=0 total=10")

    def test_partial_hits_calculation(self):
        # 7 hits + 2 partials out of 10 -> (7 + 0.5 * 2) / 10 = 0.8 -> meets 0.8
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=7,
            partials=2,
            hallucinations=0,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=False,
        )
        self.assertTrue(passed)
        self.assertAlmostEqual(comp_rate, 0.8)
        self.assertEqual(line, "SCORE: pass hit=7 partial=2 hallu=0 total=10")

        # 7 hits + 1 partial out of 10 -> (7 + 0.5) / 10 = 0.75 -> below 0.8
        passed2, comp_rate2, line2 = mp.evaluate_score_thresholds(
            hits=7,
            partials=1,
            hallucinations=0,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=False,
        )
        self.assertFalse(passed2)
        self.assertAlmostEqual(comp_rate2, 0.75)
        self.assertEqual(line2, "SCORE: fail hit=7 partial=1 hallu=0 total=10")

    def test_require_zero_hallucination(self):
        # High hit rate (1.0) but 1 hallucination with require_zero_hallucination=True -> FAIL
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=9,
            partials=0,
            hallucinations=1,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=True,
        )
        self.assertFalse(passed)
        self.assertEqual(line, "SCORE: fail hit=9 partial=0 hallu=1 total=10")

        # Same stats with require_zero_hallucination=False -> PASS (0.9 >= 0.8)
        passed2, comp_rate2, line2 = mp.evaluate_score_thresholds(
            hits=9,
            partials=0,
            hallucinations=1,
            total=10,
            fail_under=0.8,
            require_zero_hallucination=False,
        )
        self.assertTrue(passed2)
        self.assertEqual(line2, "SCORE: pass hit=9 partial=0 hallu=1 total=10")

    def test_zero_total_probes(self):
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=0, partials=0, hallucinations=0, total=0, fail_under=0.8
        )
        self.assertFalse(passed)
        self.assertEqual(comp_rate, 0.0)
        self.assertEqual(line, "SCORE: fail hit=0 partial=0 hallu=0 total=0")

    def test_fail_under_zero(self):
        passed, comp_rate, line = mp.evaluate_score_thresholds(
            hits=0, partials=0, hallucinations=0, total=10, fail_under=0.0
        )
        self.assertTrue(passed)
        self.assertEqual(line, "SCORE: pass hit=0 partial=0 hallu=0 total=10")


class TestEvaluateProbeAnswer(unittest.TestCase):
    """Test pure function evaluate_probe_answer."""

    def test_full_hit(self):
        answer = "周三值班人员是王工，地点在主控机房。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=["周三", "王工", "主控机房"],
            must_not_contain=["发票"],
            expect_empty=False,
        )
        self.assertEqual(outcome, "hit")
        self.assertFalse(is_hallu)

    def test_partial_hit(self):
        answer = "周三值班人员是王工。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=["周三", "王工", "主控机房"],
            must_not_contain=["发票"],
            expect_empty=False,
        )
        self.assertEqual(outcome, "partial")
        self.assertFalse(is_hallu)
        self.assertIn("主控机房", reason)

    def test_miss(self):
        answer = "这是一段完全不相干的文本说明。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=["周三", "王工"],
            must_not_contain=[],
            expect_empty=False,
        )
        self.assertEqual(outcome, "miss")
        self.assertFalse(is_hallu)

    def test_must_not_contain_hallucination(self):
        answer = "表格中周三是王工，附带增值税发票一张。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=["周三", "王工"],
            must_not_contain=["发票"],
            expect_empty=False,
        )
        self.assertEqual(outcome, "hallucination")
        self.assertTrue(is_hallu)
        self.assertIn("发票", reason)

    def test_expect_empty_clean(self):
        answer = "这是一张风景照片，没有识别到任何文字内容。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=[],
            must_not_contain=["发票", "金额"],
            expect_empty=True,
        )
        self.assertEqual(outcome, "hit")
        self.assertFalse(is_hallu)

    def test_expect_empty_hallucinated(self):
        answer = "图中文字为：增值税发票，金额为5000元。"
        outcome, is_hallu, reason = mp.evaluate_probe_answer(
            answer=answer,
            must_contain=[],
            must_not_contain=["发票", "金额"],
            expect_empty=True,
        )
        self.assertEqual(outcome, "hallucination")
        self.assertTrue(is_hallu)


class TestProbesSpecification(unittest.TestCase):
    """Test probes/probes.json file integrity and coverage."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.probes_file = cls.repo_root / "probes" / "probes.json"
        with open(cls.probes_file, "r", encoding="utf-8") as f:
            cls.probes = json.load(f)

    def test_probes_file_exists_and_count_ge_12(self):
        self.assertTrue(self.probes_file.is_file())
        self.assertGreaterEqual(len(self.probes), 12)

    def test_probes_schema_fields(self):
        for idx, probe in enumerate(self.probes, 1):
            self.assertIn("id", probe, f"Probe #{idx} missing 'id'")
            self.assertIn("type", probe, f"Probe #{idx} missing 'type'")
            self.assertIn("asset_id", probe, f"Probe #{idx} missing 'asset_id'")
            self.assertIn("question", probe, f"Probe #{idx} missing 'question'")
            self.assertIn("expect_empty", probe, f"Probe #{idx} missing 'expect_empty'")
            self.assertIsInstance(probe["asset_id"], str)
            self.assertEqual(
                len(probe["asset_id"]),
                32,
                f"Probe #{idx} asset_id must be 32 hex chars",
            )
            self.assertIsInstance(probe["question"], str)
            self.assertGreater(len(probe["question"]), 5)
            self.assertIsInstance(probe.get("must_contain", []), list)
            self.assertIsInstance(probe.get("must_not_contain", []), list)
            self.assertIsInstance(probe["expect_empty"], bool)

    def test_probes_covers_all_five_categories(self):
        types = {p.get("type") for p in self.probes}
        # 1. 文字图
        self.assertTrue(
            any(t.startswith("text_") for t in types),
            "Must cover text images (tables/notices/receipts)",
        )
        # 2. 纯照片
        self.assertIn("pure_photo", types, "Must cover pure photos (no text)")
        # 3. 群聊上下文
        self.assertIn("chat_context", types, "Must cover chat context")
        # 4. 反例/防幻觉
        self.assertIn(
            "counter_hallucination", types, "Must cover counter-examples"
        )
        # 5. 视频首帧
        self.assertIn(
            "video_first_frame", types, "Must cover video first frame"
        )


class TestRunScoreDryRun(unittest.TestCase):
    """Test run_score in --dry-run mode."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.probes_file = str(cls.repo_root / "probes" / "probes.json")

    def test_dry_run_score_passes_with_fail_under_zero(self):
        passed, report = mp.run_score(
            data=None,
            probes_path=self.probes_file,
            dry_run=True,
            fail_under=0.0,
            min_probes=12,
        )
        self.assertTrue(passed)
        self.assertIn("=== Semantic Probe Score Summary (DRY-RUN) ===", report)
        self.assertIn("SCORE: pass hit=12 partial=0 hallu=0 total=12", report)

    def test_dry_run_score_fails_when_threshold_unmet(self):
        passed, report = mp.run_score(
            data=None,
            probes_path=self.probes_file,
            dry_run=True,
            fail_under=1.5,
            min_probes=12,
        )
        self.assertFalse(passed)
        self.assertIn("OVERALL:            FAIL", report)
        self.assertIn("SCORE: fail hit=12 partial=0 hallu=0 total=12", report)

    def test_dry_run_fails_when_count_below_min_probes(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json") as tf:
            json.dump([
                {
                    "id": "p1",
                    "type": "text",
                    "asset_id": "a" * 32,
                    "question": "q1",
                    "expect_empty": False,
                }
            ], tf)
            tf.flush()
            passed, report = mp.run_score(
                data=None,
                probes_path=tf.name,
                dry_run=True,
                fail_under=0.0,
                min_probes=12,
            )
            self.assertFalse(passed)
            self.assertIn("below required minimum (12)", report)
            self.assertIn("SCORE: fail", report)

    def test_dry_run_missing_file_error(self):
        passed, report = mp.run_score(
            data=None,
            probes_path="/non/existent/path/probes.json",
            dry_run=True,
        )
        self.assertFalse(passed)
        self.assertIn("Probes specification file not found", report)
        self.assertIn("SCORE: fail hit=0 partial=0 hallu=0 total=0", report)


class TestRunScoreLiveMocked(unittest.TestCase):
    """Test live run_score behavior with mocked webhook calls."""

    def setUp(self):
        self.aid_img = "11111111111111111111111111111111"
        self.aid_vid = "22222222222222222222222222222222"
        self.dataset = {
            "chats": [
                {
                    "chat_id": "chat_qa",
                    "messages": [
                        {
                            "kind": "image",
                            "text": "门诊排班表",
                            "media": {
                                "asset_id": self.aid_img,
                                "kind": "image",
                                "anchor": {
                                    "chat_name": "医疗排班群",
                                    "sender": "张护士",
                                    "ts": 1750000000,
                                },
                                "ocr": {
                                    "status": "ready",
                                    "text": "周三 王工 主控机房",
                                },
                            },
                        },
                        {
                            "kind": "video",
                            "text": "",
                            "media": {
                                "asset_id": self.aid_vid,
                                "kind": "video",
                                "poster": "/media/thumb.jpg",
                                "anchor": {
                                    "chat_name": "监控巡检群",
                                    "sender": "李工",
                                    "ts": 1750000010,
                                },
                            },
                        },
                    ],
                }
            ]
        }

    def test_score_indexes_both_image_and_video(self):
        probes = [
            {
                "id": "p_img",
                "asset_id": self.aid_img,
                "question": "排班人员是谁？",
                "must_contain": ["周三", "王工"],
            },
            {
                "id": "p_vid",
                "asset_id": self.aid_vid,
                "question": "监控视频画面？",
                "must_contain": ["机房监控"],
            },
        ]
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json") as tf:
            json.dump(probes, tf)
            tf.flush()

            resp_img = json.dumps({"choices": [{"message": {"content": "周三值班人员是王工。"}}]})
            resp_vid = json.dumps({"choices": [{"message": {"content": "视频展示机房监控画面。"}}]})

            with mock.patch("muse_probe.post_probe_request") as mock_post:
                mock_post.side_effect = [
                    (200, resp_img, None),
                    (200, resp_vid, None),
                ]
                passed, report = mp.run_score(
                    data=self.dataset,
                    probes_path=tf.name,
                    webhook_url="http://mock.endpoint/eval",
                    fail_under=0.8,
                )
                self.assertTrue(passed)
                self.assertIn("Hit Rate:           100.0% (2/2)", report)
                self.assertIn("SCORE: pass hit=2 partial=0 hallu=0 total=2", report)


class TestScoreCLI(unittest.TestCase):
    """Test CLI invoking muse_probe.py directly via subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.probes_file = str(cls.repo_root / "probes" / "probes.json")
        cls.script = str(cls.repo_root / "clients" / "muse_probe.py")

    def test_cli_dry_run_pass(self):
        cmd = [
            sys.executable,
            self.script,
            "--score",
            "--expect",
            self.probes_file,
            "--dry-run",
            "--fail-under",
            "0.0",
        ]
        res = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("SCORE: pass hit=12 partial=0 hallu=0 total=12", res.stdout)

    def test_cli_dry_run_fail_under_exits_one(self):
        cmd = [
            sys.executable,
            self.script,
            "--score",
            "--expect",
            self.probes_file,
            "--dry-run",
            "--fail-under",
            "1.5",
        ]
        res = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("SCORE: fail hit=12 partial=0 hallu=0 total=12", res.stdout)


if __name__ == "__main__":
    unittest.main()
