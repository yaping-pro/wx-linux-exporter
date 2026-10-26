#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
muse_probe.py - Downstream probe verifying that Muse understands WeChat images.

Zero third-party dependencies (Python 3 stdlib only).
Supports three verification modes:
  1. --audit: Data-layer sanity check, validates OCR/anchor.
  2. --score --expect <probes.json>: Semantic accuracy & hallucination.
  3. --dry-run (default): Preview payload without network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Enable running directly from repo root or clients/ dir
_CLIENTS_DIR = Path(__file__).resolve().parent
if str(_CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CLIENTS_DIR))

try:
    from assistant_webhook import (  # type: ignore
        DEFAULT_ENDPOINT,
        DEFAULT_TOKEN,
        fetch_export,
    )
except ImportError:
    DEFAULT_ENDPOINT = "http://127.0.0.1:8765/api/wechat/export"
    DEFAULT_TOKEN = ""

    def fetch_export(  # type: ignore
        endpoint: str,
        token: str = "",
        watermark: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        url = endpoint
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "wx-linux-exporter-muse-probe/1.0"},
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                body = resp.read().decode("utf-8", "replace")
                return status, json.loads(body), None
        except urllib.error.HTTPError as e:
            return e.code, None, f"HTTPError {e.code}"
        except Exception as e:
            return 0, None, f"{type(e).__name__}: {e}"


def mask_sensitive_digits(text: str) -> str:
    """Mask consecutive digits of length >= 11 (phone, ID, bank) as ***."""
    if not text:
        return ""
    return re.sub(r"\d{11,}", "***", text)


def extract_image_messages(
    data: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Extract all image messages paired with their chat_id."""
    images: List[Tuple[str, Dict[str, Any]]] = []
    chats = data.get("chats", [])
    for chat in chats:
        chat_id = chat.get("chat_id", "unknown")
        messages = chat.get("messages", [])
        for msg in messages:
            kind = msg.get("kind", msg.get("type", ""))
            media = msg.get("media")
            is_img = kind == "image" or (
                isinstance(media, dict) and media.get("kind") == "image"
            )
            if is_img:
                images.append((chat_id, msg))
    return images


def run_audit(data: Dict[str, Any]) -> Tuple[bool, str]:
    """Execute --audit data-layer checks and generate table report."""
    images = extract_image_messages(data)
    total_images = len(images)

    lines: List[str] = []
    lines.append("=== WeChat Image Media Enrichment Audit ===")
    lines.append(f"Total Image Messages Detected: {total_images}")

    if total_images == 0:
        lines.append("No image messages found in dataset.")
        lines.append("AUDIT: PASS (0 images)")
        return True, "\n".join(lines)

    text_non_empty_count = 0
    has_media_count = 0
    ocr_status_counts: Dict[str, int] = {}
    anchor_complete_count = 0
    chat_name_diff_count = 0

    required_anchor_keys = (
        "chat_id", "chat_name", "local_id", "ts", "sender"
    )

    samples: List[Dict[str, Any]] = []

    for chat_id, msg in images:
        text = msg.get("text", "")
        if text and text.strip():
            text_non_empty_count += 1

        media = msg.get("media")
        if isinstance(media, dict) and media:
            has_media_count += 1
            ocr = media.get("ocr")
            if isinstance(ocr, dict):
                st = ocr.get("status", "unknown")
            else:
                st = "missing"
            ocr_status_counts[st] = ocr_status_counts.get(st, 0) + 1

            anchor = media.get("anchor")
            if isinstance(anchor, dict):
                if all(
                    anchor.get(k) is not None for k in required_anchor_keys
                ):
                    anchor_complete_count += 1
                c_name = anchor.get("chat_name")
                c_id = anchor.get("chat_id")
                if c_name and c_id and c_name != c_id:
                    chat_name_diff_count += 1
        else:
            ocr_status_counts["no_media"] = (
                ocr_status_counts.get("no_media", 0) + 1
            )

        if len(samples) < 5:
            samples.append({
                "chat_id": chat_id,
                "msg": msg,
            })

    text_rate = (text_non_empty_count / total_images) * 100.0
    media_rate = (has_media_count / total_images) * 100.0
    ready_empty_count = (
        ocr_status_counts.get("ready", 0) + ocr_status_counts.get("empty", 0)
    )
    ready_empty_rate = (ready_empty_count / total_images) * 100.0

    if has_media_count > 0:
        anchor_rate = (anchor_complete_count / has_media_count) * 100.0
        chat_name_rate = (chat_name_diff_count / has_media_count) * 100.0
    else:
        anchor_rate = 0.0
        chat_name_rate = 0.0

    lines.append("")
    lines.append(f"{'Metric':<36} | {'Count':<8} | {'Ratio':<10}")
    lines.append("-" * 60)
    lines.append(f"{'Total Images':<36} | {total_images:<8} | 100.0%")
    lines.append(
        f"{'Text Non-empty':<36} | {text_non_empty_count:<8} | "
        f"{text_rate:.1f}%"
    )
    lines.append(
        f"{'Media Object Present':<36} | {has_media_count:<8} | "
        f"{media_rate:.1f}%"
    )
    lines.append(
        f"{'Anchor Complete (5 fields)':<36} | {anchor_complete_count:<8} | "
        f"{anchor_rate:.1f}%"
    )
    lines.append(
        f"{'Chat Name Resolved (!= chat_id)':<36} | "
        f"{chat_name_diff_count:<8} | {chat_name_rate:.1f}%"
    )

    lines.append("")
    lines.append("OCR Status Distribution:")
    for status, count in sorted(ocr_status_counts.items()):
        ratio = (count / total_images) * 100.0
        lines.append(f"  - {status:<15}: {count:<6} ({ratio:.1f}%)")

    lines.append("")
    lines.append("Sample Images (up to 5, masked):")
    lines.append(
        f"{'#':<3} | {'Chat Name':<20} | {'Timestamp':<24} | "
        f"{'OCR Preview (≤80 chars)':<40}"
    )
    lines.append("-" * 92)
    for idx, s in enumerate(samples, 1):
        msg = s["msg"]
        media = msg.get("media") or {}
        anchor = media.get("anchor") or {}
        c_name = anchor.get("chat_name") or s["chat_id"]
        ts_val = str(anchor.get("ts") or msg.get("ts", ""))
        ocr = media.get("ocr") or {}
        raw_ocr = str(ocr.get("text") or msg.get("text") or "")
        preview = raw_ocr.strip().replace("\n", " ")[:80]
        masked_preview = mask_sensitive_digits(preview)
        masked_cname = mask_sensitive_digits(c_name[:20])
        lines.append(
            f"{idx:<3} | {masked_cname:<20} | {ts_val:<24} | "
            f"{masked_preview:<40}"
        )

    pass_text = text_rate >= 80.0
    pass_anchor = (
        (has_media_count == 0)
        or (anchor_complete_count == has_media_count)
    )
    pass_ocr = ready_empty_rate >= 90.0

    passed = pass_text and pass_anchor and pass_ocr

    lines.append("")
    status_str = "PASS" if passed else "FAIL"
    lines.append(
        f"AUDIT: {status_str} "
        f"(text_rate={text_rate:.1f}%[>=80%], "
        f"anchor={anchor_complete_count}/{has_media_count}[=100%], "
        f"ocr_ready_empty={ready_empty_rate:.1f}%[>=90%])"
    )

    return passed, "\n".join(lines)


def build_evaluation_prompt(msg: Dict[str, Any], question: str) -> str:
    """Compose semantic evaluation prompt with context and question."""
    context_str = json.dumps(msg, ensure_ascii=False, indent=2)
    return (
        f"WeChat Message Context:\n"
        f"```json\n{context_str}\n```\n\n"
        f"Question: {question}\n"
        f"Please answer based strictly on the message context and its "
        f"media enrichment information."
    )


def format_webhook_request(
    webhook_type: str,
    prompt: str,
    msg: Dict[str, Any],
) -> Dict[str, Any]:
    """Format payload for the target webhook (custom/OpenAI, dify, feishu)."""
    if webhook_type == "dify":
        return {
            "inputs": {
                "question": prompt,
                "message": msg,
            },
            "response_mode": "blocking",
            "user": "muse_probe",
        }
    if webhook_type == "feishu":
        return {
            "msg_type": "text",
            "content": {
                "text": prompt,
            },
        }
    # Default custom / OpenAI-compatible
    return {
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }


def parse_webhook_response(webhook_type: str, resp_text: str) -> str:
    """Extract assistant answer text from various webhook response shapes."""
    if not resp_text:
        return ""
    try:
        data = json.loads(resp_text)
        if isinstance(data, dict):
            # OpenAI shape: choices[0].message.content
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                c0 = choices[0]
                if isinstance(c0, dict):
                    msg = c0.get("message")
                    if isinstance(msg, dict) and "content" in msg:
                        return str(msg["content"] or "")
                    if "text" in c0:
                        return str(c0["text"] or "")
            # Dify shape: answer or data.outputs.answer
            if "answer" in data:
                return str(data["answer"] or "")
            outputs = (
                data.get("outputs")
                or (data.get("data") or {}).get("outputs")
            )
            if isinstance(outputs, dict) and "answer" in outputs:
                return str(outputs["answer"] or "")
            if "content" in data:
                return str(data["content"] or "")
            if "text" in data:
                return str(data["text"] or "")
    except Exception:
        pass
    return resp_text


def check_hallucination(
    answer: str,
    must_not_contain: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Judge if an answer intended to be empty contains hallucinations."""
    ans = answer.strip()
    if not ans:
        return False, "empty answer"

    if must_not_contain:
        for forbidden in must_not_contain:
            if forbidden and forbidden in ans:
                return True, f"contains forbidden phrase: '{forbidden}'"

    suspicious_patterns = [
        r"发票",
        r"金额",
        r"文字\s*[:：]",
        r"包含以下文字",
        r"图中文字为",
    ]
    for pat in suspicious_patterns:
        if re.search(pat, ans):
            return True, f"matches suspicious hallucination pattern: {pat}"

    # Length > 200 with digits or quotes
    if len(ans) > 200:
        has_digits = bool(re.search(r"\d", ans))
        has_quotes = bool(re.search(r"[\"\'“”‘’]", ans))
        if has_digits or has_quotes:
            return (
                True,
                f"excessive length ({len(ans)} chars) with digits/quotes",
            )

    return False, "ok"


def post_probe_request(
    webhook_url: str,
    payload: Dict[str, Any],
    timeout: int = 30,
) -> Tuple[int, str, Optional[str]]:
    """Post payload to webhook and return (status_code, body, error)."""
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "wx-linux-exporter-muse-probe/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
            return code, body, None
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, err_body, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return 0, "", f"URLError: {e.reason}"
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {e}"


def run_score(
    data: Dict[str, Any],
    probes_path: str,
    webhook_url: str,
    webhook_type: str = "custom",
    timeout: int = 30,
) -> Tuple[bool, str]:
    """Execute --score evaluation against probes.json."""
    if not os.path.isfile(probes_path):
        return False, f"Probes specification file not found: {probes_path}"

    with open(probes_path, "r", encoding="utf-8") as f:
        probes: List[Dict[str, Any]] = json.load(f)

    if not isinstance(probes, list) or not probes:
        return False, "Probes file is empty or not a valid JSON list."

    images = extract_image_messages(data)
    asset_map: Dict[str, Dict[str, Any]] = {}
    for _, msg in images:
        media = msg.get("media")
        if isinstance(media, dict):
            aid = media.get("asset_id")
            if aid:
                asset_map[aid] = msg

    total_probes = len(probes)
    hits = 0
    hallucinations = 0
    grounding_successes = 0

    details: List[str] = []
    all_passed = True

    for idx, probe in enumerate(probes, 1):
        asset_id = probe.get("asset_id", "")
        question = probe.get("question", "")
        must_contain = probe.get("must_contain", [])
        must_not_contain = probe.get("must_not_contain", [])
        expect_empty = bool(probe.get("expect_empty", False))

        msg = asset_map.get(asset_id)
        if not msg:
            details.append(
                f"[{idx}/{total_probes}] Asset '{asset_id}' not found! FAIL"
            )
            all_passed = False
            continue

        prompt = build_evaluation_prompt(msg, question)
        req_payload = format_webhook_request(webhook_type, prompt, msg)

        code, resp_body, err = post_probe_request(
            webhook_url, req_payload, timeout=timeout
        )
        if err or code < 200 or code >= 300:
            details.append(
                f"[{idx}/{total_probes}] Request failed (status={code}): "
                f"{err} FAIL"
            )
            all_passed = False
            continue

        answer = parse_webhook_response(webhook_type, resp_body)

        probe_ok = True
        is_hallu = False
        hallu_reason = ""

        if expect_empty:
            is_hallu, hallu_reason = check_hallucination(
                answer, must_not_contain
            )
            if is_hallu:
                hallucinations += 1
                probe_ok = False
            else:
                hits += 1
        else:
            missing = [mc for mc in must_contain if mc not in answer]
            if missing:
                probe_ok = False
            else:
                hits += 1

        # Check grounding against chat_name
        media = msg.get("media") or {}
        anchor = media.get("anchor") or {}
        c_name = anchor.get("chat_name")
        if c_name and c_name in answer:
            grounding_successes += 1
        elif not expect_empty and probe_ok:
            grounding_successes += 1

        if not probe_ok:
            all_passed = False

        status_tag = "PASS" if probe_ok else "FAIL"
        details.append(
            f"--- Probe #{idx} [{status_tag}] (asset={asset_id[:12]}...) ---"
        )
        details.append(f"Question: {question}")
        if expect_empty:
            details.append(
                f"Expect Empty: True, Hallucination: {is_hallu} "
                f"({hallu_reason})"
            )
        else:
            details.append(f"Must Contain: {must_contain}")
        details.append(f"Answer: {answer.strip()[:300]}")
        if is_hallu:
            details.append(
                f"Full Suspicious Answer for Review:\n{answer}"
            )

    if total_probes > 0:
        hit_rate = (hits / total_probes) * 100.0
        grounding_rate = (grounding_successes / total_probes) * 100.0
    else:
        hit_rate = 0.0
        grounding_rate = 0.0

    summary_lines = [
        "=== Semantic Probe Score Summary ===",
        f"Total Probes:       {total_probes}",
        f"Hit Rate:           {hit_rate:.1f}% ({hits}/{total_probes})",
        f"Hallucinations:     {hallucinations}",
        f"Grounding Accuracy: {grounding_rate:.1f}%",
        f"OVERALL:            {'PASS' if all_passed else 'FAIL'}",
        "",
        "Details:",
        *details,
    ]
    return all_passed, "\n".join(summary_lines)


def run_dry_run(
    data: Dict[str, Any],
    webhook_type: str = "custom",
    limit: int = 3,
) -> str:
    """Execute --dry-run printing payloads truncated to 800 chars."""
    images = extract_image_messages(data)
    lines: List[str] = []
    lines.append(
        f"[DRY-RUN] Found {len(images)} image messages. "
        f"Showing up to {limit} sample payloads:"
    )

    if not images:
        lines.append("No image messages found.")
        return "\n".join(lines)

    sample_items = images[:limit]
    for idx, (chat_id, msg) in enumerate(sample_items, 1):
        media = msg.get("media") or {}
        aid = media.get("asset_id", "none")
        prompt = build_evaluation_prompt(msg, "这张图里说了什么？前后在聊什么？")
        payload = format_webhook_request(webhook_type, prompt, msg)
        dumped = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(dumped) > 800:
            truncated = dumped[:800] + "\n... [TRUNCATED to 800 chars]"
        else:
            truncated = dumped
        lines.append(
            f"\n--- Payload Sample {idx}/{len(sample_items)} "
            f"(asset_id={aid}) ---"
        )
        lines.append(truncated)

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Muse Probe - Verify that Muse truly understands enriched "
            "WeChat image messages."
        )
    )
    parser.add_argument(
        "--endpoint",
        "--export-url",
        dest="endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"WeChat Exporter API endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Bearer token (default: env WECHAT_BEARER_TOKEN)",
    )
    parser.add_argument(
        "--input",
        dest="input_file",
        help="Path to local export JSON file (offline, bypasses network)",
    )
    parser.add_argument(
        "--since",
        help="Watermark filter for export endpoint (?since=...)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="HTTP network timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Sample display limit (default: 5)",
    )

    # Operational modes
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--audit",
        action="store_true",
        help="Run data-layer sanity check on OCR and anchor metadata",
    )
    mode_group.add_argument(
        "--score",
        action="store_true",
        help="Run semantic evaluation with question probes against webhook",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview probe payloads without network delivery (default)",
    )

    # Score mode parameters
    parser.add_argument(
        "--expect",
        dest="expect_file",
        help="Path to probes.json specification file (required with --score)",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("ASSISTANT_WEBHOOK_URL", ""),
        help="Target Webhook URL for scoring",
    )
    parser.add_argument(
        "--webhook-type",
        choices=["custom", "dify", "feishu"],
        default=os.environ.get("ASSISTANT_WEBHOOK_TYPE", "custom").lower(),
        help="Target Webhook payload type: custom (default), dify, feishu",
    )

    return parser.parse_args()


def load_dataset(
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load export dataset from local file or remote endpoint."""
    if args.input_file:
        if not os.path.isfile(args.input_file):
            return None, f"Input file not found: {args.input_file}"
        try:
            with open(args.input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data, None
        except Exception as e:
            return None, f"Failed to parse input JSON '{args.input_file}': {e}"

    # Fetch from API endpoint
    code, data, err = fetch_export(
        endpoint=args.endpoint,
        token=args.token,
        watermark=args.since,
        timeout=args.timeout,
    )
    if err or code != 200 or not data:
        return None, f"Fetch export failed (status={code}): {err}"
    return data, None


def main() -> int:
    args = parse_args()

    is_audit = args.audit
    is_score = args.score

    if is_score:
        if not args.expect_file:
            print(
                "[ERROR] --score requires --expect <probes.json>",
                file=sys.stderr,
            )
            return 1
        if not args.webhook_url:
            print("[ERROR] --score requires --webhook-url", file=sys.stderr)
            return 1

    data, err = load_dataset(args)
    if err or data is None:
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    if is_audit:
        passed, report = run_audit(data)
        print(report)
        return 0 if passed else 1

    if is_score:
        passed, report = run_score(
            data=data,
            probes_path=args.expect_file,
            webhook_url=args.webhook_url,
            webhook_type=args.webhook_type,
            timeout=args.timeout,
        )
        print(report)
        return 0 if passed else 1

    # Dry-run
    preview = run_dry_run(
        data, webhook_type=args.webhook_type, limit=args.limit
    )
    print(preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
