"""wx_contract: WeChat export data contract definitions.

Pure functions, zero IO, no third-party dependencies.
"""
from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "1.3.0"

KIND_PREFIX = {
    "image": "图片",
    "video": "视频",
}


def media_note(asset: dict[str, Any] | None, kind: str = "image") -> str:
    """根据 ledger 中资产的 state 与 deny_reason 映射细化 note（图片/视频共用）。"""
    prefix = KIND_PREFIX.get(kind, "视频" if kind == "video" else "图片")
    if not asset:
        return prefix
    state = asset.get("state")
    reason = str(asset.get("deny_reason") or "")

    if state == "ready":
        return prefix
    elif state == "pending" or reason == "image_not_on_disk":
        return f"{prefix}（处理中）"
    elif state == "skipped":
        if reason == "missing_source":
            return f"{prefix}（未下载）"
        elif reason == "skipped:animated":
            return f"{prefix}（动图）"
        elif reason in ("max_pixels_exceeded", "oversize"):
            return f"{prefix}（超大）"
        elif reason in (
            "missing_keys",
            "skipped:decryptor_unavailable",
            "encrypted_video_unverified",
        ):
            return f"{prefix}（不可解密）"
        elif reason.startswith("unresolved:"):
            return f"{prefix}（无法定位）"
        return prefix
    elif state == "failed":
        return f"{prefix}（解析失败）"
    return prefix


def ocr_projection(asset: dict[str, Any], max_chars: int = 2000) -> str:
    """计算 OCR 的 text 投影（<=max_chars 字符，超出截断并附加 '…'）。"""
    th = asset.get("text_head")
    ocr_raw = asset.get("ocr") or {}
    if th is None and ocr_raw.get("status") == "ready" and ocr_raw.get("text"):
        raw_t = str(ocr_raw["text"]).strip()
        th = (raw_t[:max_chars] + "…") if len(raw_t) > max_chars else raw_t
    return str(th or "")


def build_media_object(
    asset: dict[str, Any],
    kind: str,
    anchor: dict[str, Any],
    ocr_mode: str = "head",
) -> dict[str, Any]:
    """产出导出侧 media 对象。

    - image: asset_id/kind/quality/format/width/height/bytes/path/url/ocr
    - video: asset_id/kind/url/poster/duration_s/format/bytes/width/height
    与现有输出逐字段一致。
    """
    if kind == "image":
        ocr_raw = dict(asset.get("ocr") or {})
        ocr_data: dict[str, Any] = {
            "status": ocr_raw.get("status", "skipped"),
            "text": ocr_raw.get("text", ""),
            "provider": ocr_raw.get("provider", "none"),
            "confidence": ocr_raw.get("confidence"),
        }
        if "lang" in ocr_raw:
            ocr_data["lang"] = ocr_raw["lang"]
        if "version" in ocr_raw:
            ocr_data["version"] = ocr_raw["version"]
        if "sha256" in ocr_raw:
            ocr_data["sha256"] = ocr_raw["sha256"]

        aid = asset["asset_id"]
        fmt = asset.get("format", "jpg")
        media_obj: dict[str, Any] = {
            "asset_id": aid,
            "kind": "image",
            "quality": asset.get("quality", "full"),
            "format": fmt,
            "width": asset.get("width"),
            "height": asset.get("height"),
            "bytes": int(asset.get("bytes") or 0),
            "path": asset.get("path") or f"media/{aid}/image.{fmt}",
            "url": asset.get("url") or f"/api/wechat/media/{aid}/image.{fmt}",
            "anchor": anchor,
            "source": asset.get("source") or {"dat": "", "variant": ""},
            "ocr": ocr_data,
        }
        if ocr_mode == "none":
            media_obj.pop("ocr", None)
        elif ocr_mode == "full":
            pass
        else:  # head
            ocr_data.pop("text", None)
        return media_obj

    elif kind == "video":
        aid = asset.get("md5") or asset.get("asset_id") or ""
        fmt = asset.get("format", "mp4")
        poster = (
            asset.get("poster")
            or (
                f"/api/wechat/media/{aid}/thumb.jpg"
                if asset.get("thumb")
                else None
            )
        )
        media_obj = {
            "asset_id": aid,
            "kind": "video",
            "url": asset.get("url") or f"/api/wechat/media/{aid}/video.{fmt}",
            "poster": poster,
            "duration_s": asset.get("duration_s"),
            "format": fmt,
            "bytes": int(asset.get("bytes") or 0),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "anchor": anchor,
            "source": asset.get("source") or "probe",
        }
        if asset.get("path"):
            media_obj["path"] = asset["path"]
        return media_obj

    return {}
