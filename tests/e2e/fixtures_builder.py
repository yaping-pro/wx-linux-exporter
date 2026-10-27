"""Synthetic WeChat 4.x vault fixture builder for E2E testing.

Constructs a minimal, offline, synthetic WeChat 4.x decrypted vault snapshot
without any real user privacy data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time
from typing import NamedTuple

from Crypto.Cipher import AES

SYNTH_CHAT = "synth_chat_001"
BLOCKED_CHAT = "blocked_chat_001"
SYNTH_SENDER = "synth_sender_01"

IMAGE_ASSET_ID = "11223344556677889900aabbccddeeff"
VIDEO_ASSET_ID = "aabbccddeeff00112233445566778899"
BLOCKED_ASSET_ID = "badbeef0000000000000000000000000"
FILE_ASSET_ID = "3344556677889900aabbccddeeff0011"

AES_KEY = b"0123456789abcdef"
XOR_KEY = 0x0C
WXID = "wxid_synth_test_1a2b"
TOKEN = "test-e2e-token-secret-0123456789"


def create_synthetic_mp4(width: int = 1920, height: int = 1080, duration_s: int = 12) -> bytes:
    """Build a minimal valid MP4 container with ftyp, mvhd, and tkhd boxes."""
    ftyp_data = b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41"
    ftyp = struct.pack(">I", 8 + len(ftyp_data)) + b"ftyp" + ftyp_data

    timescale = 1000
    dur = duration_s * timescale
    mvhd_payload = (
        b"\x00"  # version
        + b"\x00\x00\x00"  # flags
        + struct.pack(">II", 0, 0)
        + struct.pack(">II", timescale, dur)
        + b"\x00\x01\x00\x00"  # rate 1.0
        + b"\x01\x00"  # volume 1.0
        + b"\x00" * 10  # reserved
        + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00"
        + b"\x00" * 24
        + struct.pack(">I", 2)
    )
    mvhd = struct.pack(">I", 8 + len(mvhd_payload)) + b"mvhd" + mvhd_payload

    tkhd_payload = (
        b"\x00"
        + b"\x00\x00\x07"
        + struct.pack(">II", 0, 0)
        + struct.pack(">I", 1)
        + b"\x00" * 4
        + struct.pack(">I", dur)
        + b"\x00" * 8
        + b"\x00\x00"
        + b"\x00\x00"
        + b"\x00\x00"
        + b"\x00\x00"
        + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00"
        + struct.pack(">II", width << 16, height << 16)
    )
    tkhd = struct.pack(">I", 8 + len(tkhd_payload)) + b"tkhd" + tkhd_payload

    trak = struct.pack(">I", 8 + len(tkhd)) + b"trak" + tkhd
    moov_payload = mvhd + trak
    moov = struct.pack(">I", 8 + len(moov_payload)) + b"moov" + moov_payload

    mdat_payload = b"\x00" * 64
    mdat = struct.pack(">I", 8 + len(mdat_payload)) + b"mdat" + mdat_payload

    return ftyp + moov + mdat


def make_synthetic_v2(
    plaintext: bytes,
    aes_key: bytes,
    xor_key: int,
    aes_size: int = 16,
) -> bytes:
    """Create a synthetic V2 container wrapping plaintext."""
    magic = b"\x07\x08\x56\x32\x08\x07"
    eff_aes_size = min(len(plaintext), aes_size)
    aligned = eff_aes_size - (~(~eff_aes_size % 16))
    aes_plain = plaintext[:eff_aes_size]
    pad_len = 16 - (len(aes_plain) % 16)
    padded = aes_plain + bytes([pad_len] * pad_len)
    if len(padded) < aligned:
        padded = padded + bytes([aligned - len(padded)] * (aligned - len(padded)))
    cipher = AES.new(aes_key, AES.MODE_ECB)
    enc_aes = cipher.encrypt(padded[:aligned])

    remaining = plaintext[len(aes_plain):]
    if remaining:
        xor_size = min(1024 * 1024, len(remaining))
        raw_size = len(remaining) - xor_size
        raw_part = remaining[:raw_size]
        tail = remaining[raw_size:]
        trans = bytes.maketrans(
            bytes(range(256)),
            bytes(b ^ xor_key for b in range(256)),
        )
        enc_tail = tail.translate(trans)
    else:
        xor_size = 0
        raw_part = b""
        enc_tail = b""

    header = (
        magic
        + struct.pack("<I", eff_aes_size)
        + struct.pack("<I", xor_size)
        + b"\x01"
    )
    return header + enc_aes + raw_part + enc_tail


class SyntheticVaultInfo(NamedTuple):
    base_dir: Path
    vault_dir: Path
    decrypted_dir: Path
    account_dir: Path
    policy_path: Path
    ledger_path: Path
    base_time: int
    image_asset_id: str
    video_asset_id: str
    token: str


def build_synthetic_vault(base_dir: Path, include_packed_info_data: bool = True) -> SyntheticVaultInfo:
    """Build a complete offline synthetic WeChat 4.x vault."""
    vault_dir = base_dir / "vault"
    decrypted_dir = vault_dir / "decrypted" / "current"
    account_dir = base_dir / "account"
    config_dir = vault_dir / "config"
    state_dir = vault_dir / "state"
    media_dir = vault_dir / "media"
    exports_dir = vault_dir / "exports"

    for d in (
        decrypted_dir / "contact",
        decrypted_dir / "message",
        decrypted_dir / "hardlink",
        decrypted_dir / "session",
        config_dir,
        state_dir,
        media_dir,
        exports_dir,
        account_dir / "msg" / "attach",
        account_dir / "msg" / "video" / "2026-10",
        account_dir / "msg" / "file" / "2026-10",
        account_dir / "cache",
    ):
        d.mkdir(parents=True, exist_ok=True)

    base_time = int(time.time()) - 300

    # 1. decrypted/current/contact/contact.db
    c_con = sqlite3.connect(decrypted_dir / "contact" / "contact.db")
    c_con.execute(
        "CREATE TABLE contact ("
        "id INTEGER PRIMARY KEY, "
        "username TEXT, "
        "remark TEXT, "
        "nick_name TEXT, "
        "alias TEXT, "
        "local_type INTEGER, "
        "chat_room_type INTEGER"
        ")"
    )
    c_con.execute(
        "INSERT INTO contact VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, SYNTH_CHAT, "合成测试群", "Synthetic Test Chat", "synth_chat", 1, 0),
    )
    c_con.execute(
        "INSERT INTO contact VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, BLOCKED_CHAT, "机密屏蔽群", "Confidential Blocked Chat", "blocked_chat", 1, 0),
    )
    c_con.execute(
        "INSERT INTO contact VALUES (?, ?, ?, ?, ?, ?, ?)",
        (3, SYNTH_SENDER, "张三", "Zhang San", "zhangsan", 1, 0),
    )
    c_con.commit()
    c_con.close()

    # session.db for fallback chat_name resolution
    s_con = sqlite3.connect(decrypted_dir / "session" / "session.db")
    s_con.execute(
        "CREATE TABLE SessionTable ("
        "username TEXT PRIMARY KEY, "
        "display_name TEXT, "
        "last_timestamp INTEGER, "
        "unread_count INTEGER"
        ")"
    )
    s_con.execute(
        "INSERT INTO SessionTable VALUES (?, ?, ?, ?)",
        (SYNTH_CHAT, "合成测试群", base_time, 0),
    )
    s_con.execute(
        "INSERT INTO SessionTable VALUES (?, ?, ?, ?)",
        (BLOCKED_CHAT, "机密屏蔽群", base_time, 0),
    )
    s_con.commit()
    s_con.close()

    # 2. decrypted/current/hardlink/hardlink.db
    h_con = sqlite3.connect(decrypted_dir / "hardlink" / "hardlink.db")
    h_con.execute("CREATE TABLE dir2id (rowid INTEGER PRIMARY KEY, username TEXT)")
    h_con.execute("INSERT INTO dir2id VALUES (1, '2026-10')")

    h_con.execute(
        "CREATE TABLE file_hardlink_info_v4 ("
        "md5 TEXT PRIMARY KEY, "
        "file_name TEXT, "
        "dir1 INTEGER, "
        "dir2 INTEGER, "
        "file_size INTEGER"
        ")"
    )
    h_con.execute(
        "INSERT INTO file_hardlink_info_v4 VALUES (?, ?, 1, 0, 1024)",
        (FILE_ASSET_ID, "synthetic_doc.pdf"),
    )

    h_con.execute(
        "CREATE TABLE image_hardlink_info_v4 ("
        "md5 TEXT PRIMARY KEY, "
        "file_name TEXT, "
        "dir1 INTEGER, "
        "dir2 INTEGER"
        ")"
    )
    h_con.execute(
        "INSERT INTO image_hardlink_info_v4 VALUES (?, ?, 1, 0)",
        (IMAGE_ASSET_ID, f"{IMAGE_ASSET_ID}.dat"),
    )
    h_con.execute(
        "INSERT INTO image_hardlink_info_v4 VALUES (?, ?, 1, 0)",
        (BLOCKED_ASSET_ID, f"{BLOCKED_ASSET_ID}.dat"),
    )

    h_con.execute(
        "CREATE TABLE video_hardlink_info_v4 ("
        "md5 TEXT PRIMARY KEY, "
        "file_name TEXT, "
        "dir1 INTEGER, "
        "dir2 INTEGER"
        ")"
    )
    # Populate a secondary entry to verify video_hardlink_info_v4 schema while allowing
    # probe discovery of VIDEO_ASSET_ID to resolve both MP4 and native thumbnail
    h_con.execute(
        "INSERT INTO video_hardlink_info_v4 VALUES (?, ?, 1, 0)",
        ("99887766554433221100ffeeddccbbaa", "secondary_video.mp4"),
    )
    h_con.commit()
    h_con.close()

    # 3. Media files on disk
    ocr_sample_path = Path(__file__).resolve().parents[1] / "fixtures" / "ocr_sample.png"
    if ocr_sample_path.is_file():
        img_bytes = ocr_sample_path.read_bytes()
    else:
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    chat_hash = hashlib.md5(SYNTH_CHAT.encode("utf-8")).hexdigest()
    img_dir = account_dir / "msg" / "attach" / chat_hash / "2026-10" / "Img"
    img_dir.mkdir(parents=True, exist_ok=True)

    # Image .dat in V2 format
    img_dat_bytes = make_synthetic_v2(img_bytes, AES_KEY, XOR_KEY)
    (img_dir / f"{IMAGE_ASSET_ID}.dat").write_bytes(img_dat_bytes)

    # Video thumbnail .dat in V2 format
    thumb_dat_bytes = make_synthetic_v2(img_bytes, AES_KEY, XOR_KEY)
    (img_dir / f"{VIDEO_ASSET_ID}_t.dat").write_bytes(thumb_dat_bytes)

    # Video MP4 file
    mp4_bytes = create_synthetic_mp4(width=1920, height=1080, duration_s=12)
    (account_dir / "msg" / "video" / "2026-10" / f"{VIDEO_ASSET_ID}.mp4").write_bytes(mp4_bytes)

    # Blocked chat image and media file
    blocked_hash = hashlib.md5(BLOCKED_CHAT.encode("utf-8")).hexdigest()
    blocked_img_dir = account_dir / "msg" / "attach" / blocked_hash / "2026-10" / "Img"
    blocked_img_dir.mkdir(parents=True, exist_ok=True)
    (blocked_img_dir / f"{BLOCKED_ASSET_ID}.dat").write_bytes(img_dat_bytes)

    # Pre-populate vault/media for blocked chat asset so anti-leak test can verify access denial
    blocked_media_dir = media_dir / BLOCKED_ASSET_ID
    blocked_media_dir.mkdir(parents=True, exist_ok=True)
    (blocked_media_dir / "image.png").write_bytes(img_bytes)

    # 4. decrypted/current/message/message_0.db
    m_con = sqlite3.connect(decrypted_dir / "message" / "message_0.db")
    m_con.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    m_con.execute("INSERT INTO Name2Id VALUES (1, ?)", (SYNTH_CHAT,))
    m_con.execute("INSERT INTO Name2Id VALUES (2, ?)", (BLOCKED_CHAT,))
    m_con.execute("INSERT INTO Name2Id VALUES (3, ?)", (SYNTH_SENDER,))

    tbl_name = f"Msg_{chat_hash}"
    if include_packed_info_data:
        m_con.execute(
            f"CREATE TABLE [{tbl_name}] ("
            "local_id INTEGER PRIMARY KEY, "
            "server_id INTEGER, "
            "local_type INTEGER, "
            "real_sender_id INTEGER, "
            "create_time INTEGER, "
            "message_content TEXT, "
            "compress_content BLOB, "
            "packed_info_data BLOB"
            ")"
        )
    else:
        m_con.execute(
            f"CREATE TABLE [{tbl_name}] ("
            "local_id INTEGER PRIMARY KEY, "
            "server_id INTEGER, "
            "local_type INTEGER, "
            "real_sender_id INTEGER, "
            "create_time INTEGER, "
            "message_content TEXT, "
            "compress_content BLOB"
            ")"
        )

    # Insert 5 synthetic messages into synth_chat:
    # 1: Text
    # 2: Image (3)
    # 3: Video (43)
    # 4: Voice (34)
    # 5: File (49)
    img_xml = f'<msg><img md5="{IMAGE_ASSET_ID}" length="{len(img_bytes)}" /></msg>'
    video_xml = f'<msg><videomsg md5="{VIDEO_ASSET_ID}" length="{len(mp4_bytes)}" playlength="12" /></msg>'
    file_xml = (
        f'<msg><appmsg><title>synthetic_doc.pdf</title><fileext>pdf</fileext>'
        f'<totallen>1024</totallen><type>6</type><md5>{FILE_ASSET_ID}</md5></appmsg></msg>'
    )

    rows = [
        (1, 10001, 1, 3, base_time + 10, "Hello, synthetic WeChat world!", None),
        (2, 10002, 3, 3, base_time + 20, img_xml, None),
        (3, 10003, 43, 3, base_time + 30, video_xml, None),
        (4, 10004, 34, 3, base_time + 40, "", None),
        (5, 10005, 49, 3, base_time + 50, file_xml, None),
    ]

    for row in rows:
        if include_packed_info_data:
            m_con.execute(
                f"INSERT INTO [{tbl_name}] VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                row,
            )
        else:
            m_con.execute(
                f"INSERT INTO [{tbl_name}] VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    # Insert message into blocked chat table
    blocked_tbl = f"Msg_{blocked_hash}"
    if include_packed_info_data:
        m_con.execute(
            f"CREATE TABLE [{blocked_tbl}] ("
            "local_id INTEGER PRIMARY KEY, "
            "server_id INTEGER, "
            "local_type INTEGER, "
            "real_sender_id INTEGER, "
            "create_time INTEGER, "
            "message_content TEXT, "
            "compress_content BLOB, "
            "packed_info_data BLOB"
            ")"
        )
        m_con.execute(
            f"INSERT INTO [{blocked_tbl}] VALUES (1, 20001, 3, 3, ?, ?, NULL, NULL)",
            (base_time + 25, f'<msg><img md5="{BLOCKED_ASSET_ID}" length="{len(img_bytes)}" /></msg>'),
        )
    else:
        m_con.execute(
            f"CREATE TABLE [{blocked_tbl}] ("
            "local_id INTEGER PRIMARY KEY, "
            "server_id INTEGER, "
            "local_type INTEGER, "
            "real_sender_id INTEGER, "
            "create_time INTEGER, "
            "message_content TEXT, "
            "compress_content BLOB"
            ")"
        )
        m_con.execute(
            f"INSERT INTO [{blocked_tbl}] VALUES (1, 20001, 3, 3, ?, ?, NULL)",
            (base_time + 25, f'<msg><img md5="{BLOCKED_ASSET_ID}" length="{len(img_bytes)}" /></msg>'),
        )

    m_con.commit()
    m_con.close()

    # 5. state/image_keys.json
    # Notice: wxid in keys cache must match clean_wxid(WXID) for cache verification
    clean_id = WXID.split("_")[0] + "_" + WXID.split("_")[1] if len(WXID.split("_")) >= 3 else WXID
    image_keys_payload = {
        "wxid": clean_id,
        "aes_key": AES_KEY.decode("ascii"),
        "xor_key": XOR_KEY,
        "code": 1234,
        "source": "cache",
    }
    (state_dir / "image_keys.json").write_text(
        json.dumps(image_keys_payload, indent=2), encoding="utf-8"
    )

    # 6. config/media_policy.json
    policy_path = config_dir / "media_policy.json"
    policy_payload = {
        "enabled": True,
        "chat_allowlist": [SYNTH_CHAT],
        "kinds": {
            "file": True,
            "voice": True,
            "image": True,
            "video": True,
        },
        "wechat_files_glob": str(account_dir / "msg" / "file"),
        "wechat_account_dir": str(account_dir),
        "wechat_attach_root": str(account_dir / "msg" / "attach"),
        "wechat_cache_root": str(account_dir / "cache"),
        "wechat_video_root": str(account_dir / "msg" / "video"),
        "wxid": WXID,
        "daily_file_budget": 100,
        "max_items_per_run": 50,
        "initial_since_days": 30,
        "lookback_hours": 72,
        "image": {
            "max_pixels": 50000000,
            "ocr": {
                "enabled": True,
                "providers": ["tesseract", "rapidocr"],
            },
        },
        "video": {
            "max_bytes": 524288000,
            "keep_thumb": True,
            "thumbnail": {
                "mode": "auto",
            },
        },
    }
    policy_path.write_text(json.dumps(policy_payload, indent=2), encoding="utf-8")
    ledger_path = state_dir / "media_ledger.json"
    initial_ledger = {
        "schema": 1,
        "version": 1,
        "watermark": 0,
        "assets": {},
        "by_message": {f"{BLOCKED_CHAT}|1": BLOCKED_ASSET_ID},
        "anchors": {f"{BLOCKED_CHAT}|1": {"chat_id": BLOCKED_CHAT, "asset_id": BLOCKED_ASSET_ID}},
        "daily": {},
    }
    ledger_path.write_text(json.dumps(initial_ledger, indent=2), encoding="utf-8")

    return SyntheticVaultInfo(
        base_dir=base_dir,
        vault_dir=vault_dir,
        decrypted_dir=decrypted_dir,
        account_dir=account_dir,
        policy_path=policy_path,
        ledger_path=ledger_path,
        base_time=base_time,
        image_asset_id=IMAGE_ASSET_ID,
        video_asset_id=VIDEO_ASSET_ID,
        token=TOKEN,
    )
