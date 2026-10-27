#!/usr/bin/env python3
"""Tests for WeChat video media enrichment and OCR fallback contract.

Covers:
- Ready state: MP4 plaintext video export, thumb export, metadata parsing
- Missing source: video file not present on disk
- Oversize: file larger than video.max_bytes
- Encrypted video dat: unverified branch
- Ledger entry schema & kind="video" field completeness
- Optional OCR: image ready when OCR disabled or unavailable
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import struct
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import wechat_image  # noqa: E402
import wechat_media  # noqa: E402


def _create_synthetic_mp4(width: int = 1920, height: int = 1080, duration_s: int = 12) -> bytes:
    """Build a minimal valid MP4 container with ftyp, mvhd, and tkhd boxes."""
    # ftyp box
    ftyp_data = b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41"
    ftyp = struct.pack(">I", 8 + len(ftyp_data)) + b"ftyp" + ftyp_data

    # mvhd box inside moov
    # version 0, creation 0, mod 0, timescale 1000, duration (duration_s * 1000)
    timescale = 1000
    dur = duration_s * timescale
    mvhd_payload = (
        b"\x00"  # version
        + b"\x00\x00\x00"  # flags
        + struct.pack(">II", 0, 0)  # creation, modification
        + struct.pack(">II", timescale, dur)  # timescale, duration
        + b"\x00\x01\x00\x00"  # rate 1.0
        + b"\x01\x00"  # volume 1.0
        + b"\x00" * 10  # reserved
        + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00"  # matrix
        + b"\x00" * 24  # pre-defined
        + struct.pack(">I", 2)  # next_track_id
    )
    mvhd = struct.pack(">I", 8 + len(mvhd_payload)) + b"mvhd" + mvhd_payload

    # tkhd box inside trak
    # track width and height are 16.16 fixed point at the end
    tkhd_payload = (
        b"\x00"  # version
        + b"\x00\x00\x07"  # flags: track enabled, in movie, in preview
        + struct.pack(">II", 0, 0)  # creation, mod
        + struct.pack(">I", 1)  # track id
        + b"\x00" * 4  # reserved
        + struct.pack(">I", dur)  # duration
        + b"\x00" * 8  # reserved
        + b"\x00\x00"  # layer
        + b"\x00\x00"  # alternate group
        + b"\x00\x00"  # volume
        + b"\x00\x00"  # reserved
        + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00"  # matrix
        + struct.pack(">II", width << 16, height << 16)  # width, height
    )
    tkhd = struct.pack(">I", 8 + len(tkhd_payload)) + b"tkhd" + tkhd_payload

    trak = struct.pack(">I", 8 + len(tkhd)) + b"trak" + tkhd
    moov_payload = mvhd + trak
    moov = struct.pack(">I", 8 + len(moov_payload)) + b"moov" + moov_payload

    # mdat dummy box
    mdat_payload = b"\x00" * 64
    mdat = struct.pack(">I", 8 + len(mdat_payload)) + b"mdat" + mdat_payload

    return ftyp + moov + mdat


def _setup_decrypted_db(decrypted_dir: Path, chat_id: str, local_id: int, ct: int,
                        xml_content: str, local_type: int = 43) -> None:
    msg_dir = decrypted_dir / "message"
    msg_dir.mkdir(parents=True, exist_ok=True)
    db_path = msg_dir / "message_0.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    con.execute("INSERT INTO Name2Id VALUES (1, ?)", (chat_id,))
    chat_hash = hashlib.md5(chat_id.encode()).hexdigest()
    tbl_name = f"Msg_{chat_hash}"
    con.execute(
        f"CREATE TABLE [{tbl_name}] ("
        "local_id INTEGER, real_sender_id INTEGER, create_time INTEGER, "
        "local_type INTEGER, message_content TEXT, compress_content BLOB)"
    )
    con.execute(
        f"INSERT INTO [{tbl_name}] VALUES (?, 0, ?, ?, ?, NULL)",
        (local_id, ct, local_type, xml_content),
    )
    con.commit()
    con.close()


class TestMediaVideo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault = self.tmp / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        self.decrypted = self.vault / "decrypted" / "current"
        self.decrypted.mkdir(parents=True, exist_ok=True)
        self.account_dir = self.tmp / "account"
        self.video_root = self.account_dir / "msg" / "video"
        self.video_root.mkdir(parents=True, exist_ok=True)
        self.attach_root = self.account_dir / "msg" / "attach"
        self.attach_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_detect_format_and_metadata_mp4(self):
        mp4_bytes = _create_synthetic_mp4(1920, 1080, 15)
        self.assertEqual(wechat_image.detect_format(mp4_bytes[:32]), "mp4")
        w, h, dur = wechat_image.parse_mp4_metadata(mp4_bytes)
        self.assertEqual(w, 1920)
        self.assertEqual(h, 1080)
        self.assertEqual(dur, 15.0)

    def test_video_ready_export(self):
        chat_id = "test_user_video"
        lid = 101
        ct = int(time.time()) - 100
        asset_md5 = "e10adc3949ba59abbe56e057f20f883e"
        mp4_data = _create_synthetic_mp4(1080, 1920, 12)

        # Write video to video_root/2026-10/
        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        video_file = month_dir / f"{asset_md5}.mp4"
        video_file.write_bytes(mp4_data)

        # Write hardlink.db
        hl_dir = self.decrypted / "hardlink"
        hl_dir.mkdir(parents=True, exist_ok=True)
        hl_con = sqlite3.connect(hl_dir / "hardlink.db")
        hl_con.execute("CREATE TABLE dir2id(rowid INTEGER PRIMARY KEY, username TEXT)")
        hl_con.execute("INSERT INTO dir2id VALUES (1, '2026-10')")
        hl_con.execute("CREATE TABLE video_hardlink_info_v4(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)")
        hl_con.execute("INSERT INTO video_hardlink_info_v4 VALUES (?, ?, 1, 0)", (asset_md5, f"{asset_md5}.mp4"))
        hl_con.commit()
        hl_con.close()

        xml = (
            f'<videomsg length="{len(mp4_data)}" playlength="12" '
            f'md5="{asset_md5}" fromusername="{chat_id}"/>'
        )
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["video"] = {"max_bytes": 524288000, "copy_mode": "copy", "keep_thumb": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        ledger_path = self.vault / "state" / "media_ledger.json"
        ret = wechat_media.main_with_args([
            "--policy", str(policy_path),
            "--vault", str(self.vault),
        ]) if hasattr(wechat_media, "main_with_args") else None

        # If main_with_args is not present, invoke via subprocess or monkeypatch sys.argv
        if ret is None:
            old_argv = sys.argv
            try:
                sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
                ret = wechat_media.main()
            finally:
                sys.argv = old_argv

        self.assertEqual(ret, 0)
        self.assertTrue(ledger_path.is_file())
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)

        # Contract verification
        self.assertEqual(asset["md5"], asset_md5)
        self.assertEqual(asset["kind"], "video")
        self.assertEqual(asset["state"], "ready")
        self.assertIsNone(asset["deny_reason"])
        self.assertEqual(asset["format"], "mp4")
        self.assertEqual(asset["bytes"], len(mp4_data))
        self.assertEqual(asset["duration_s"], 12)
        self.assertEqual(asset["width"], 1080)
        self.assertEqual(asset["height"], 1920)
        self.assertTrue(asset["path"].endswith(f"{asset_md5}/video.mp4"))
        self.assertEqual(asset["anchor"]["chat_id"], chat_id)
        self.assertEqual(asset["anchor"]["local_id"], lid)
        self.assertEqual(asset["anchor"]["ts"], ct)
        self.assertEqual(asset["source"], "hardlink")
        self.assertEqual(asset["attempts"], 1)
        self.assertIn("parsed_at", asset)

        # Verify exported file exists (both in exports/media/<date>/<md5>/ and vault/media/<md5>/)
        exported_path = self.vault / "exports" / asset["path"]
        vault_media_path = self.vault / "media" / asset_md5 / "video.mp4"
        self.assertTrue(exported_path.is_file())
        self.assertTrue(vault_media_path.is_file())
        self.assertEqual(exported_path.stat().st_size, len(mp4_data))
    def test_video_hardlink_copy_mode(self):
        """Verify video.copy_mode=hardlink uses os.link rather than separate copy."""
        chat_id = "test_user_video"
        lid = 105
        ct = int(time.time()) - 100
        asset_md5 = "d41d8cd98f00b204e9800998ecf8427e"
        mp4_data = _create_synthetic_mp4(720, 1280, 8)

        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        video_file = month_dir / f"{asset_md5}.mp4"
        video_file.write_bytes(mp4_data)

        hl_dir = self.decrypted / "hardlink"
        hl_dir.mkdir(parents=True, exist_ok=True)
        hl_con = sqlite3.connect(hl_dir / "hardlink.db")
        hl_con.execute("CREATE TABLE IF NOT EXISTS dir2id(rowid INTEGER PRIMARY KEY, username TEXT)")
        hl_con.execute("CREATE TABLE IF NOT EXISTS video_hardlink_info_v4(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)")
        hl_con.execute("INSERT OR REPLACE INTO dir2id VALUES (1, '2026-10')")
        hl_con.execute("INSERT OR REPLACE INTO video_hardlink_info_v4 VALUES (?, ?, 1, 0)", (asset_md5, f"{asset_md5}.mp4"))
        hl_con.commit()
        hl_con.close()

        xml = f'<videomsg length="{len(mp4_data)}" playlength="8" md5="{asset_md5}"/>'
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["video"] = {"max_bytes": 524288000, "copy_mode": "hardlink", "keep_thumb": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        old_argv = sys.argv
        try:
            sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
            wechat_media.main()
        finally:
            sys.argv = old_argv

        ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "ready")
        exported_path = self.vault / "exports" / asset["path"]
        self.assertTrue(exported_path.is_file())
        # Verify same inode on platforms supporting hardlinks
        self.assertEqual(exported_path.stat().st_ino, video_file.stat().st_ino)

    def test_video_missing_attributes_graceful(self):
        """Verify iter_video_messages and processing handles missing md5/playlength without throwing."""
        chat_id = "test_user_video"
        lid = 106
        ct = int(time.time()) - 100
        # videomsg missing md5 and playlength
        xml = '<videomsg corrupted="true" />'
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        old_argv = sys.argv
        try:
            sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
            ret = wechat_media.main()
        finally:
            sys.argv = old_argv

        self.assertEqual(ret, 0)
        ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
        # Corrupted entry with no md5 was skipped gracefully without crash
        self.assertNotIn("", ledger["assets"])


    def test_video_missing_source(self):
        chat_id = "test_user_video"
        lid = 102
        ct = int(time.time()) - 100
        asset_md5 = "c4ca4238a0b923820dcc509a6f75849b"

        xml = f'<videomsg length="12345" playlength="5" md5="{asset_md5}"/>'
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        old_argv = sys.argv
        try:
            sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
            wechat_media.main()
        finally:
            sys.argv = old_argv

        ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "skipped")
        self.assertEqual(asset["deny_reason"], "missing_source")
        self.assertEqual(asset["kind"], "video")

    def test_video_oversize(self):
        chat_id = "test_user_video"
        lid = 103
        ct = int(time.time()) - 100
        asset_md5 = "eccbc87e4b5ce2fe28308fd9f2a7baf3"

        # Declared length exceeds video.max_bytes
        xml = f'<videomsg length="600000000" playlength="60" md5="{asset_md5}"/>'
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["video"] = {"max_bytes": 524288000, "copy_mode": "copy", "keep_thumb": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        old_argv = sys.argv
        try:
            sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
            wechat_media.main()
        finally:
            sys.argv = old_argv

        ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "skipped")
        self.assertEqual(asset["deny_reason"], "oversize")
        self.assertEqual(asset["kind"], "video")

    def test_video_encrypted_unverified(self):
        chat_id = "test_user_video"
        lid = 104
        ct = int(time.time()) - 100
        asset_md5 = "a87ff679a2f3e71d9181a67b7542122c"

        # Write encrypted .dat file with V2 magic
        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        dat_file = month_dir / f"{asset_md5}.dat"
        dat_file.write_bytes(b"\x07\x08V2" + b"\x00" * 64)

        xml = f'<videomsg length="68" playlength="3" md5="{asset_md5}"/>'
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {"file": False, "image": False, "voice": False, "video": True}
        policy["video"] = {"max_bytes": 524288000, "copy_mode": "copy", "keep_thumb": True}
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        old_argv = sys.argv
        try:
            sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
            wechat_media.main()
        finally:
            sys.argv = old_argv

        ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "skipped")
        self.assertEqual(asset["deny_reason"], "encrypted_video_unverified")
        self.assertEqual(asset["kind"], "video")

    def test_image_ready_when_ocr_disabled(self):
        """When image.ocr.enabled=false, images must still export as ready with ocr.status=skipped."""
        chat_id = "test_user_img"
        lid = 201
        ct = int(time.time()) - 100
        asset_md5 = "8f14e45fceea167a5a36dedd4bea2543"

        # Build raw JPEG image
        jpeg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00" + b"\x01" * 64
        # Add SOF0 marker for width=100, height=100
        jpeg_data += b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 100, 100) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        jpeg_data += b"\xff\xd9"

        # Mock resolve_keys and decrypt_dat in wechat_image
        import wechat_image
        orig_resolve = wechat_image.resolve_keys
        orig_decrypt = wechat_image.decrypt_dat

        from collections import namedtuple
        MockDec = namedtuple("MockDec", ["data", "fmt", "width", "height", "quality"])
        wechat_image.resolve_keys = lambda *args, **kwargs: object()
        wechat_image.decrypt_dat = lambda *args, **kwargs: MockDec(
            data=jpeg_data, fmt="jpg", width=100, height=100, quality="full"
        )

        try:
            # Place dummy dat file in attach_root/chat_hash/2026-10/Img/
            chat_hash = hashlib.md5(chat_id.encode()).hexdigest()
            img_dir = self.attach_root / chat_hash / "2026-10" / "Img"
            img_dir.mkdir(parents=True, exist_ok=True)
            dat_file = img_dir / f"{asset_md5}.dat"
            dat_file.write_bytes(b"\x07\x08V2" + b"\x00" * 32)

            xml = f'<msg><img md5="{asset_md5}" length="{len(jpeg_data)}" /></msg>'
            _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml, local_type=3)

            policy = dict(wechat_media.DEFAULT_POLICY_BODY)
            policy["enabled"] = True
            policy["chat_allowlist"] = [chat_id]
            policy["kinds"] = {"file": False, "image": True, "voice": False, "video": False}
            policy["image"] = {
                "enabled": True,
                "ocr": {"enabled": False},  # OCR disabled!
            }
            policy["wechat_account_dir"] = str(self.account_dir)
            policy_path = self.vault / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            old_argv = sys.argv
            try:
                sys.argv = ["wechat_media.py", "--policy", str(policy_path), "--vault", str(self.vault)]
                wechat_media.main()
            finally:
                sys.argv = old_argv

            ledger = json.loads((self.vault / "state" / "media_ledger.json").read_text(encoding="utf-8"))
            asset = ledger["assets"].get(asset_md5)
            self.assertIsNotNone(asset)
            self.assertEqual(asset["state"], "ready")
            self.assertEqual(asset["kind"], "image")
            self.assertEqual(asset["ocr"]["status"], "skipped")
            # Image path is media/<md5>/image.jpg in vault/
            self.assertTrue((self.vault / asset["path"]).is_file())
        finally:
            wechat_image.resolve_keys = orig_resolve
            wechat_image.decrypt_dat = orig_decrypt

    def test_video_thumb_no_ffmpeg(self):
        """When no ffmpeg/thumb, state=ready & thumb_error=no_ffmpeg."""
        chat_id = "test_user_video"
        lid = 301
        ct = int(time.time()) - 100
        asset_md5 = "11111111111111111111111111111111"
        mp4_data = _create_synthetic_mp4(720, 1280, 5)

        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / f"{asset_md5}.mp4").write_bytes(mp4_data)

        xml = (
            f'<videomsg length="{len(mp4_data)}" playlength="5" '
            f'md5="{asset_md5}"/>'
        )
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml,
                            local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {
            "file": False, "image": False, "voice": False, "video": True
        }
        policy["video"] = {
            "max_bytes": 524288000,
            "copy_mode": "copy",
            "keep_thumb": True,
            "thumbnail": {"mode": "auto", "ffmpeg_timeout": 20},
        }
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        orig_which = shutil.which
        try:
            shutil.which = lambda c: None if c == "ffmpeg" else orig_which(c)
            old_argv = sys.argv
            try:
                sys.argv = [
                    "wechat_media.py", "--policy", str(policy_path),
                    "--vault", str(self.vault)
                ]
                wechat_media.main()
            finally:
                sys.argv = old_argv
        finally:
            shutil.which = orig_which

        ledger_file = self.vault / "state" / "media_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "ready")
        self.assertIsNone(asset["thumb"])
        self.assertEqual(asset["thumb_error"], "no_ffmpeg")

    def test_video_thumb_ffmpeg_success(self):
        """When ffmpeg succeeds, thumb exists on disk and in ledger."""
        chat_id = "test_user_video"
        lid = 302
        ct = int(time.time()) - 100
        asset_md5 = "22222222222222222222222222222222"
        mp4_data = _create_synthetic_mp4(720, 1280, 5)

        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / f"{asset_md5}.mp4").write_bytes(mp4_data)

        xml = (
            f'<videomsg length="{len(mp4_data)}" playlength="5" '
            f'md5="{asset_md5}"/>'
        )
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml,
                            local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {
            "file": False, "image": False, "voice": False, "video": True
        }
        policy["video"] = {
            "max_bytes": 524288000,
            "copy_mode": "copy",
            "keep_thumb": True,
            "thumbnail": {"mode": "auto", "ffmpeg_timeout": 20},
        }
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        orig_which = shutil.which
        orig_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "/mock/ffmpeg":
                out_file = Path(cmd[-1])
                out_file.write_bytes(
                    b"\xff\xd8\xff\xe0fake_jpg_content\xff\xd9"
                )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=b"", stderr=b""
                )
            return orig_run(cmd, *args, **kwargs)

        try:
            shutil.which = (
                lambda c: "/mock/ffmpeg" if c == "ffmpeg" else orig_which(c)
            )
            subprocess.run = fake_run
            old_argv = sys.argv
            try:
                sys.argv = [
                    "wechat_media.py", "--policy", str(policy_path),
                    "--vault", str(self.vault)
                ]
                wechat_media.main()
            finally:
                sys.argv = old_argv
        finally:
            shutil.which = orig_which
            subprocess.run = orig_run

        ledger_file = self.vault / "state" / "media_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "ready")
        self.assertIsNotNone(asset["thumb"])
        self.assertIsNone(asset["thumb_error"])
        self.assertTrue((self.vault / "exports" / asset["thumb"]).is_file())

    def test_video_thumb_ffmpeg_timeout(self):
        """When ffmpeg times out, state is ready and thumb_error is timeout."""
        chat_id = "test_user_video"
        lid = 303
        ct = int(time.time()) - 100
        asset_md5 = "33333333333333333333333333333333"
        mp4_data = _create_synthetic_mp4(720, 1280, 5)

        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / f"{asset_md5}.mp4").write_bytes(mp4_data)

        xml = (
            f'<videomsg length="{len(mp4_data)}" playlength="5" '
            f'md5="{asset_md5}"/>'
        )
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml,
                            local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {
            "file": False, "image": False, "voice": False, "video": True
        }
        policy["video"] = {
            "max_bytes": 524288000,
            "copy_mode": "copy",
            "keep_thumb": True,
            "thumbnail": {"mode": "auto", "ffmpeg_timeout": 5},
        }
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        orig_which = shutil.which
        orig_run = subprocess.run

        def fake_run_timeout(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "/mock/ffmpeg":
                raise subprocess.TimeoutExpired(cmd, timeout=5)
            return orig_run(cmd, *args, **kwargs)

        try:
            shutil.which = (
                lambda c: "/mock/ffmpeg" if c == "ffmpeg" else orig_which(c)
            )
            subprocess.run = fake_run_timeout
            old_argv = sys.argv
            try:
                sys.argv = [
                    "wechat_media.py", "--policy", str(policy_path),
                    "--vault", str(self.vault)
                ]
                wechat_media.main()
            finally:
                sys.argv = old_argv
        finally:
            shutil.which = orig_which
            subprocess.run = orig_run

        ledger_file = self.vault / "state" / "media_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "ready")
        self.assertIsNone(asset["thumb"])
        self.assertEqual(asset["thumb_error"], "timeout")

    def test_video_thumb_mode_wechat_no_ffmpeg_call(self):
        """When mode=wechat, ffmpeg subprocess is never invoked."""
        chat_id = "test_user_video"
        lid = 304
        ct = int(time.time()) - 100
        asset_md5 = "44444444444444444444444444444444"
        mp4_data = _create_synthetic_mp4(720, 1280, 5)

        month_dir = self.video_root / "2026-10"
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / f"{asset_md5}.mp4").write_bytes(mp4_data)

        xml = (
            f'<videomsg length="{len(mp4_data)}" playlength="5" '
            f'md5="{asset_md5}"/>'
        )
        _setup_decrypted_db(self.decrypted, chat_id, lid, ct, xml,
                            local_type=43)

        policy = dict(wechat_media.DEFAULT_POLICY_BODY)
        policy["enabled"] = True
        policy["chat_allowlist"] = [chat_id]
        policy["kinds"] = {
            "file": False, "image": False, "voice": False, "video": True
        }
        policy["video"] = {
            "max_bytes": 524288000,
            "copy_mode": "copy",
            "keep_thumb": True,
            "thumbnail": {"mode": "wechat", "ffmpeg_timeout": 20},
        }
        policy["wechat_account_dir"] = str(self.account_dir)
        policy_path = self.vault / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        orig_which = shutil.which
        orig_run = subprocess.run
        ffmpeg_called = []

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "/mock/ffmpeg":
                ffmpeg_called.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return orig_run(cmd, *args, **kwargs)

        try:
            shutil.which = (
                lambda c: "/mock/ffmpeg" if c == "ffmpeg" else orig_which(c)
            )
            subprocess.run = fake_run
            old_argv = sys.argv
            try:
                sys.argv = [
                    "wechat_media.py", "--policy", str(policy_path),
                    "--vault", str(self.vault)
                ]
                wechat_media.main()
            finally:
                sys.argv = old_argv
        finally:
            shutil.which = orig_which
            subprocess.run = orig_run

        self.assertEqual(len(ffmpeg_called), 0)
        ledger_file = self.vault / "state" / "media_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        asset = ledger["assets"].get(asset_md5)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["state"], "ready")
        self.assertIsNone(asset["thumb"])
        self.assertIsNone(asset["thumb_error"])


if __name__ == "__main__":
    unittest.main()
