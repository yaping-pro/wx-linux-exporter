from __future__ import annotations

import struct
from typing import Callable

_PAGE_SIZE = 4096
_FRAME_HDR = 24
_MAGIC_LE = 0x377f0682   # 小端校验和
_MAGIC_BE = 0x377f0683   # 大端校验和


class WalFormatError(ValueError):
    pass


def _checksum(data: bytes, endian: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    a, b = state
    for x, y in struct.iter_unpack(endian + "II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def parse_committed_frames(wal: bytes, page_size: int = _PAGE_SIZE):
    """返回 (frames, final_pages)。frames = [(pgno, raw_page_bytes), ...] 仅含已提交帧。

    wal 为空 / 头部无效 / 校验失败 / 帧校验失败 -> 抛 WalFormatError，调用方回退。
    """
    if not wal:
        return [], None
    if len(wal) < 32:
        raise WalFormatError("WAL 头部不完整")
    magic, version, wal_page_size = struct.unpack(">III", wal[:12])
    if magic not in (_MAGIC_LE, _MAGIC_BE) or version != 3007000:
        raise WalFormatError("WAL 魔数或版本无效")
    if wal_page_size != page_size:
        raise WalFormatError("WAL 页大小不匹配")
    endian = "<" if magic == _MAGIC_LE else ">"
    state = _checksum(wal[:24], endian)
    if state != struct.unpack(">II", wal[24:32]):
        raise WalFormatError("WAL 头部校验失败")

    salt = wal[16:24]
    frame_size = _FRAME_HDR + page_size
    pending: dict[int, bytes] = {}
    committed: dict[int, bytes] = {}
    final_pages = None
    offset = 32
    while offset + frame_size <= len(wal):
        header = wal[offset:offset + _FRAME_HDR]
        if header[8:16] != salt:
            break                                   # 上一世代残留 / 零填充尾部
        pgno, db_size = struct.unpack(">II", header[:8])
        if pgno == 0 or pgno > 0xFFFFFFFE:
            raise WalFormatError(f"WAL 页号无效: {pgno}")
        page = wal[offset + _FRAME_HDR:offset + frame_size]
        state = _checksum(page, endian, _checksum(header[:8], endian, state))
        if state != struct.unpack(">II", header[16:24]):
            raise WalFormatError("WAL 帧校验失败")
        pending[pgno] = page
        if db_size:
            committed.update(pending)
            pending.clear()
            committed = {n: p for n, p in committed.items() if n <= db_size}
            final_pages = db_size
        offset += frame_size
    return sorted(committed.items()), final_pages


def merge_into(db_bytes: bytes, wal: bytes,
               decrypt_page: Callable[[bytes, int], bytes] | None = None,
               page_size: int = _PAGE_SIZE) -> tuple[bytes, dict]:
    """把已提交 WAL 帧叠加到明文库字节上。decrypt_page=None 表示库已是明文。

    返回 (merged_bytes, info)。info = {frames, patched_pages, final_pages}。
    """
    frames, final_pages = parse_committed_frames(wal, page_size)
    out = bytearray(db_bytes)
    for pgno, raw in frames:
        plain = decrypt_page(raw, pgno) if decrypt_page else raw
        if len(plain) != page_size:
            raise WalFormatError(f"解密后页长度异常: {len(plain)}")
        end = pgno * page_size
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[(pgno - 1) * page_size:end] = plain
    if final_pages is not None:
        want = final_pages * page_size
        if len(out) > want:
            del out[want:]
        elif len(out) < want:
            out.extend(b"\x00" * (want - len(out)))
        if len(out) >= 32:
            out[28:32] = struct.pack(">I", final_pages)
    return bytes(out), {"frames": len(frames), "patched_pages": len(frames), "final_pages": final_pages}
