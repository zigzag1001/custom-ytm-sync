#!/usr/bin/env python3
"""
check_archive.py — Archive integrity checker
============================================
Reads downloaded.txt, verifies each entry's file actually exists on disk,
and removes stale entries (while preserving UNAVAILABLE sentinels and blank
lines are dropped as a side-effect of the rewrite).

A timestamped backup of the original archive is written before any changes
are made so you can always roll back.

Usage (standalone):
    python check_archive.py [--root /music] [--dry-run] [--quiet]

Usage (from dl.py):
    import check_archive
    removed = check_archive.check_archive()   # returns list of pruned IDs
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_ROOT    = Path(os.environ.get("YTDL_OUTPUT_DIR", "/music")).expanduser()
ARCHIVE_NAME    = "downloaded.txt"
UNAVAILABLE_SENTINEL = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def check_archive(
    root: Path | str | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> list[str]:
    """
    Verify every entry in <root>/downloaded.txt against the filesystem.

    Rules:
      • Entry has path UNAVAILABLE → kept unconditionally (intentional sentinel)
      • Entry has an empty path    → kept (archive recorded before path tracking)
      • Entry path exists on disk  → kept
      • Entry path missing on disk → removed

    A backup is written to <root>/downloaded.<timestamp>.bak before any
    changes are committed.  Nothing is written if dry_run=True.

    Args:
        root:    Music library root.  Defaults to YTDL_OUTPUT_DIR or /music.
        dry_run: Report what would change without touching any file.
        quiet:   Suppress all printed output.

    Returns:
        List of video IDs that were (or would be) removed.
    """
    root         = Path(root) if root else DEFAULT_ROOT
    archive_path = root / ARCHIVE_NAME

    def log(msg: str) -> None:
        if not quiet:
            print(msg)

    if not archive_path.exists():
        log(f"[Archive] No archive found at {archive_path} — nothing to do.")
        return []

    raw_lines = archive_path.read_text(encoding="utf-8").splitlines()
    log(f"[Archive] {archive_path}  ({len(raw_lines)} raw lines)")

    kept:    list[tuple[str, str]] = []   # (video_id, rel_path) to retain
    removed: list[tuple[str, str]] = []   # (video_id, rel_path) to drop

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue   # drop blank lines; they'll vanish in the rewrite

        parts    = line.split("\t", 1)
        video_id = parts[0]
        rel_path = parts[1].strip() if len(parts) == 2 else ""

        # Always keep UNAVAILABLE sentinels and bare IDs (no path recorded)
        if rel_path in (UNAVAILABLE_SENTINEL, ""):
            kept.append((video_id, rel_path))
            continue

        abs_path = root / rel_path
        if abs_path.exists():
            kept.append((video_id, rel_path))
        else:
            removed.append((video_id, rel_path))

    # ── Report ────────────────────────────────────────────────────────────────
    if removed:
        log(f"\n[Archive] {len(removed)} stale entry/entries to remove:")
        for vid, path in removed:
            log(f"  - {vid}\t{path}")
    else:
        log("[Archive] All entries are valid — nothing to remove.")

    log(
        f"\n[Archive] Summary: {len(kept)} kept"
        + (f", {len(removed)} removed" if removed else "")
        + (" (dry run — no files changed)" if dry_run else "")
    )

    if not removed or dry_run:
        return [vid for vid, _ in removed]

    # ── Backup ────────────────────────────────────────────────────────────────
    timestamp   = time.strftime("%Y%m%d_%H%M%S")
    backup_path = archive_path.with_suffix(f".{timestamp}.bak")
    shutil.copy2(archive_path, backup_path)
    log(f"\n[Archive] Backup written: {backup_path}")

    # --- If more than 5 backups exist, delete the oldest one to avoid clutter
    backups = sorted(root.glob("downloaded.*.bak"))
    if len(backups) > 5:
        oldest = backups[0]
        oldest.unlink()
        log(f"[Archive] Oldest backup deleted: {oldest}")

    # ── Rewrite ───────────────────────────────────────────────────────────────
    new_content = "".join(
        f"{vid}\t{path}\n" if path else f"{vid}\n"
        for vid, path in kept
    )
    archive_path.write_text(new_content, encoding="utf-8")
    log(f"[Archive] Archive updated: {archive_path}")

    return [vid for vid, _ in removed]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check archive integrity and prune missing files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Check and prune (default root from YTDL_OUTPUT_DIR or /music)
  python check_archive.py

  # Custom root
  python check_archive.py --root ~/music

  # Preview without changing anything
  python check_archive.py --dry-run
""",
    )
    p.add_argument(
        "--root", "-r",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Music library root  (default: {DEFAULT_ROOT})",
    )
    p.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Report stale entries without modifying the archive",
    )
    p.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress all output except errors",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.root.exists():
        print(f"ERROR: root directory not found: {args.root}", file=sys.stderr)
        sys.exit(1)

    removed = check_archive(
        root    = args.root,
        dry_run = args.dry_run,
        quiet   = args.quiet,
    )

    sys.exit(0 if not removed else 1)


if __name__ == "__main__":
    main()
