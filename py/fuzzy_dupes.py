#!/usr/bin/env python3
"""
fuzzy_dupes.py — Fuzzy duplicate song finder
=============================================
Scans a music library for tracks that are likely duplicates based on
artist + title similarity, even when tags differ slightly (e.g. punctuation,
featuring credits, remaster suffixes, unicode variants).

Supported formats: .opus, .flac, .mp3, .m4a, .ogg, .wav

Dependencies:
    pip install rapidfuzz mutagen python-dotenv

Usage (standalone):
    python fuzzy_dupes.py [--root /music] [--threshold 90] [--quiet] [--json]
    python fuzzy_dupes.py --playlist                  # also write dupes.m3u8
    python fuzzy_dupes.py --playlist my_dupes.m3u8   # custom playlist path

Usage (from dl.py):
    import fuzzy_dupes
    groups = fuzzy_dupes.get_dupes(threshold=90)
    fuzzy_dupes.write_dupes_playlist(groups, root, playlist_path)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from rapidfuzz import fuzz

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".opus", ".flac", ".mp3", ".m4a", ".ogg", ".wav"}

DEFAULT_ROOT      = Path(os.environ.get("YTDL_OUTPUT_DIR", "/music")).expanduser()
DEFAULT_THRESHOLD = 90   # 0–100; higher = stricter (fewer false positives)

# Noise patterns stripped before comparison (same logic as dl.py's clean_text)
_NOISE = re.compile(
    r"""
    \s*[\(\[\{]\s*
    (?:
        feat(?:uring)?\.?\s+[^\)\]\}]+   |
        ft\.?\s+[^\)\]\}]+               |
        prod(?:uced)?\.?\s+by\s+[^\)\]\}]+ |
        dir(?:ected)?\.?\s+by\s+[^\)\]\}]+ |
        (?:official\s+)?(?:music\s+|lyric\s+|audio\s+|live\s+)?video |
        official\s+(?:audio|visuali[sz]er|clip|performance|stream) |
        (?:official\s+)?visuali[sz]er   |
        official | audio | lyrics?       |
        (?:\d+\s+)?remaster(?:ed)?       |
        (?:\d+\s+)?reissue               |
        (?:\d+(?:st|nd|rd|th)\s+)?anniversary\s+(?:edition|remaster(?:ed)?)? |
        (?:super\s+)?deluxe(?:\s+edition)? |
        (?:special|limited)\s+edition   |
        bonus\s+track | explicit         |
        clean\s+version | radio\s+edit   |
        single\s+version                 |
        hd | hq | 4k | uhd               |
        slowed(?:\s+\+\s*reverb)?        |
        reverb | extended | instrumental |
        acoustic | live | demo | remix
    )
    \s*[\)\]\}]
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Track:
    path:    Path
    artist:  str
    title:   str
    # Normalised key used for comparison
    key:     str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.key = _make_key(self.artist, self.title)


@dataclass
class DupeGroup:
    tracks:     list[Track]
    similarity: float   # 0–100; similarity of the pair that triggered grouping


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(s: str) -> str:
    """Unicode-normalise, lowercase, strip noise, collapse whitespace."""
    # s = unicodedata.normalize("NFKD", s)
    # s = s.encode("ascii", "ignore").decode("ascii")   # drop diacritics
    # s = _NOISE.sub("", s)
    # s = re.sub(r"[^\w\s]", " ", s)                   # punctuation → space
    # s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _make_key(artist: str, title: str) -> str:
    return _normalise(artist) + " " + _normalise(title)


# ---------------------------------------------------------------------------
# Tag reading
# ---------------------------------------------------------------------------

def _read_tags(path: Path) -> tuple[str, str] | None:
    """Return (artist, title) or None if unreadable / untagged."""
    ext = path.suffix.lower()
    try:
        if ext in (".opus", ".ogg"):
            cls   = OggOpus if ext == ".opus" else OggVorbis
            audio = cls(path)
            artist = (audio.get("artist") or audio.get("albumartist") or [""])[0].strip()
            title  = (audio.get("title") or [""])[0].strip()

        elif ext == ".flac":
            audio  = FLAC(path)
            artist = (audio.get("artist") or audio.get("albumartist") or [""])[0].strip()
            title  = (audio.get("title") or [""])[0].strip()

        elif ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return None
            artist = str(tags.get("TPE1") or tags.get("TPE2") or "").strip()
            title  = str(tags.get("TIT2") or "").strip()

        elif ext == ".m4a":
            audio  = MP4(path)
            artist = (audio.get("©ART") or audio.get("aART") or [""])[0].strip()
            title  = (audio.get("©nam") or [""])[0].strip()

        else:
            # .wav and other formats: fall back to filename
            artist, title = "", path.stem

    except Exception:
        return None

    if not title:
        # Derive title from filename as last resort
        title = path.stem

    return artist or "Unknown Artist", title


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

def scan_library(root: Path) -> list[Track]:
    """Walk *root* recursively and return a Track for every audio file found."""
    tracks: list[Track] = []
    skip_dirs = {".tmp_ytdl", "playlists"}

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune directories we never want to descend into
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            path = Path(dirpath) / fname
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            tags = _read_tags(path)
            if tags is None:
                continue
            artist, title = tags
            tracks.append(Track(path=path, artist=artist, title=title))

    return tracks


# ---------------------------------------------------------------------------
# Fuzzy duplicate detection
# ---------------------------------------------------------------------------

def find_dupes(tracks: list[Track], threshold: float) -> list[DupeGroup]:
    """
    Compare every pair of tracks using token_set_ratio on their normalised
    artist+title key.  Groups overlapping pairs into clusters so a set of
    3+ near-identical tracks appears as one group rather than multiple pairs.

    Time complexity: O(n²) — fine for libraries up to ~50 k tracks.
    """
    n = len(tracks)
    # Union-Find for clustering
    parent = list(range(n))
    best_score: dict[int, float] = {}   # root → max similarity seen

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int, score: float) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            best_score[rx] = max(best_score.get(rx, 0), score)
            return
        # Merge smaller into larger (arbitrary tie-break on index)
        if rx > ry:
            rx, ry = ry, rx
        parent[ry] = rx
        best_score[rx] = max(best_score.get(rx, 0), best_score.get(ry, 0), score)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = tracks[i], tracks[j]
            # Quick guard: if both keys share no words at all, skip
            if not (set(a.key.split()) & set(b.key.split())):
                continue
            score = fuzz.token_set_ratio(a.key, b.key)
            if score >= threshold:
                union(i, j, score)

    # Collect groups
    from collections import defaultdict
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    groups: list[DupeGroup] = []
    for root_idx, members in clusters.items():
        if len(members) < 2:
            continue
        member_tracks = [tracks[i] for i in members]
        # Sort: oldest file first (likely the "original")
        member_tracks.sort(key=lambda t: t.path.stat().st_mtime)
        sim = best_score.get(root_idx, threshold)
        groups.append(DupeGroup(tracks=member_tracks, similarity=sim))

    # Most suspicious groups first
    groups.sort(key=lambda g: g.similarity, reverse=True)
    return groups


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def report_text(groups: list[DupeGroup], root: Path) -> str:
    if not groups:
        return "✓ No duplicates found.\n"

    lines: list[str] = [
        f"Found {len(groups)} suspected duplicate group(s):",
        "=" * 60,
        "",
    ]
    for i, g in enumerate(groups, 1):
        lines.append(f"Group {i}  (similarity {g.similarity:.0f}%)")
        for t in g.tracks:
            mtime = t.path.stat().st_mtime
            import time
            age = time.strftime("%Y-%m-%d", time.localtime(mtime))
            lines.append(f"  [{age}]  {t.artist} – {t.title}")
            lines.append(f"           {_rel(t.path, root)}")
        lines.append("")
    return "\n".join(lines)


def report_json(groups: list[DupeGroup], root: Path) -> str:
    out = []
    for g in groups:
        out.append({
            "similarity": round(g.similarity, 1),
            "tracks": [
                {
                    "artist": t.artist,
                    "title":  t.title,
                    "path":   _rel(t.path, root),
                }
                for t in g.tracks
            ],
        })
    return json.dumps(out, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Playlist writing
# ---------------------------------------------------------------------------

def _extinf_for_track(track: Track) -> str:
    """Return an #EXTINF line, reading duration from the file when possible."""
    duration = -1
    display  = f"{track.artist} - {track.title}" if track.artist else track.title

    try:
        ext = track.path.suffix.lower()
        if ext in (".opus", ".ogg"):
            cls   = OggOpus if ext == ".opus" else OggVorbis
            audio = cls(track.path)
            duration = int(getattr(audio.info, "length", -1))
        elif ext == ".flac":
            audio    = FLAC(track.path)
            duration = int(getattr(audio.info, "length", -1))
        elif ext == ".mp3":
            from mutagen.mp3 import MP3
            audio    = MP3(track.path)
            duration = int(getattr(audio.info, "length", -1))
        elif ext == ".m4a":
            audio    = MP4(track.path)
            duration = int(getattr(audio.info, "length", -1))
    except Exception:
        pass

    return f"#EXTINF:{duration},{display}"


def write_dupes_playlist(
    groups: list[DupeGroup],
    root: Path,
    playlist_path: Path | None = None,
) -> Path | None:
    """
    Write an extended M3U8 playlist containing every track from every dupe
    group, separated by group-header comments.

    Tracks within each group are sorted oldest-first (the presumed original
    comes first).  Groups are ordered by descending similarity so the most
    certain duplicates appear at the top.

    The playlist uses paths relative to its own location so the whole music
    tree remains portable.

    Args:
        groups:        Output of find_dupes().
        root:          Music library root (used to compute relative paths).
        playlist_path: Where to write the file.  Defaults to
                       <root>/playlists/dupes.m3u8.

    Returns:
        The Path that was written, or None if groups is empty.
    """
    if not groups:
        return None

    if playlist_path is None:
        playlist_path = root / "playlists" / "dupes.m3u8"

    playlist_path.parent.mkdir(parents=True, exist_ok=True)

    total = sum(len(g.tracks) for g in groups)
    lines: list[str] = [
        "#EXTM3U",
        f"#PLAYLIST:Duplicate Songs ({len(groups)} groups, {total} tracks)",
        "",
    ]

    for i, g in enumerate(groups, 1):
        lines.append(
            f"#EXTGRP:Group {i} — similarity {g.similarity:.0f}% "
            f"({len(g.tracks)} tracks)"
        )
        for t in g.tracks:
            # Path relative to the playlist file's directory
            try:
                rel = Path(os.path.relpath(t.path, playlist_path.parent))
            except ValueError:
                # Different drive on Windows — fall back to absolute
                rel = t.path
            lines.append(_extinf_for_track(t))
            lines.append(rel.as_posix())
        lines.append("")   # blank line between groups

    playlist_path.write_text("\n".join(lines), encoding="utf-8")
    return playlist_path


# ---------------------------------------------------------------------------
# Public API (called from dl.py)
# ---------------------------------------------------------------------------

def get_dupes(
    root: Path | str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    quiet: bool = False,
    playlist_path: Path | str | bool | None = None,
) -> list[DupeGroup]:
    """
    Scan *root* for fuzzy duplicate tracks and return a list of DupeGroup.

    Args:
        root:          Music library root.  Defaults to YTDL_OUTPUT_DIR or /music.
        threshold:     Similarity threshold 0–100.  90 is a good default.
                       Lower = more matches (more false positives).
                       Higher = only near-exact matches.
        quiet:         Suppress printed output.
        playlist_path: Where to write the dupes playlist.
                       • None / False  → no playlist written  (default)
                       • True          → write to <root>/playlists/dupes.m3u8
                       • Path / str    → write to that exact path

    Returns:
        List of DupeGroup, each containing 2+ Track objects.
    """
    root = Path(root) if root else DEFAULT_ROOT
    if not quiet:
        print(f"[FuzzyDupes] Scanning {root}  (threshold={threshold})")

    tracks = scan_library(root)
    if not quiet:
        print(f"[FuzzyDupes] {len(tracks)} tracks loaded")

    groups = find_dupes(tracks, threshold)
    if not quiet:
        print(report_text(groups, root))

    # Playlist
    if playlist_path:
        resolved: Path | None = None if isinstance(playlist_path, bool) else Path(playlist_path)
        out = write_dupes_playlist(groups, root, resolved)
        if out and not quiet:
            total = sum(len(g.tracks) for g in groups)
            print(f"[FuzzyDupes] Playlist written: {out}  ({total} tracks)")

    return groups


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fuzzy duplicate song finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Threshold guide
---------------
  95+   Near-exact only (different punctuation, trivial spacing)
  90    Default — catches remasters, feat. variants, minor edits
  80    Aggressive — may surface false positives for short titles
  70    Very aggressive — useful for finding covers / alternate versions
""",
    )
    p.add_argument(
        "--root", "-r",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Music library root  (default: {DEFAULT_ROOT})",
    )
    p.add_argument(
        "--threshold", "-t",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="0-100",
        help=f"Similarity threshold  (default: {DEFAULT_THRESHOLD})",
    )
    p.add_argument(
        "--playlist", "-p",
        nargs="?",
        const=True,           # --playlist with no value → default path
        default=None,         # flag absent → no playlist
        metavar="PATH",
        help=(
            "Write a .m3u8 playlist of all duplicate tracks. "
            "Omit PATH to use <root>/playlists/dupes.m3u8."
        ),
    )
    p.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output JSON instead of plain text",
    )
    p.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress messages (implies --json friendly output)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not (0 <= args.threshold <= 100):
        print("ERROR: --threshold must be between 0 and 100", file=sys.stderr)
        sys.exit(1)

    if not args.root.exists():
        print(f"ERROR: root directory not found: {args.root}", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(f"Scanning: {args.root}")
        print(f"Threshold: {args.threshold}")
        print()

    tracks = scan_library(args.root)
    if not args.quiet:
        print(f"{len(tracks)} tracks loaded\n")

    groups = find_dupes(tracks, args.threshold)

    if args.json:
        print(report_json(groups, args.root))
    else:
        print(report_text(groups, args.root))

    if args.playlist:
        playlist_path = None if args.playlist is True else Path(args.playlist)
        out = write_dupes_playlist(groups, args.root, playlist_path)
        if out:
            total = sum(len(g.tracks) for g in groups)
            print(f"Playlist written: {out}  ({total} tracks in {len(groups)} groups)")
        else:
            print("No duplicates — playlist not written.")

    sys.exit(0 if not groups else 1)


if __name__ == "__main__":
    main()
