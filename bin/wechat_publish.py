#!/usr/bin/env python3
"""
Atomic Publish and Rollback manager for WeChat Local Vault.

Handles:
1. Concurrency control via non-blocking file lock (.sync.lock)
2. Safe migration of legacy non-symlink 'current' directory
3. Decrypting WeChat databases into unique versioned directories (<UTC_TS>-<PID>)
4. In-place WAL merging on newly decrypted version
5. Atomic symlink switching for current and previous versions
6. Retention pruning preserving pinned/current/previous versions
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any, Callable

# Add scripts directory to sys.path
SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from wechat_wal import WalFormatError, merge_into  # noqa: E402

PAGE_SIZE = 4096
RESERVE = 80
IV_SIZE = 16

try:
    from Crypto.Cipher import AES
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


def atomic_symlink(link: Path, target: Path) -> None:
    """Atomically create or update a symlink to target."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link.parent / f".tmp_{link.name}_{os.getpid()}_{datetime.now().strftime('%f')}"
    try:
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        tmp_link.symlink_to(target)
        # os.replace atomically replaces destination if both are on the same filesystem
        os.replace(tmp_link, link)
    except Exception:
        if tmp_link.is_symlink() or tmp_link.exists():
            try:
                tmp_link.unlink()
            except OSError:
                pass
        raise


def extract_key_bytes(keys: dict[str, Any], rel_path: Path) -> bytes | None:
    """Look up 32-byte encryption key for a given database relative path."""
    candidates = [
        str(rel_path),
        str(rel_path).replace("\\", "/"),
        rel_path.name,
        rel_path.stem,
    ]
    for c in candidates:
        if c in keys:
            val = keys[c]
            hex_str = val.get("enc_key") if isinstance(val, dict) else val
            if hex_str:
                try:
                    k = bytes.fromhex(hex_str)
                    if len(k) == 32:
                        return k
                except (ValueError, TypeError):
                    pass
    return None


def decrypt_wechat_page(raw_page: bytes, pgno: int, key: bytes) -> bytes:
    """Decrypt a single SQLCipher page."""
    if not HAVE_CRYPTO:
        raise RuntimeError("pycryptodome is required for decrypting encrypted pages")
    if len(raw_page) != PAGE_SIZE:
        raise ValueError(f"Page size mismatch: expected {PAGE_SIZE}, got {len(raw_page)}")
    enc_start = 16 if pgno == 1 else 0
    enc_size = PAGE_SIZE - RESERVE - enc_start
    iv = raw_page[PAGE_SIZE - RESERVE : PAGE_SIZE - RESERVE + IV_SIZE]
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw_page[enc_start : enc_start + enc_size])
    out_page = bytearray(PAGE_SIZE)
    if pgno == 1:
        out_page[:16] = b"SQLite format 3\x00"
        out_page[16 : 16 + len(dec)] = dec
        out_page[16:18] = struct.pack(">H", PAGE_SIZE)
    else:
        out_page[: len(dec)] = dec
    return bytes(out_page)


def merge_wal_into(version_dir: Path, db_dir: Path, keys: dict[str, Any]) -> dict[str, Any]:
    """Merge WAL changes for all decrypted databases in version_dir."""
    summary: dict[str, Any] = {"merged": 0, "frames": 0, "details": {}}
    for dec_db in sorted(version_dir.rglob("*.db")):
        if dec_db.name.endswith("-wal") or dec_db.name.endswith("-shm"):
            continue

        rel_path = dec_db.relative_to(version_dir)
        src_wal = db_dir / rel_path.parent / f"{dec_db.name}-wal"
        if not src_wal.exists() or src_wal.stat().st_size == 0:
            continue

        key = extract_key_bytes(keys, rel_path)
        if not key:
            continue

        try:
            db_bytes = dec_db.read_bytes()
            wal_bytes = src_wal.read_bytes()
            decrypt_fn = lambda raw, pgno, k=key: decrypt_wechat_page(raw, pgno, k)
            merged_bytes, info = merge_into(db_bytes, wal_bytes, decrypt_fn)
            if info["frames"] > 0:
                dec_db.write_bytes(merged_bytes)
                summary["merged"] += 1
                summary["frames"] += info["frames"]
                summary["details"][str(rel_path)] = info
        except (WalFormatError, Exception) as e:
            # WAL merge failure is non-fatal: baseline decrypted database remains intact
            summary["details"][str(rel_path)] = {"error": str(e)}

    return summary


def prune_versions(dec_dir: Path, keep: int, pinned: set[str]) -> list[str]:
    """Prune older version directories in dec_dir/versions, preserving pinned ones."""
    versions_dir = dec_dir / "versions"
    if not versions_dir.is_dir():
        return []

    removed = []
    # Collect all version directories
    all_versions = [
        d for d in versions_dir.iterdir()
        if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")
    ]

    # Filter out pinned versions
    candidates = []
    for d in all_versions:
        if d.name in pinned or str(d) in pinned or d.name.startswith("legacy"):
            continue
        candidates.append(d)

    # Sort ascending by name (older versions first)
    candidates.sort(key=lambda p: p.name)

    if len(candidates) > keep:
        to_delete = candidates[: len(candidates) - keep]
        for d in to_delete:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)

    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="WeChat Local Vault Atomic Publisher")
    parser.add_argument("--db-dir", type=Path, required=True, help="Path to WeChat db_storage directory")
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path.home() / ".local/share/wechat-vault",
        help="Vault root directory",
    )
    parser.add_argument("--keys-file", type=Path, help="Path to all_keys.json")
    parser.add_argument(
        "--key-tool",
        type=Path,
        default=Path("/opt/wcdb-key-tool/wcdb_key_tool.py"),
        help="Path to wcdb_key_tool.py",
    )
    parser.add_argument("--keep-versions", type=int, default=3, help="Number of versions to retain")
    parser.add_argument("--dry-run", action="store_true", help="Perform decrypt and WAL merge without switching current")
    args = parser.parse_args()

    vault_root: Path = args.vault_root.expanduser().resolve()
    db_dir: Path = args.db_dir.expanduser().resolve()
    keys_file: Path = (args.keys_file or (vault_root / "keys/all_keys.json")).expanduser().resolve()
    key_tool: Path = args.key_tool.expanduser().resolve()
    dec_dir: Path = vault_root / "decrypted"
    versions_dir: Path = dec_dir / "versions"
    state_dir: Path = vault_root / "state"

    vault_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    versions_dir.mkdir(parents=True, exist_ok=True)

    # 1. Non-blocking file lock
    lock_file = vault_root / ".sync.lock"
    lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(f"[WARN] Another sync process is running (lock held on {lock_file}), skipping.", file=sys.stderr)
        os.close(lock_fd)
        return 0

    try:
        # Load pinned versions
        pinned_file = state_dir / "pinned_versions.json"
        pinned: set[str] = set()
        if pinned_file.exists():
            try:
                pinned = set(json.loads(pinned_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        # 2. Migrate existing non-symlink current directory if present (only when not dry-run)
        current_link = dec_dir / "current"
        if not args.dry_run and current_link.exists() and not current_link.is_symlink() and current_link.is_dir():
            legacy_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            legacy_name = f"legacy-{legacy_ts}"
            legacy_target = versions_dir / legacy_name
            print(f"[INFO] Migrating legacy non-symlink current to {legacy_target}...")
            shutil.move(str(current_link), str(legacy_target))
            atomic_symlink(current_link, legacy_target)
            pinned.add(legacy_name)
            pinned_file.write_text(json.dumps(sorted(pinned), indent=2), encoding="utf-8")

        # 3. Create unique version directory (timestamp with microseconds + PID)
        ver_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + str(os.getpid())
        target = versions_dir / ver_name
        target.mkdir(parents=True, exist_ok=False)

        # 4. Decrypt databases into target
        cmd = [
            sys.executable,
            str(key_tool),
            "decrypt",
            "--db-dir",
            str(db_dir),
            "--keys",
            str(keys_file),
            "--output",
            str(target),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[ERROR] Decrypt tool failed (exit {proc.returncode}):\n{proc.stderr}", file=sys.stderr)
            shutil.rmtree(target, ignore_errors=True)
            return 1

        # Verify key database exists
        expected_db = target / "message" / "message_0.db"
        if not expected_db.exists():
            # Check if flat layout or alternative
            any_db = list(target.rglob("*.db"))
            if not any_db:
                print(f"[ERROR] Decrypted target contains no database files: {target}", file=sys.stderr)
                shutil.rmtree(target, ignore_errors=True)
                return 1

        # 5. Merge WAL into target
        keys_data: dict[str, Any] = {}
        if keys_file.exists():
            try:
                keys_data = json.loads(keys_file.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[WARN] Failed to load keys for WAL merge: {e}", file=sys.stderr)

        wal_summary = merge_wal_into(target, db_dir, keys_data)
        print(f"[INFO] WAL merge complete: {wal_summary['merged']} DBs updated, {wal_summary['frames']} frames merged.")

        # 6. Dry run handling
        if args.dry_run:
            print(f"[INFO] [Dry-run] Version {ver_name} prepared successfully. Cleaning up dry-run directory.")
            shutil.rmtree(target, ignore_errors=True)
            return 0

        # 7. Atomic switch of symlinks
        previous_link = dec_dir / "previous"
        old_current: Path | None = None
        if current_link.is_symlink():
            try:
                old_target_str = os.readlink(str(current_link))
                old_current = Path(old_target_str).resolve()
            except OSError:
                pass

        if old_current and old_current.exists():
            atomic_symlink(previous_link, old_current)

        atomic_symlink(current_link, target)
        # Rebuild the pin set from scratch on every publish. Only the live
        # roll-forward pair (new current + previous/old current) is auto-pinned;
        # automatic publishes must never accumulate, or retention pruning can never
        # reclaim disk space. Legacy dirs are protected separately by prune_versions.
        auto_pins = {target.name}
        if old_current and old_current.exists():
            auto_pins.add(old_current.name)
        # No manual pin mechanism exists: pinned_versions.json is written only by this
        # script. Rebuilding from scratch each publish keeps the set bounded to the
        # live roll-forward pair, so retention pruning always has candidates.
        pinned = auto_pins

        # 8. Record state
        last_publish_info = {
            "version": ver_name,
            "target": str(target),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "wal_summary": wal_summary,
            "previous": str(old_current) if old_current else None,
        }
        (state_dir / "last_publish.json").write_text(
            json.dumps(last_publish_info, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Defense-in-depth: hard cap so any future accumulation bug cannot silently
        # disable retention again. Keeps the newest N pins.
        PIN_CAP = max(args.keep_versions * 3, 10)
        if len(pinned) > PIN_CAP:
            pinned = set(sorted(pinned)[-PIN_CAP:])
        pinned_file.write_text(json.dumps(sorted(pinned), indent=2), encoding="utf-8")

        # 9. Prune older versions
        removed = prune_versions(dec_dir, keep=args.keep_versions, pinned=pinned)
        if removed:
            print(f"[INFO] Pruned {len(removed)} old versions: {', '.join(removed)}")

        print(f"[INFO] Successfully published version {ver_name} -> {current_link}")
        return 0

    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
