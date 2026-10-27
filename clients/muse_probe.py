#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
muse_probe.py - Downstream probe verifying that Muse understands WeChat images.

Zero third-party dependencies (Python 3 stdlib only).
Supports verification modes:
  1. --audit: Data-layer sanity check, validates OCR/anchor/media.
  2. --score --expect <probes.json>: Semantic accuracy & hallucination.
  3. --vision: Generate OpenAI-compatible multimodal payloads for Muse.
  4. --estimate-bytes: Size comparison across media/ocr filter combinations.
  5. --dry-run (default): Preview payload without network calls.
"""

from __future__ import annotations

import argparse
import base64
import copy
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
if _CLIENTS_DIR.exists() and str(_CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CLIENTS_DIR))

try:
    from clients.assistant_webhook import (  # type: ignore
        DEFAULT_ENDPOINT,
        DEFAULT_TOKEN,
        derive_base_url,
        fetch_export,
    )
except ImportError:
    try:
        from assistant_webhook import (  # type: ignore
            DEFAULT_ENDPOINT,
            DEFAULT_TOKEN,
            derive_base_url,
            fetch_export,
        )
    except ImportError:
        pass
    DEFAULT_ENDPOINT = "http://127.0.0.1:8765/api/wechat/export"
    DEFAULT_TOKEN = ""

    def derive_base_url(export_url: str) -> str:  # type: ignore
        if not export_url:
            return ""
        try:
            parsed = urllib.parse.urlparse(export_url)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
        return ""

    def fetch_export(  # type: ignore
        endpoint: str,
        token: str = "",
        watermark: Optional[str] = None,
        timeout: int = 15,
        media: str = "all",
        media_chats: str = "",
        ocr: str = "head",
    ) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        url = endpoint
        parsed = urllib.parse.urlparse(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        if watermark:
            query["since"] = [watermark]
        if media:
            query["media"] = [media]
        if media_chats:
            query["media_chats"] = [media_chats]
        if ocr:
            query["ocr"] = [ocr]
        url = urllib.parse.urlunparse(
            parsed._replace(
                query=urllib.parse.urlencode(query, doseq=True)
            )
        )
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


def extract_media_messages(
    data: Dict[str, Any],
    target_kind: str = "image",
    chats_filter: Optional[List[str]] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Extract messages matching target_kind ('image' or 'video')."""
    results: List[Tuple[str, Dict[str, Any]]] = []
    chats = data.get("chats", [])
    for chat in chats:
        chat_id = chat.get("chat_id", "unknown")
        if chats_filter and chat_id not in chats_filter:
            continue
        messages = chat.get("messages", [])
        for msg in messages:
            kind = msg.get("kind", msg.get("type", ""))
            media = msg.get("media")
            mkind = (
                media.get("kind")
                if isinstance(media, dict)
                else ""
            )
            if target_kind == "video":
                if kind in ("video", "shortvideo") or mkind == "video":
                    results.append((chat_id, msg))
            else:
                if kind == "image" or mkind == "image":
                    results.append((chat_id, msg))
    return results


def resolve_ocr_enabled_policy(
    policy_path: Optional[str],
    assume_disabled: bool,
    input_file: Optional[str],
) -> Tuple[bool, str]:
    """Determine whether OCR is enabled based on policy or flags."""
    if assume_disabled:
        return False, "flag (--assume-ocr-disabled)"

    cand_paths: List[Path] = []
    if policy_path:
        cand_paths.append(Path(policy_path))
    if input_file:
        inp = Path(input_file).resolve().parent
        cand_paths.append(inp / "media_policy.json")
        cand_paths.append(inp / "config" / "media_policy.json")
        cand_paths.append(inp.parents[1] / "config" / "media_policy.json")

    for p in cand_paths:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    pol = json.load(f)
                    img_cfg = pol.get("image", {})
                    if isinstance(img_cfg, dict):
                        ocr_cfg = img_cfg.get("ocr", {})
                        if isinstance(ocr_cfg, dict) and "enabled" in ocr_cfg:
                            val = bool(ocr_cfg["enabled"])
                            return val, f"policy ({p})"
            except Exception:
                pass

    return True, "default (enabled)"


def run_audit(
    data: Dict[str, Any],
    ocr_enabled: bool = True,
    ocr_source: str = "default",
    chats_filter: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Execute --audit data-layer checks and generate table report."""
    images = extract_media_messages(
        data, target_kind="image", chats_filter=chats_filter
    )
    total_images = len(images)

    lines: List[str] = []
    lines.append("=== WeChat Image Media Enrichment Audit ===")
    lines.append(f"Total Image Messages Detected: {total_images}")
    ocr_state_label = "enabled" if ocr_enabled else "disabled"
    lines.append(
        f"OCR Policy Basis: {ocr_state_label} (source: {ocr_source})"
    )

    if total_images == 0:
        lines.append("No image messages found in dataset.")
        lines.append("AUDIT: PASS (0 images)")
        return True, "\n".join(lines)

    text_non_empty_count = 0
    has_media_count = 0
    valid_url_count = 0
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
            if media.get("url"):
                valid_url_count += 1

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
    url_rate = (valid_url_count / total_images) * 100.0
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
    if ocr_enabled:
        lines.append(
            f"{'Text Non-empty':<36} | {text_non_empty_count:<8} | "
            f"{text_rate:.1f}%"
        )
    else:
        lines.append(
            f"{'Text Non-empty (OCR disabled)':<36} | "
            f"{text_non_empty_count:<8} | n/a"
        )
    lines.append(
        f"{'Media Object Present':<36} | {has_media_count:<8} | "
        f"{media_rate:.1f}%"
    )
    lines.append(
        f"{'Media URL Resolvable':<36} | {valid_url_count:<8} | "
        f"{url_rate:.1f}%"
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

    pass_anchor = (
        (has_media_count == 0)
        or (anchor_complete_count == has_media_count)
    )
    pass_media = has_media_count == total_images
    pass_url = valid_url_count == has_media_count

    if ocr_enabled:
        pass_text = text_rate >= 80.0
        pass_ocr = ready_empty_rate >= 90.0
        passed = pass_text and pass_anchor and pass_ocr
        detail_msg = (
            f"text_rate={text_rate:.1f}%[>=80%], "
            f"anchor={anchor_complete_count}/{has_media_count}[=100%], "
            f"ocr_ready_empty={ready_empty_rate:.1f}%[>=90%]"
        )
    else:
        passed = pass_media and pass_anchor and pass_url
        detail_msg = (
            f"ocr=disabled, media={has_media_count}/{total_images}[=100%], "
            f"url={valid_url_count}/{has_media_count}[=100%], "
            f"anchor={anchor_complete_count}/{has_media_count}[=100%]"
        )

    lines.append("")
    status_str = "PASS" if passed else "FAIL"
    lines.append(f"AUDIT: {status_str} ({detail_msg})")

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


def evaluate_score_thresholds(
    hits: int,
    partials: int,
    hallucinations: int,
    total: int,
    fail_under: float = 0.8,
    require_zero_hallucination: bool = False,
) -> Tuple[bool, float, str]:
    """Evaluate score against pass/fail criteria and return machine-readable line.

    Returns:
        (passed, composite_rate, score_line)
    """
    threshold = fail_under
    if total > 0:
        composite_rate = (hits + 0.5 * partials) / total
    else:
        composite_rate = 0.0

    passed = True
    if total == 0 or composite_rate < threshold:
        passed = False
    if require_zero_hallucination and hallucinations > 0:
        passed = False
    status_tag = "pass" if passed else "fail"
    score_line = (
        f"SCORE: {status_tag} hit={hits} partial={partials} "
        f"hallu={hallucinations} total={total}"
    )
    return passed, composite_rate, score_line


def evaluate_probe_answer(
    answer: str,
    must_contain: Optional[List[str]] = None,
    must_not_contain: Optional[List[str]] = None,
    expect_empty: bool = False,
) -> Tuple[str, bool, str]:
    """Evaluate a single probe's answer.

    Returns:
        (outcome, is_hallucination, reason)
        outcome in ("hit", "partial", "miss", "hallucination")
    """
    if expect_empty:
        is_hallu, reason = check_hallucination(answer, must_not_contain)
        if is_hallu:
            return "hallucination", True, reason
        return "hit", False, "ok"

    if must_not_contain:
        for forbidden in must_not_contain:
            if forbidden and forbidden in answer:
                return (
                    "hallucination",
                    True,
                    f"contains forbidden phrase: '{forbidden}'",
                )

    if not must_contain:
        return "hit", False, "ok"

    contained = [mc for mc in must_contain if mc in answer]
    missing = [mc for mc in must_contain if mc not in answer]

    if not missing:
        return "hit", False, "all keywords matched"
    if contained:
        return (
            "partial",
            False,
            f"partial match ({len(contained)}/{len(must_contain)}): "
            f"missing {missing}",
        )
    return "miss", False, f"missing all keywords: {missing}"


def run_score(
    data: Optional[Dict[str, Any]],
    probes_path: str,
    webhook_url: str = "",
    webhook_type: str = "custom",
    timeout: int = 30,
    chats_filter: Optional[List[str]] = None,
    dry_run: bool = False,
    fail_under: float = 0.8,
    require_zero_hallucination: bool = False,
    min_probes: int = 0,
) -> Tuple[bool, str]:
    """Execute --score evaluation against probes.json."""
    if not os.path.isfile(probes_path):
        _, _, score_line = evaluate_score_thresholds(
            0, 0, 0, 0, fail_under, require_zero_hallucination
        )
        return False, (
            f"Probes specification file not found: {probes_path}\n{score_line}"
        )

    try:
        with open(probes_path, "r", encoding="utf-8") as f:
            probes: List[Dict[str, Any]] = json.load(f)
    except Exception as e:
        _, _, score_line = evaluate_score_thresholds(
            0, 0, 0, 0, fail_under, require_zero_hallucination
        )
        return False, f"Failed to parse probes file: {e}\n{score_line}"

    if not isinstance(probes, list) or not probes:
        _, _, score_line = evaluate_score_thresholds(
            0, 0, 0, 0, fail_under, require_zero_hallucination
        )
        return False, (
            "Probes file is empty or not a valid JSON list.\n" f"{score_line}"
        )

    total_probes = len(probes)
    if min_probes > 0 and total_probes < min_probes:
        score_line = f"SCORE: fail hit=0 partial=0 hallu=0 total={total_probes}"
        return False, (
            f"Probes count ({total_probes}) below required minimum "
            f"({min_probes}).\n{score_line}"
        )

    for idx, p in enumerate(probes, 1):
        if not isinstance(p, dict):
            score_line = f"SCORE: fail hit=0 partial=0 hallu=0 total={total_probes}"
            return False, (
                f"Probe #{idx} is not a valid JSON object.\n{score_line}"
            )
        if not p.get("asset_id"):
            score_line = f"SCORE: fail hit=0 partial=0 hallu=0 total={total_probes}"
            return False, (
                f"Probe #{idx} missing required 'asset_id'.\n{score_line}"
            )
        if not p.get("question"):
            score_line = f"SCORE: fail hit=0 partial=0 hallu=0 total={total_probes}"
            return False, (
                f"Probe #{idx} missing required 'question'.\n{score_line}"
            )

    if dry_run:
        hits = total_probes
        partials = 0
        hallucinations = 0
        passed, comp_rate, score_line = evaluate_score_thresholds(
            hits=hits,
            partials=partials,
            hallucinations=hallucinations,
            total=total_probes,
            fail_under=fail_under,
            require_zero_hallucination=require_zero_hallucination,
        )
        details: List[str] = []
        for idx, p in enumerate(probes, 1):
            pid = p.get("id", f"probe_{idx:02d}")
            ptype = p.get("type", "generic")
            aid = p.get("asset_id", "")
            q = p.get("question", "")
            details.append(
                f"  - [{idx}/{total_probes}] id={pid} type={ptype} "
                f"asset={aid[:12]}... question='{q[:30]}...' [SCHEMA OK]"
            )

        sample_probe = probes[0]
        sample_aid = sample_probe.get("asset_id", "")
        sample_msg: Dict[str, Any] = {
            "kind": sample_probe.get("type", "image"),
            "media": {
                "asset_id": sample_aid,
                "kind": (
                    "video"
                    if "video" in sample_probe.get("type", "")
                    else "image"
                ),
                "anchor": {
                    "chat_name": "SampleChat",
                    "sender": "SampleSender",
                    "ts": 1750000000,
                },
                "ocr": {
                    "status": "ready",
                    "text": " ".join(sample_probe.get("must_contain", [])),
                },
            },
        }
        sample_prompt = build_evaluation_prompt(
            sample_msg, sample_probe.get("question", "")
        )
        sample_payload = format_webhook_request(
            webhook_type, sample_prompt, sample_msg
        )
        dumped = json.dumps(sample_payload, ensure_ascii=False, indent=2)
        if len(dumped) > 800:
            truncated = dumped[:800] + "\n... [TRUNCATED to 800 chars]"
        else:
            truncated = dumped

        summary_lines = [
            "=== Semantic Probe Score Summary (DRY-RUN) ===",
            f"Probes File:        {probes_path}",
            f"Total Probes:       {total_probes}",
            f"Hit Rate:           100.0% ({hits}/{total_probes})",
            f"Composite Score:    {comp_rate * 100:.1f}%",
            f"Hallucinations:     {hallucinations}",
            f"Fail Under:         {fail_under}",
            f"Require Zero Hallu: {require_zero_hallucination}",
            f"OVERALL:            {'PASS' if passed else 'FAIL'}",
            "",
            "Details (Schema Validation):",
            *details,
            "",
            f"Sample Payload Preview (1/{total_probes}):",
            truncated,
            "",
            score_line,
        ]
        return passed, "\n".join(summary_lines)

    media_messages = (
        extract_media_messages(
            data or {}, target_kind="image", chats_filter=chats_filter
        )
        + extract_media_messages(
            data or {}, target_kind="video", chats_filter=chats_filter
        )
    )
    asset_map: Dict[str, Dict[str, Any]] = {}
    for _, msg in media_messages:
        media = msg.get("media")
        if isinstance(media, dict):
            aid = media.get("asset_id")
            if aid:
                asset_map[aid] = msg

    hits = 0
    partials = 0
    hallucinations = 0
    grounding_successes = 0

    details = []

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
            continue

        answer = parse_webhook_response(webhook_type, resp_body)
        outcome, is_hallu, hallu_reason = evaluate_probe_answer(
            answer=answer,
            must_contain=must_contain,
            must_not_contain=must_not_contain,
            expect_empty=expect_empty,
        )

        probe_ok = False
        if outcome == "hit":
            hits += 1
            probe_ok = True
        elif outcome == "partial":
            partials += 1
        elif outcome == "hallucination":
            hallucinations += 1

        # Check grounding against chat_name
        media = msg.get("media") or {}
        anchor = media.get("anchor") or {}
        c_name = anchor.get("chat_name")
        if c_name and c_name in answer:
            grounding_successes += 1
        elif not expect_empty and probe_ok:
            grounding_successes += 1

        status_tag = (
            "PASS"
            if probe_ok
            else ("PARTIAL" if outcome == "partial" else "FAIL")
        )
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

    passed, comp_rate, score_line = evaluate_score_thresholds(
        hits=hits,
        partials=partials,
        hallucinations=hallucinations,
        total=total_probes,
        fail_under=fail_under,
        require_zero_hallucination=require_zero_hallucination,
    )

    summary_lines = [
        "=== Semantic Probe Score Summary ===",
        f"Total Probes:       {total_probes}",
        f"Hit Rate:           {hit_rate:.1f}% ({hits}/{total_probes})",
        f"Hallucinations:     {hallucinations}",
        f"Grounding Accuracy: {grounding_rate:.1f}%",
        f"OVERALL:            {'PASS' if passed else 'FAIL'}",
        "",
        "Details:",
        *details,
        "",
        score_line,
    ]
    return passed, "\n".join(summary_lines)


def locate_local_media_file(
    media: Dict[str, Any],
    image_dir: Optional[str],
    input_file: Optional[str],
) -> Optional[Path]:
    """Search for the local image or video file on disk."""
    rel_path = media.get("path") or ""
    aid = media.get("asset_id") or ""
    ext = media.get("format") or "jpg"

    candidate_dirs: List[Path] = []
    if image_dir:
        candidate_dirs.append(Path(image_dir))
    if input_file:
        inp = Path(input_file).resolve().parent
        candidate_dirs.append(inp)
        candidate_dirs.append(inp / "exports")
        candidate_dirs.append(inp / "vault" / "exports")

    for d in candidate_dirs:
        if rel_path:
            p = d / rel_path
            if p.is_file():
                return p
        if aid:
            for variant in (
                f"image.{ext}", f"{aid}.{ext}", "image.jpg",
                "image.png", "video.mp4", f"{aid}.mp4"
            ):
                p1 = d / "media" / aid / variant
                if p1.is_file():
                    return p1
                p2 = d / aid / variant
                if p2.is_file():
                    return p2
                matches = list(d.glob(f"**/media/*/{aid}/{variant}"))
                if matches and matches[0].is_file():
                    return matches[0]

    return None
def locate_local_poster_file(
    media: Dict[str, Any],
    image_dir: Optional[str],
    input_file: Optional[str],
) -> Optional[Path]:
    """Search for local poster / thumbnail file on disk."""
    poster = media.get("poster") or ""
    aid = media.get("asset_id") or ""
    thumb_field = media.get("thumb") or ""
    video_path = media.get("path") or ""

    candidate_dirs: List[Path] = []
    if image_dir:
        candidate_dirs.append(Path(image_dir))
    if input_file:
        inp = Path(input_file).resolve().parent
        candidate_dirs.append(inp)
        candidate_dirs.append(inp / "exports")
        candidate_dirs.append(inp / "vault" / "exports")

    # 1. Direct path from thumb field (e.g. media/2026-10-27/<aid>/thumb.jpg)
    direct_paths: List[str] = []
    if thumb_field:
        direct_paths.append(thumb_field)

    # 2. Derive potential relative path from poster URL or path
    if poster and not poster.startswith("http://") and not poster.startswith("https://"):
        cleaned = poster.lstrip("/")
        if cleaned.startswith("api/wechat/"):
            direct_paths.append(cleaned[len("api/wechat/"):])
        direct_paths.append(cleaned)

    # 3. Derive relative path from video path directory (e.g. video.mp4 -> thumb.jpg)
    if video_path:
        v_parent = Path(video_path).parent
        direct_paths.append(str(v_parent / "thumb.jpg"))

    for d in candidate_dirs:
        for p_rel in direct_paths:
            if p_rel:
                p = d / p_rel
                if p.is_file():
                    return p

        # 4. Fallback search by asset_id without assuming fixed directory depth
        if aid:
            for variant in ("thumb.jpg", f"{aid}.jpg", "image.jpg", f"{aid}_thumb.jpg"):
                # Check direct media/<aid>/variant
                p1 = d / "media" / aid / variant
                if p1.is_file():
                    return p1
                p2 = d / aid / variant
                if p2.is_file():
                    return p2
                # Deep search across any directory level (e.g. media/2026-10-27/<aid>/thumb.jpg)
                matches = list(d.rglob(f"{aid}/{variant}"))
                if matches and matches[0].is_file():
                    return matches[0]

    return None
def run_vision(
    data: Dict[str, Any],
    vision_model: str = "gpt-4o",
    limit: int = 1,
    no_base64: bool = False,
    image_dir: Optional[str] = None,
    input_file: Optional[str] = None,
    public_base_url: str = "",
    export_url: str = "",
    only: str = "image",
    chats_filter: Optional[List[str]] = None,
    video_mode: str = "poster",
) -> str:
    """Generate OpenAI-compatible multimodal payload for Muse."""
    items = extract_media_messages(
        data, target_kind=only, chats_filter=chats_filter
    )
    lines: List[str] = []
    lines.append(
        "[INFO] 这是给 Muse 的载荷，本工具不发送。"
        f"（Kind: {only}, Vision Model: {vision_model}, limit: {limit}）"
    )

    if not items:
        lines.append(f"No {only} messages found in dataset.")
        return "\n".join(lines)

    sample_items = items[:limit]
    base = (
        public_base_url.rstrip("/")
        or derive_base_url(export_url).rstrip("/")
    )

    for idx, (chat_id, msg) in enumerate(sample_items, 1):
        media = msg.get("media") or {}
        aid = media.get("asset_id", "unknown")
        anchor = media.get("anchor") or {}
        chat_name = anchor.get("chat_name") or chat_id
        ts_val = anchor.get("ts") or msg.get("ts", "")
        sender = anchor.get("sender") or msg.get("sender", "")

        context_lines = [
            f"会话名称: {chat_name}",
            f"发送时间: {ts_val}",
            f"发送者: {sender}",
        ]
        ocr = media.get("ocr") or {}
        ocr_text = ocr.get("text")
        if ocr_text and str(ocr_text).strip():
            context_lines.append(f"OCR识别参考文本:\n{ocr_text.strip()}")

        text_block = "\n".join(context_lines)

        content_list: List[Dict[str, Any]] = [
            {"type": "text", "text": text_block},
        ]

        if only == "video":
            rel_url = media.get("url") or f"/api/wechat/media/{aid}/video.mp4"
            abs_url = f"{base}{rel_url}" if base else rel_url
            duration_s = media.get("duration_s")
            dur_str = f"{duration_s}s" if duration_s is not None else "未知时长"

            poster_file = (
                locate_local_poster_file(media, image_dir, input_file)
                if not no_base64
                else None
            )

            if video_mode == "url":
                content_list.append({
                    "type": "text",
                    "text": (
                        f"视频链接：{abs_url}（时长 {dur_str}，需 Bearer 鉴权；"
                        "大文件不进行 base64 内联）"
                    ),
                })
            elif poster_file and poster_file.is_file():
                p_bytes = base64.b64encode(poster_file.read_bytes()).decode("ascii")
                p_fmt = poster_file.suffix.lstrip(".").lower()
                if p_fmt in ("jpg", ""):
                    p_fmt = "jpeg"
                img_data_url = f"data:image/{p_fmt};base64,{p_bytes}"

                content_list.append({
                    "type": "text",
                    "text": f"这是视频首帧（时长 {dur_str}），视频本体见 {abs_url}",
                })
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": img_data_url},
                })
                if video_mode == "both":
                    content_list.append({
                        "type": "text",
                        "text": f"视频链接：{abs_url}（需 Bearer 鉴权）",
                    })
            else:
                # Poster not found locally or poster is None
                content_list.append({
                    "type": "text",
                    "text": (
                        f"视频链接：{abs_url}（时长 {dur_str}，无首帧，需消费端自行拉取视频抽帧）"
                    ),
                })
        else:
            local_file = locate_local_media_file(media, image_dir, input_file)
            fmt = (media.get("format") or "jpeg").lower()
            if fmt == "jpg":
                fmt = "jpeg"
            if not no_base64 and local_file and local_file.is_file():
                b64_bytes = base64.b64encode(
                    local_file.read_bytes()
                ).decode("ascii")
                image_url_val = f"data:image/{fmt};base64,{b64_bytes}"
            else:
                rel_url = (
                    media.get("url")
                    or f"/api/wechat/media/{aid}/image.{fmt}"
                )
                abs_url = f"{base}{rel_url}" if base else rel_url
                image_url_val = abs_url

            content_list.append({
                "type": "image_url",
                "image_url": {"url": image_url_val},
            })
            if not image_url_val.startswith("data:"):
                content_list.append({
                    "type": "text",
                    "text": "提示：从 media_url_abs 获取原图需携带 Bearer Token 鉴权请求头。",
                })

        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": content_list,
                }
            ],
            "temperature": 0.0,
        }

        dumped = json.dumps(payload, ensure_ascii=False, indent=2)
        lines.append(f"\n--- Vision Payload #{idx} (asset_id={aid}) ---")
        lines.append(dumped)

    return "\n".join(lines)


def estimate_payload_bytes(data: Dict[str, Any]) -> str:
    """Compare payload sizes across media/ocr filter combinations."""
    # 1. media=none & ocr=none: drop media objects and empty OCR texts
    d_none = copy.deepcopy(data)
    for chat in d_none.get("chats", []):
        for msg in chat.get("messages", []):
            if "media" in msg:
                del msg["media"]
            if msg.get("kind") in ("image", "video"):
                msg["text"] = ""
                if not msg.get("note"):
                    msg["note"] = "图片" if msg.get("kind") == "image" else "视频"

    # 2. media=image (strip video objects)
    d_image = copy.deepcopy(data)
    for chat in d_image.get("chats", []):
        for msg in chat.get("messages", []):
            media = msg.get("media")
            if isinstance(media, dict) and media.get("kind") != "image":
                del msg["media"]
            elif msg.get("kind") == "video" and "media" in msg:
                del msg["media"]

    # 3. media=all (image + video present)
    d_all = copy.deepcopy(data)

    size_none = len(json.dumps(d_none, ensure_ascii=False).encode("utf-8"))
    size_img = len(json.dumps(d_image, ensure_ascii=False).encode("utf-8"))
    size_all = len(json.dumps(d_all, ensure_ascii=False).encode("utf-8"))

    def fmt_size(sz: int) -> str:
        if sz < 1024:
            return f"{sz} B"
        if sz < 1024 * 1024:
            return f"{sz / 1024:.2f} KB"
        return f"{sz / (1024 * 1024):.2f} MB"

    lines: List[str] = [
        "=== Payload Size Estimation Across Filter Modes ===",
        f"{'Mode Combination':<30} | {'Estimated JSON':<16} | {'Ratio':<8}",
        "-" * 60,
        f"{'media=none & ocr=none':<30} | {fmt_size(size_none):<16} | 1.0x",
        (
            f"{'media=image & ocr=head/full':<30} | "
            f"{fmt_size(size_img):<16} | "
            f"{size_img / (size_none or 1):.2f}x"
        ),
        (
            f"{'media=all (image + video)':<30} | "
            f"{fmt_size(size_all):<16} | "
            f"{size_all / (size_none or 1):.2f}x"
        ),
    ]
    return "\n".join(lines)


def run_dry_run(
    data: Dict[str, Any],
    webhook_type: str = "custom",
    limit: int = 3,
    chats_filter: Optional[List[str]] = None,
) -> str:
    """Execute --dry-run printing payloads truncated to 800 chars."""
    images = extract_media_messages(
        data, target_kind="image", chats_filter=chats_filter
    )
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
        "--public-base-url",
        default=os.environ.get("WECHAT_PUBLIC_BASE_URL", "").strip(),
        help="Base URL for constructing absolute media URLs",
    )
    parser.add_argument(
        "--input",
        dest="input_file",
        help="Path to local export JSON file (offline, bypasses network)",
    )
    parser.add_argument(
        "--policy",
        dest="policy_file",
        help="Path to media_policy.json (to detect OCR enabled/disabled)",
    )
    parser.add_argument(
        "--assume-ocr-disabled",
        action="store_true",
        help="Override: evaluate audit assuming OCR is disabled",
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
    parser.add_argument(
        "--chats",
        help="Comma-separated chat_ids to scope probe processing",
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
        "--vision",
        action="store_true",
        help="Generate OpenAI-compatible multimodal payloads for Muse",
    )
    mode_group.add_argument(
        "--estimate-bytes",
        action="store_true",
        help="Estimate payload byte sizes across filter combinations",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview probe payloads or run offline probe self-check",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.8,
        help="Minimum composite hit rate threshold to pass (default: 0.8)",
    )
    parser.add_argument(
        "--require-zero-hallucination",
        action="store_true",
        default=False,
        help="Fail (exit 1) if any hallucination is detected",
    )
    parser.add_argument(
        "--min-probes",
        type=int,
        default=12,
        help="Minimum probes required in --score suite (default: 12)",
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

    # Vision mode parameters
    parser.add_argument(
        "--only",
        choices=["image", "video"],
        default="image",
        help="Select media kind in vision payload (default: image)",
    )
    parser.add_argument(
        "--vision-model",
        default="gpt-4o",
        help="Vision model identifier in generated payload (default: gpt-4o)",
    )
    parser.add_argument(
        "--vision-limit",
        type=int,
        default=1,
        help="Number of vision payloads to generate (default: 1)",
    )
    parser.add_argument(
        "--no-base64",
        action="store_true",
        help="Output HTTP media_url_abs instead of data: base64 in vision",
    )
    parser.add_argument(
        "--video-mode",
        choices=["poster", "url", "both"],
        default="poster",
        help="Video delivery mode in vision: poster (default), url, both",
    )
    parser.add_argument(
        "--image-dir",
        help="Directory containing exported image files for base64 inlining",
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
    is_vision = args.vision
    is_estimate = args.estimate_bytes

    if is_score:
        if not args.expect_file:
            print(
                "[ERROR] --score requires --expect <probes.json>",
                file=sys.stderr,
            )
            return 1
        if not args.dry_run and not args.webhook_url:
            print("[ERROR] --score requires --webhook-url", file=sys.stderr)
            return 1

    if is_score and args.dry_run and not args.input_file:
        data = None
    else:
        data, err = load_dataset(args)
        if err or data is None:
            print(f"[ERROR] {err}", file=sys.stderr)
            return 1

    chats_filter = (
        [c.strip() for c in args.chats.split(",") if c.strip()]
        if args.chats
        else None
    )

    if is_estimate:
        table = estimate_payload_bytes(data)
        print(table)
        return 0

    if is_audit:
        ocr_enabled, ocr_source = resolve_ocr_enabled_policy(
            policy_path=args.policy_file,
            assume_disabled=args.assume_ocr_disabled,
            input_file=args.input_file,
        )
        passed, report = run_audit(
            data,
            ocr_enabled=ocr_enabled,
            ocr_source=ocr_source,
            chats_filter=chats_filter,
        )
        print(report)
        return 0 if passed else 1

    if is_score:
        passed, report = run_score(
            data=data,
            probes_path=args.expect_file,
            webhook_url=args.webhook_url,
            webhook_type=args.webhook_type,
            timeout=args.timeout,
            chats_filter=chats_filter,
            dry_run=args.dry_run,
            fail_under=args.fail_under,
            require_zero_hallucination=args.require_zero_hallucination,
            min_probes=args.min_probes if args.dry_run else 0,
        )
        print(report)
        return 0 if passed else 1

    if is_vision:
        preview = run_vision(
            data=data,
            vision_model=args.vision_model,
            limit=args.vision_limit,
            no_base64=args.no_base64,
            image_dir=args.image_dir,
            input_file=args.input_file,
            public_base_url=args.public_base_url,
            export_url=args.endpoint,
            only=args.only,
            chats_filter=chats_filter,
            video_mode=args.video_mode,
        )
        print(preview)
        return 0

    # Dry-run
    preview = run_dry_run(
        data,
        webhook_type=args.webhook_type,
        limit=args.limit,
        chats_filter=chats_filter,
    )
    print(preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
