#!/usr/bin/env python3
"""MinerU 客户端（Linux 家服专用）——双通道。

  * agent 通道：免 token，≤10MB / ≤20 页，仅 pdf/docx/pptx/xlsx + 图片
  * precise 通道：需 token，≤200MB / ≤200 页，支持 html（MinerU-HTML）/doc/xls/ppt 等

凭据（零明文落盘）：按序解析 —— `--token-file` → `$CREDENTIALS_DIRECTORY/mineru-token`
（systemd LoadCredentialEncrypted）→ 环境变量 `MINERU_API_TOKEN`。
token 绝不进入 argv，绝不写日志。

与 macOS 版 mineru_doc_to_md 的关系：那边交互使用、走 Keychain；本文件服务家服自动化，
因此不复用其凭据层（/usr/bin/security 在 Linux 不存在）。

退出码：
  0 成功 | 2 超大小 | 3 超页数 | 4 网络/上传失败（可重试）
  5 解析失败 | 6 用法错误 | 7 格式不受支持 | 8 限流或额度耗尽（可退避）
  9 缺少凭据（需要 precise 但未配置 token）

stdout 末行输出 JSON 结果，供调用方解析。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API_BASE = "https://mineru.net"
AGENT_CREATE_URL = f"{API_BASE}/api/v1/agent/parse/file"
AGENT_QUERY_URL = f"{API_BASE}/api/v1/agent/parse/{{task_id}}"
PRECISE_BATCH_URL = f"{API_BASE}/api/v4/file-urls/batch"
PRECISE_RESULT_URL = f"{API_BASE}/api/v4/extract-results/batch/{{batch_id}}"

AGENT_MAX_BYTES = 10 * 1024 * 1024
PRECISE_MAX_BYTES = 200 * 1024 * 1024
AGENT_SUPPORTED_EXT = {"pdf", "docx", "pptx", "xlsx",
                       "png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}
# html 必须走 MinerU-HTML 专用模型，否则 precise 也会拒
HTML_EXT = {"html", "htm"}

EXIT_OK = 0
EXIT_OVERSIZE = 2
EXIT_PAGE_LIMIT = 3
EXIT_NETWORK = 4
EXIT_PARSE_FAILED = 5
EXIT_USAGE = 6
EXIT_UNSUPPORTED = 7
EXIT_THROTTLED = 8
EXIT_NO_CREDENTIAL = 9

RETRYABLE_CODES = {"-60018", "429"}
UNSUPPORTED_CODES = {"-30002", "-60002"}
PAGE_LIMIT_CODES = {"-30003", "-60006"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


# ── 凭据 ─────────────────────────────────────────────────────────────────────
def resolve_token(token_file: str | None) -> str | None:
    if token_file:
        p = Path(token_file).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8").strip() or None
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        for name in ("mineru-token", "mineru_token", "mineru"):
            p = Path(cred_dir) / name
            if p.is_file():
                return p.read_text(encoding="utf-8").strip() or None
    return os.environ.get("MINERU_API_TOKEN", "").strip() or None


def auth_headers(token: str | None) -> dict:
    h = {"Accept": "*/*"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── HTTP ─────────────────────────────────────────────────────────────────────
def request_json(url: str, method: str = "GET", headers: dict | None = None,
                 data: dict | None = None, timeout: int = 60) -> dict:
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload(url: str, src: Path) -> None:
    """curl -T 纯净流式 PUT。URL 经 --config - 从 stdin 注入，不进 argv。"""
    if '"' in url:
        raise RuntimeError("预签名 URL 含非法字符")
    cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
           "--retry", "3", "--retry-connrefused",
           "--connect-timeout", "30", "--max-time", "1800",
           "-K", "-", "-X", "PUT", "-T", str(src)]
    p = subprocess.run(cmd, input=f'url = "{url}"\n', capture_output=True, text=True)
    code = (p.stdout or "").strip()
    if p.returncode != 0 or code != "200":
        raise RuntimeError(f"上传失败 HTTP {code or '无响应'} {p.stderr.strip()}")


def download(url: str, dest: Path, timeout: int = 600) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh, length=1 << 20)


def safe_extract(zip_path: Path, dest: Path) -> None:
    """防御 Zip Slip：成员路径必须落在 dest 之内。"""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise RuntimeError(f"压缩包含非法路径：{name}")
        zf.extractall(dest)


def classify(msg: str) -> int:
    for code in PAGE_LIMIT_CODES:
        if code in msg:
            return EXIT_PAGE_LIMIT
    for code in UNSUPPORTED_CODES:
        if code in msg:
            return EXIT_UNSUPPORTED
    if "not supported by the lightweight API" in msg:
        return EXIT_UNSUPPORTED
    if "page count exceeds" in msg:
        return EXIT_PAGE_LIMIT
    for code in RETRYABLE_CODES:
        if code in msg:
            return EXIT_THROTTLED
    return EXIT_PARSE_FAILED


# ── agent 通道（免 token）────────────────────────────────────────────────────
def parse_agent(src: Path, out_dir: Path, name: str, args) -> int:
    payload: dict = {"file_name": name}
    if args.lang:
        payload["language"] = args.lang
    payload["enable_table"] = not args.no_table
    payload["enable_formula"] = not args.no_formula
    if args.ocr:
        payload["is_ocr"] = True
    if args.pages:
        payload["page_range"] = args.pages

    try:
        res = request_json(AGENT_CREATE_URL, method="POST", data=payload)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            emit({"state": "failed", "deny_reason": "throttled"})
            return EXIT_THROTTLED
        emit({"state": "failed", "deny_reason": "network", "detail": f"HTTP {e.code}"})
        return EXIT_NETWORK
    except Exception as e:  # noqa: BLE001
        emit({"state": "failed", "deny_reason": "network", "detail": str(e)[:200]})
        return EXIT_NETWORK

    if res.get("code") != 0:
        msg = str(res.get("msg") or res.get("code"))
        rc = classify(msg)
        emit({"state": "failed", "deny_reason": f"mineru:{rc}", "detail": msg[:200]})
        return rc

    data = res.get("data") or {}
    task_id, file_url = data.get("task_id"), data.get("file_url")
    if not task_id or not file_url:
        emit({"state": "failed", "deny_reason": "bad_response"})
        return EXIT_NETWORK

    try:
        upload(file_url, src)
    except Exception as e:  # noqa: BLE001
        emit({"state": "failed", "deny_reason": "upload", "detail": str(e)[:200]})
        return EXIT_NETWORK

    deadline = time.time() + args.timeout
    while True:
        if time.time() > deadline:
            emit({"state": "failed", "deny_reason": "timeout", "task_id": task_id})
            return EXIT_NETWORK
        try:
            q = request_json(AGENT_QUERY_URL.format(task_id=task_id))
        except Exception as e:  # noqa: BLE001
            log(f"轮询异常（重试）：{e}")
            time.sleep(5)
            continue
        d = q.get("data") or {}
        state = d.get("state")
        if state == "done":
            md_url = d.get("markdown_url")
            break
        if state == "failed":
            msg = str(d.get("err_msg") or d.get("err_code") or "")
            rc = classify(msg)
            emit({"state": "failed", "deny_reason": f"mineru:{rc}", "detail": msg[:200]})
            return rc
        time.sleep(5)

    if not md_url:
        emit({"state": "failed", "deny_reason": "no_markdown_url"})
        return EXIT_PARSE_FAILED
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "full.md"
    try:
        download(md_url, md_path)
    except Exception as e:  # noqa: BLE001
        emit({"state": "failed", "deny_reason": "download", "detail": str(e)[:200]})
        return EXIT_NETWORK
    emit({"state": "ready", "md_path": str(md_path),
          "chars": len(md_path.read_text(encoding="utf-8", errors="replace")),
          "channel": "agent", "task_id": task_id})
    return EXIT_OK


# ── precise 通道（需 token）──────────────────────────────────────────────────
def parse_precise(src: Path, out_dir: Path, name: str, args, token: str) -> int:
    ext = src.suffix.lower().lstrip(".")
    model = "MinerU-HTML" if ext in HTML_EXT else (args.model or "vlm")

    entry: dict = {"name": name, "data_id": "0"}
    if args.pages and ext == "pdf":
        entry["page_ranges"] = args.pages
    payload: dict = {"files": [entry], "model_version": model}
    if model != "MinerU-HTML":
        payload["language"] = args.lang
        payload["enable_table"] = not args.no_table
        payload["enable_formula"] = not args.no_formula
        if args.extra_formats:
            extra = [f.strip() for f in args.extra_formats.split(",") if f.strip()]
            payload["extra_formats"] = extra

    try:
        res = request_json(PRECISE_BATCH_URL, method="POST",
                           headers=auth_headers(token), data=payload)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            emit({"state": "failed", "deny_reason": "bad_token", "detail": f"HTTP {e.code}"})
            return EXIT_NO_CREDENTIAL
        emit({"state": "failed", "deny_reason": "network", "detail": f"HTTP {e.code}"})
        return EXIT_NETWORK
    except Exception as e:  # noqa: BLE001
        emit({"state": "failed", "deny_reason": "network", "detail": str(e)[:200]})
        return EXIT_NETWORK

    if res.get("code") != 0:
        msg = str(res.get("msg") or res.get("code"))
        rc = classify(msg)
        emit({"state": "failed", "deny_reason": f"mineru:{rc}", "detail": msg[:200]})
        return rc

    data = res.get("data") or {}
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        emit({"state": "failed", "deny_reason": "bad_response"})
        return EXIT_NETWORK

    try:
        upload(urls[0], src)
    except Exception as e:  # noqa: BLE001
        emit({"state": "failed", "deny_reason": "upload", "detail": str(e)[:200]})
        return EXIT_NETWORK

    deadline = time.time() + args.timeout
    zip_url = None
    while True:
        if time.time() > deadline:
            emit({"state": "failed", "deny_reason": "timeout", "batch_id": batch_id})
            return EXIT_NETWORK
        try:
            q = request_json(PRECISE_RESULT_URL.format(batch_id=batch_id),
                             headers=auth_headers(token))
        except Exception as e:  # noqa: BLE001
            log(f"轮询异常（重试）：{e}")
            time.sleep(5)
            continue
        results = (q.get("data") or {}).get("extract_result") or []
        if isinstance(results, dict):
            results = [results]
        for item in results:
            st = item.get("state")
            if st == "done":
                zip_url = item.get("full_zip_url")
            elif st == "failed":
                msg = str(item.get("err_msg") or "")
                rc = classify(msg)
                emit({"state": "failed", "deny_reason": f"mineru:{rc}", "detail": msg[:200]})
                return rc
        if zip_url:
            break
        time.sleep(5)

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "r.zip"
        try:
            download(zip_url, zp)
            ex = Path(tmp) / "x"
            safe_extract(zp, ex)
        except Exception as e:  # noqa: BLE001
            emit({"state": "failed", "deny_reason": "download", "detail": str(e)[:200]})
            return EXIT_NETWORK
        root = ex
        entries = list(root.iterdir())
        if len(entries) == 1 and entries[0].is_dir() and not (root / "full.md").exists():
            root = entries[0]
        if not (root / "full.md").is_file():
            emit({"state": "failed", "deny_reason": "no_full_md"})
            return EXIT_PARSE_FAILED
        for item in root.iterdir():
            dst = out_dir / item.name
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.move(str(item), str(out_dir))

    md_path = out_dir / "full.md"
    emit({"state": "ready", "md_path": str(md_path),
          "chars": len(md_path.read_text(encoding="utf-8", errors="replace")),
          "channel": "precise", "model": model, "batch_id": batch_id})
    return EXIT_OK


# ── 入口 ─────────────────────────────────────────────────────────────────────
def choose_mode(args, ext: str, size: int, token: str | None) -> tuple[str, int]:
    """返回 (mode, exit_code_if_impossible)。"""
    if args.mode != "auto":
        if args.mode == "precise" and not token:
            return "precise", EXIT_NO_CREDENTIAL
        return args.mode, EXIT_OK
    if ext in AGENT_SUPPORTED_EXT and size <= AGENT_MAX_BYTES:
        return "agent", EXIT_OK
    if token:
        return "precise", EXIT_OK
    # 无凭据且 agent 覆盖不到
    return "agent", (EXIT_UNSUPPORTED if ext not in AGENT_SUPPORTED_EXT else EXIT_OVERSIZE)


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 客户端（agent 免 token / precise 需 token）")
    ap.add_argument("path")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--name")
    ap.add_argument("--mode", choices=["auto", "agent", "precise"], default="auto")
    ap.add_argument("--model", default="vlm", help="precise 模型：vlm / pipeline（html 自动用 MinerU-HTML）")
    ap.add_argument("--pages", help="页码范围，如 1-20")
    ap.add_argument("--extra-formats", help="precise 附加导出：docx,html,latex")
    ap.add_argument("--token-file", help="token 文件路径（默认取 $CREDENTIALS_DIRECTORY/mineru-token）")
    ap.add_argument("-l", "--lang", default="ch")
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--no-table", action="store_true")
    ap.add_argument("--no-formula", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    src = Path(args.path).expanduser().resolve()
    if not src.is_file():
        log(f"输入不存在：{src}")
        return EXIT_USAGE

    ext = src.suffix.lower().lstrip(".")
    size = src.stat().st_size
    name = args.name or src.name
    out_dir = Path(args.out).expanduser().resolve()
    token = resolve_token(args.token_file)

    mode, impossible = choose_mode(args, ext, size, token)
    if impossible != EXIT_OK:
        reason = ("agent_unsupported" if impossible == EXIT_UNSUPPORTED
                  else "oversize" if impossible == EXIT_OVERSIZE
                  else "no_credential")
        emit({"state": "skipped", "deny_reason": reason, "ext": ext, "bytes": size})
        return impossible

    limit = AGENT_MAX_BYTES if mode == "agent" else PRECISE_MAX_BYTES
    if size > limit:
        emit({"state": "skipped", "deny_reason": "oversize",
              "bytes": size, "limit_bytes": limit, "mode": mode})
        return EXIT_OVERSIZE

    started = time.time()
    if mode == "agent":
        rc = parse_agent(src, out_dir, name, args)
    else:
        rc = parse_precise(src, out_dir, name, args, token or "")
    log(f"[{mode}] {name} rc={rc} elapsed={time.time() - started:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
