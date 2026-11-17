#!/usr/bin/env python3
"""
YouTube Music Downloader
Downloads audio from music.youtube.com using yt-dlp with rich metadata,
and optionally enriches metadata via MusicBrainz.

Output path: <artist>/<album>/<artist1>, <artist2>, ... - <title>.opus

Dependencies:
    pip install yt-dlp mutagen requests musicbrainzngs python-dotenv Pillow
"""

import base64
import io
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import musicbrainzngs
import yt_dlp
from dotenv import load_dotenv
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.oggopus import OggOpus

import ntfy
import do_rsgain
import fuzzy_dupes
import check_archive

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MUSICBRAINZ_APP = "ytmusic-dl"
MUSICBRAINZ_VERSION = "1.0"
MUSICBRAINZ_CONTACT = os.environ.get("MUSICBRAINZ_CONTACT", "ytmusic-dl@localhost")

musicbrainzngs.set_useragent(MUSICBRAINZ_APP, MUSICBRAINZ_VERSION, MUSICBRAINZ_CONTACT)

IfExists = str   # "skip" | "overwrite" | "ask" | "rename"

AUDIO_EXTENSIONS = {".opus", ".mp3", ".flac", ".wav", ".m4a", ".ogg"}

PLAYLIST_URL_RE = re.compile(
    r"(?:list=|/playlist\?|/album/|/browse/)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# String / path helpers
# ---------------------------------------------------------------------------

def dedupe_list(lst: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in lst if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]


def clean_text(s: str) -> str:
    """
    Strip YouTube title noise from track/album names.

    Removes (in any bracket style) things like:
      - feat. / ft. credits
      - Official (Music/Lyric/Audio) Video, Official Visualizer
      - (prod. by …), (dir. by …)
      - Remastered / Remaster, Anniversary Edition, Deluxe Edition
      - (Audio), (Lyrics), (HQ), (HD), (4K), (Official), (Video)
      - [Official Video], [Music Video], [Visualizer], etc.
    """
    _NOISE = re.compile(
        r"""
        \s*                         # leading space
        [\(\[\{]                    # opening bracket (any style)
        \s*
        (?:
            feat(?:uring)?\.?\s+[^\)\]\}]+ |   # feat. / featuring …
            ft\.?\s+[^\)\]\}]+             |   # ft. …
            prod(?:uced)?\.?\s+by\s+[^\)\]\}]+ |  # prod. by …
            dir(?:ected)?\.?\s+by\s+[^\)\]\}]+ |  # dir. by …
            (?:official\s+)?(?:music\s+|lyric\s+|audio\s+|live\s+)?video |
            official\s+(?:audio|visuali[sz]er|clip|performance|stream) |
            (?:official\s+)?visuali[sz]er |
            official |
            audio |
            lyrics? |
            (?:\d+\s+)?remaster(?:ed)? |
            (?:\d+\s+)?reissue |
            (?:\d+(?:st|nd|rd|th)\s+)?anniversary\s+(?:edition|remaster(?:ed)?)? |
            (?:super\s+)?deluxe(?:\s+edition)? |
            (?:special|limited)\s+edition |
            bonus\s+track |
            explicit |
            clean\s+version |
            radio\s+edit |
            single\s+version |
            hd | hq | 4k | uhd
        )
        \s*
        [\)\]\}]                    # closing bracket
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    s = _NOISE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def strip_artist_prefix(title: str, artists: list[str]) -> str:
    """
    Remove a leading 'Artist - ' prefix from a title when the prefix matches
    one of the track's known artists (or a comma-joined combination of them).

    Handles separators: ' - ', ' – ' (en-dash), ' — ' (em-dash).
    Matches are case-insensitive.
    """
    _SEP = r"\s*[-\u2013\u2014]\s*"

    # Build candidates: each individual artist plus all comma-joined combos
    # e.g. ["Lady Gaga", "Bruno Mars"] → also tries "Lady Gaga, Bruno Mars"
    candidates = list(artists)
    if len(artists) > 1:
        candidates.append(", ".join(artists))

    for candidate in candidates:
        pattern = re.compile(
            r"^" + re.escape(candidate) + _SEP,
            re.IGNORECASE,
        )
        stripped = pattern.sub("", title, count=1)
        if stripped != title:
            return stripped.strip()
    return title


def sanitize(name: str) -> str:
    """Make a string safe for use as a filesystem path component."""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"[\s_]+", " ", name).strip(" .")
    return name or "Unknown"


def artists_str(artists: list[str]) -> str:
    return ", ".join(artists) if artists else "Unknown Artist"


def extract_artists(info: dict) -> list[str]:
    """Pull a deduplicated artist list from a yt-dlp info dict."""
    if info.get("artists"):
        return dedupe_list(info["artists"])
    if info.get("artist"):
        return dedupe_list(a.strip() for a in re.split(r"[,;&]", info["artist"]))
    if info.get("uploader"):
        return [info["uploader"].removesuffix(" - Topic").strip()]
    return ["Unknown Artist"]


def build_output_path(title: str, album: str, artists: list[str]) -> Path:
    """
    Construct:  <primary_artist>/<album>/<all_artists> - <title>.opus
    All inputs are assumed already cleaned (feat. stripped, sanitized).
    """
    primary  = sanitize(artists[0]) if artists else "Unknown Artist"
    all_str  = sanitize(artists_str([sanitize(a) for a in artists]))
    filename = f"{all_str} - {sanitize(title)}.opus"
    return Path(primary) / sanitize(album) / filename


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

def load_archive(archive_path: Path) -> dict[str, str]:
    """Return {video_id: relative_path} from the archive file."""
    if not archive_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in archive_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        result[parts[0]] = parts[1] if len(parts) == 2 else ""
    return result


def record_downloaded(archive_path: Path, video_id: str, rel_path: str) -> None:
    with archive_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{video_id}\t{rel_path}\n")


def _read_youtube_id(audio_path: Path) -> str:
    """
    Extract the youtube_id tag from an audio file, trying the appropriate
    mutagen class for each format.  Falls back to parsing a YouTube URL out
    of a comment/description tag.  Returns "" if nothing is found.
    """
    ext = audio_path.suffix.lower()
    vid = ""
    try:
        if ext == ".opus" or ext == ".ogg":
            audio = OggOpus(audio_path)
            vid = (audio.get("youtube_id") or [""])[0].strip()
            if not vid:
                comment = (audio.get("comment") or [""])[0].strip()
                if "youtube.com/watch" in comment:
                    cand = comment.split("?v=")[-1].split("&")[0]
                    vid = cand if re.match(r"^[a-zA-Z0-9_-]{11}$", cand) else ""
        elif ext == ".flac":
            audio = FLAC(audio_path)
            vid = (audio.get("youtube_id") or [""])[0].strip()
            if not vid:
                comment = (audio.get("comment") or audio.get("description") or [""])[0].strip()
                if "youtube.com/watch" in comment:
                    cand = comment.split("?v=")[-1].split("&")[0]
                    vid = cand if re.match(r"^[a-zA-Z0-9_-]{11}$", cand) else ""
        elif ext == ".mp3":
            try:
                tags = ID3(audio_path)
            except ID3NoHeaderError:
                return ""
            # Custom TXXX frame written as "youtube_id"
            txxx = tags.get("TXXX:youtube_id")
            if txxx:
                vid = txxx.text[0].strip() if txxx.text else ""
            if not vid:
                # Fall back to COMM (comment) frames
                for key, frame in tags.items():
                    if key.startswith("COMM"):
                        text = frame.text[0].strip() if frame.text else ""
                        if "youtube.com/watch" in text:
                            cand = text.split("?v=")[-1].split("&")[0]
                            vid = cand if re.match(r"^[a-zA-Z0-9_-]{11}$", cand) else ""
                            break
    except Exception:
        pass
    return vid


def reconcile_archive(archive_path: Path, output_root: Path) -> dict[str, str]:
    """
    Load the archive, then walk output_root for any audio files whose
    youtube_id tag is not yet recorded.  Backfills both the in-memory dict
    and the archive file so future runs are instant.

    This handles files that existed before the archive was introduced, were
    downloaded outside this tool, or whose path changed between runs (e.g.
    because MusicBrainz returned a different album name on a prior run).
    Supports .opus, .flac, and .mp3 files.
    """
    downloaded = load_archive(archive_path)
    known_paths = set(downloaded.values())

    new_entries: list[tuple[str, str]] = []

    for audio_path in output_root.rglob("*"):
        if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        rel = str(audio_path.relative_to(output_root))
        if rel in known_paths:
            continue
        vid = _read_youtube_id(audio_path)
        # print(f"[Archive] Found file: {rel} (ID: {vid})")
        if not vid or vid in downloaded:
            continue
        if not vid:
            print(f"[Archive] WARNING: No video ID found for {rel}, skipping.")
        downloaded[vid] = rel
        known_paths.add(rel)
        new_entries.append((vid, rel))
        print(f"[Archive] Backfilled from file: {vid} → {rel}")

    if new_entries:
        print(f"[Archive] Backfilling {len(new_entries)} untracked file(s)…")
        with archive_path.open("a", encoding="utf-8") as fh:
            for vid, rel in new_entries:
                fh.write(f"{vid}\t{rel}\n")

    return downloaded


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------

def rotate_session_log(output_root: Path) -> Path:
    """
    Rotate logs so we keep a history of 2 sessions:
      current.log  →  last.log   (overwriting any previous last.log)
      a fresh current.log is created and returned.
    """
    current = output_root / "current.log"
    last    = output_root / "last.log"
    if current.exists():
        current.replace(last)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    current.write_text(f"# Session started {timestamp}\n", encoding="utf-8")
    return current


def log_downloaded(log_path: Path, meta: dict, rel_path: str) -> None:
    """Append one line to the session log for a newly saved track."""
    artists = artists_str(meta.get("artists") or [])
    title   = meta.get("title", "Unknown Title")
    album   = meta.get("album", "")
    vid     = meta.get("youtube_id", "")
    line    = f"{artists} – {title} [{album}]  {vid}  → {rel_path}\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# ---------------------------------------------------------------------------
# URL classification and entry resolution
# ---------------------------------------------------------------------------

def is_playlist_url(url: str) -> bool:
    return bool(PLAYLIST_URL_RE.search(url))


class PlaylistResult:
    """Carries everything needed to write an m3u8 after processing."""
    __slots__ = ("title", "playlist_id", "ordered_ids", "entries")

    def __init__(self, title: str, playlist_id: str, ordered_ids: list[str], entries: list[dict]):
        self.title       = title        # human-readable playlist name
        self.playlist_id = playlist_id  # YouTube playlist/album ID
        self.ordered_ids = ordered_ids  # video IDs in playlist order (all, incl. archived)
        self.entries     = entries      # fully-resolved info dicts for new tracks only


def resolve_entries(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    downloaded_ids: dict[str, str],
    output_root: Path,
    archive_path: Path,
) -> tuple[list[dict], PlaylistResult | None]:
    """
    For a playlist URL: extract flat (metadata only, no per-video requests),
    filter out already-archived IDs, then fetch full info only for new entries.
    Returns (entries_to_process, PlaylistResult).

    For a single video URL: fetch full info directly.
    Returns (entries_to_process, None).
    """
    if not is_playlist_url(url):
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:
            print(f"  [ERROR] extract_info failed for {url}: {exc}", file=sys.stderr)
            return [], None
        return ([info] if info else []), None

    # ── Playlist: flat extraction first ──────────────────────────────────────
    print(f"  [Playlist] Flat-extracting entries from {url}")
    flat_opts = dict(ydl.params)
    flat_opts["extract_flat"] = "in_playlist"
    flat_opts["quiet"] = True

    try:
        with yt_dlp.YoutubeDL(flat_opts) as flat_ydl:
            playlist_info = flat_ydl.extract_info(url, download=False)
    except Exception as exc:
        print(f"  [ERROR] Flat extraction failed for {url}: {exc}", file=sys.stderr)
        return [], None

    if not playlist_info:
        return [], None

    flat_entries = playlist_info.get("entries") or []
    playlist_title = playlist_info.get("title") or playlist_info.get("id") or "playlist"
    playlist_id    = playlist_info.get("id") or sanitize(playlist_title)
    print(f"  [Playlist] '{playlist_title}' — {len(flat_entries)} entries")

    # Preserve ordered IDs for m3u8; split into archived vs. needs-fetching
    ordered_ids: list[str] = []
    to_fetch:    list[str] = []
    skipped = 0

    for entry in flat_entries:
        if not entry:
            continue
        vid = entry.get("id", "")
        if not vid:
            continue
        ordered_ids.append(vid)
        if vid in downloaded_ids:
            skipped += 1
            continue
        webpage_url = entry.get("url") or entry.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={vid}"
        )
        to_fetch.append(webpage_url)

    unavailable = sum(1 for v in ordered_ids if downloaded_ids.get(v) == "UNAVAILABLE")
    print(f"  [Playlist] {skipped} skipped ({unavailable} unavailable), {len(to_fetch)} to fetch")

    resolved: list[dict] = []
    for video_url in to_fetch:
        try:
            info = ydl.extract_info(video_url, download=False)
            if info:
                resolved.append(info)
        except Exception as exc:
            print(f"  [ERROR] extract_info failed for {video_url}: {exc}", file=sys.stderr)

    playlist_result = PlaylistResult(
        title=playlist_title,
        playlist_id=playlist_id,
        ordered_ids=ordered_ids,
        entries=resolved,
    )
    return resolved, playlist_result


# ---------------------------------------------------------------------------
# M3U8 playlist writing
# ---------------------------------------------------------------------------

def write_m3u8(
    playlist: PlaylistResult,
    downloaded_ids: dict[str, str],
    output_root: Path,
) -> None:
    """
    Write (or overwrite) an extended M3U playlist at:
        <output_root>/playlists/<sanitized_title>.m3u8

    Tracks are listed in the original playlist order.  Only entries whose
    video ID maps to a known relative path (via the archive) are included;
    unavailable or not-yet-downloaded tracks are silently omitted.

    Paths in the file are relative to the playlists/ directory so the whole
    music tree can be moved without breaking the playlist.
    """
    playlists_dir = output_root / "playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    m3u8_path = playlists_dir / f"{sanitize(playlist.title)}.m3u8"

    lines: list[str] = ["#EXTM3U", f"#PLAYLIST:{playlist.title}", ""]

    included = 0
    for vid in playlist.ordered_ids:
        rel_path = downloaded_ids.get(vid)
        if not rel_path or rel_path == "UNAVAILABLE":
            continue  # not downloaded, unavailable, or permanently skipped

        # Path relative to the playlists/ dir: go up one level then follow rel_path
        track_path = Path("..") / rel_path

        # Read display info from tags if available, fall back to filename
        abs_path = output_root / rel_path
        extinf = _extinf_for_track(abs_path)
        lines.append(extinf)
        lines.append(track_path.as_posix())
        lines.append("")
        included += 1

    m3u8_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [M3U8] Written: {m3u8_path} ({included} tracks)")


def _extinf_for_track(path: Path) -> str:
    """Return an #EXTINF line for a track, reading tags when possible."""
    duration = -1
    display  = path.stem   # fallback: filename without extension

    if path.exists():
        try:
            audio = OggOpus(path)
            # Duration from mutagen is in seconds (float)
            duration = int(getattr(audio.info, "length", -1))
            title   = (audio.get("title")   or [""])[0].strip()
            artists = audio.get("artist")   or []
            artist  = ", ".join(a.strip() for a in artists if a.strip())
            if title and artist:
                display = f"{artist} - {title}"
            elif title:
                display = title
        except Exception:
            pass

    return f"#EXTINF:{duration},{display}"


# ---------------------------------------------------------------------------
# MusicBrainz enrichment
# ---------------------------------------------------------------------------

# Preferred release countries in priority order.
# XW = "Worldwide" (digital releases), US = United States.
_PREFERRED_COUNTRIES = ("XW", "US")


def _pick_release(releases: list[dict]) -> dict:
    """
    From a MusicBrainz release-list, return the best match by country.

    Priority:
      1. First release whose country is in _PREFERRED_COUNTRIES
         (XW before US, matching the order of _PREFERRED_COUNTRIES)
      2. First release in the list (MusicBrainz default ordering)

    Country may appear as the top-level "country" field or inside
    "release-event-list" entries.
    """
    def release_countries(rel: dict) -> set[str]:
        countries: set[str] = set()
        if rel.get("country"):
            countries.add(rel["country"].upper())
        for event in rel.get("release-event-list", []):
            area = event.get("area", {})
            iso = area.get("iso-3166-1-code-list", [])
            countries.update(c.upper() for c in iso)
        return countries

    for preferred in _PREFERRED_COUNTRIES:
        for rel in releases:
            if preferred in release_countries(rel):
                return rel

    return releases[0]


def mb_search(title: str, artists: list[str], album: str | None = None) -> dict:
    """
    Query MusicBrainz for a recording and return any extra metadata found.
    Returns a (possibly empty) dict with keys like: isrc, mb_recording_id,
    mb_release_id, mb_artist_ids, label, catalog_number, date, genres, album.
    """
    extra: dict = {}
    query_parts = [f'recording:"{title}"']
    if artists:
        query_parts.append(f'artist:"{artists[0]}"')
    if album and album != "":
        query_parts.append(f'release:"{album}"')

    query = " AND ".join(query_parts)
    print(f"  [MusicBrainz] Query: {query}")
    try:
        result = musicbrainzngs.search_recordings(query=query, limit=5)
    except musicbrainzngs.WebServiceError as exc:
        print(f"  [MusicBrainz] Search failed: {exc}", file=sys.stderr)
        return extra


    recordings = result.get("recording-list", [])
    if not recordings:
        return extra

    # test
    # import json
    # with open("/music/mb_debug.json", "w", encoding="utf-8") as fh:
    #     fh.write(json.dumps(recordings, indent=2))
    # exit()

    rec = recordings[0]
    found = False
    for r in recordings:
        releases = r.get("release-list", [])
        for rel in releases:
            if rel.get("status", "").lower() == "official":
                rec = r
                found = True
                break
        if found:
            break


    release = rec.get("release-list", [])[0] if rec.get("release-list") else None
    for rel in rec.get("release-list", []):
        if rel.get("country", "").upper() in _PREFERRED_COUNTRIES:
            release = rel

    extra["mb_recording_id"] = rec.get("id", "")

    isrcs = rec.get("isrc-list", [])
    if isrcs:
        extra["isrc"] = isrcs[0]

    if release:
        rel = release
        extra["mb_release_id"] = rel.get("id", "")
        extra.setdefault("album", rel.get("title", ""))
        extra.setdefault("date",  rel.get("date", ""))

        label_info = rel.get("label-info-list", [])
        if label_info:
            li = label_info[0]
            label = li.get("label", {})
            if label.get("name"):
                extra["label"] = label["name"]
            if li.get("catalog-number"):
                extra["catalog_number"] = li["catalog-number"]

    artist_credits = rec.get("artist-credit", [])
    mb_artist_ids = [
        ac["artist"]["id"]
        for ac in artist_credits
        if isinstance(ac, dict) and "artist" in ac
    ]
    if mb_artist_ids:
        extra["mb_artist_ids"] = mb_artist_ids

    tags = rec.get("tag-list", [])
    if tags:
        extra["genres"] = [
            t["name"]
            for t in sorted(tags, key=lambda t: -int(t.get("count", 0)))
        ][:5]

    if not extra.get("genres") and releases:
        extra["genres"] = _genres_from_release_group(releases)

    return extra


def _genres_from_release_group(releases: list[dict]) -> list[str]:
    seen_rg_ids: set[str] = set()
    for rel in releases:
        rg = rel.get("release-group", {})
        rg_id = rg.get("id", "")
        if not rg_id or rg_id in seen_rg_ids:
            continue
        seen_rg_ids.add(rg_id)

        rg_tags = rg.get("tag-list", [])
        if not rg_tags:
            try:
                time.sleep(0.5)
                rg_result = musicbrainzngs.get_release_group_by_id(rg_id, includes=["tags"])
                rg_tags = rg_result.get("release-group", {}).get("tag-list", [])
            except musicbrainzngs.WebServiceError as exc:
                print(f"  [MusicBrainz] Release-group lookup failed: {exc}", file=sys.stderr)
                continue

        if rg_tags:
            names = [t["name"] for t in sorted(rg_tags, key=lambda t: -int(t.get("count", 0)))]
            print(f"  [MusicBrainz] Tags from release-group {rg_id}: {names[:5]}")
            return names[:5]

    return []


# ---------------------------------------------------------------------------
# File-exists / destination resolution
# ---------------------------------------------------------------------------

def _artist_folder_has_track(dest: Path, output_root: Path) -> Path | None:
    """
    Search all audio files directly inside the artist folder (and its album
    sub-folders) for a filename stem matching dest's stem.  Returns the first
    matching path, or None if no match is found.

    The artist folder is the first component of dest relative to output_root
    (i.e.  output_root/<artist>/…).  We walk every album sub-folder one level
    deep — this covers the normal  artist/album/song.opus  layout without
    recursing arbitrarily deep.
    """
    try:
        rel = dest.relative_to(output_root)
    except ValueError:
        return None

    artist_dir = output_root / rel.parts[0]
    if not artist_dir.is_dir():
        # Fall back to a case-insensitive search among existing subdirectories.
        # This handles the common case where the folder already exists under a
        # different capitalisation (e.g. "taylor swift" vs "Taylor Swift").
        target_name = rel.parts[0].lower()
        artist_dir = next(
            (d for d in output_root.iterdir() if d.is_dir() and d.name.lower() == target_name),
            None,
        )
        if artist_dir is None:
            return None

    target_stem = dest.stem.lower()

    # Collect every audio file under artist_dir (all depths, keeps it simple)
    for candidate in artist_dir.rglob("*"):
        if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if candidate.stem.lower() == target_stem:
            return candidate

    return None


def resolve_dest(dest: Path, if_exists: IfExists, output_root: Path | None = None) -> Path | None:
    """Return the final path to write to, or None to skip this track."""
    # Check across the whole artist folder (any album subfolder), not just the
    # exact destination path.  This avoids re-downloading a track that already
    # exists under a different album name.
    if output_root is not None:
        existing = _artist_folder_has_track(dest, output_root)
        if existing is not None:
            if if_exists == "skip":
                print(f"  [SKIP] Already exists in artist folder: {existing}")
                return existing  # caller will archive this path, then skip
            # For other if_exists modes fall through to the exact-path logic below
    elif not dest.exists():
        return dest

    if not dest.exists():
        return dest

    if if_exists == "skip":
        print(f"  [SKIP] Already exists: {dest}")
        return 

    # if if_exists == "overwrite":
    #     print(f"  [OVERWRITE] Replacing: {dest}")
    #     dest.unlink()
    #     return dest
    #
    # if if_exists == "ask":
    #     while True:
    #         choice = input(
    #             f"\n  File exists: {dest}\n"
    #             "  [s]kip / [o]verwrite / [r]ename? "
    #         ).strip().lower()
    #         if choice in ("s", "skip"):
    #             return None
    #         if choice in ("o", "overwrite"):
    #             dest.unlink()
    #             return dest
    #         if choice in ("r", "rename"):
    #             break
    #
    # # rename: append (2), (3), … until free
    # stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    # counter = 2
    # while True:
    #     candidate = parent / f"{stem} ({counter}){suffix}"
    #     if not candidate.exists():
    #         print(f"  [RENAME] Saving as: {candidate}")
    #         return candidate
    #     counter += 1


# ---------------------------------------------------------------------------
# Processing a single resolved entry
# ---------------------------------------------------------------------------

def _process_entry(
    ydl: yt_dlp.YoutubeDL,
    info: dict,
    tmp_dir: Path,
    output_root: Path,
    no_musicbrainz: bool,
    if_exists: IfExists,
    archive_path: Path,
    downloaded_ids: dict[str, str],
    log_path: Path,
    newly_downloaded: list[str] | None = None,
) -> None:
    video_id = info.get("id", "unknown")
    print(f"\n  Track: {info.get('title', video_id)}")

    # ── Archive check ─────────────────────────────────────────────────────────
    # (For playlists this is already done in resolve_entries; single-video URLs
    #  land here directly, so we check once more.)
    if if_exists == "skip" and video_id in downloaded_ids:
        print(f"  [SKIP] ID in archive: {video_id}")
        return

    # ── Build preliminary metadata ────────────────────────────────────────────
    artists = extract_artists(info)
    title   = clean_text(info.get("title") or "Unknown Title")
    title   = strip_artist_prefix(title, artists)
    album   = clean_text(
        info.get("album") or info.get("playlist_title") or ""
    )

    meta = {
        "title":        title,
        "artists":      artists,
        "album":        album,
        "album_artist": info.get("album_artist") or (artists[0] if artists else ""),
        "date":         str(info.get("release_year") or info.get("upload_date", "")[:4]),
        "track":        str(info.get("track_number") or ""),
        "disc":         str(info.get("disc_number") or ""),
        "genres":       info.get("genres") or ([info["genre"]] if info.get("genre") else []),
        "description":  info.get("description", ""),
        "comment":      info.get("webpage_url", ""),
        "youtube_id":           video_id,
        "youtube_channel_id":   info.get("channel_id", ""),
        "youtube_playlist_id":  info.get("playlist_id", ""),
        "youtube_view_count":   str(info.get("view_count") or ""),
        "youtube_like_count":   str(info.get("like_count") or ""),
    }

    # ── Pre-download existence check (preliminary path, before MusicBrainz) ──
    pre_dest = output_root / build_output_path(meta["title"], meta["album"], meta["artists"])
    existing = _artist_folder_has_track(pre_dest, output_root)
    if existing is not None:
        rel = str(existing.relative_to(output_root))
        if video_id not in downloaded_ids:
            record_downloaded(archive_path, video_id, rel)
            downloaded_ids[video_id] = rel
            print(f"  [SKIP] Found before download (pre-MB): {existing}  →  archived")
        else:
            print(f"  [SKIP] Found before download (pre-MB): {existing}")
        return

    # ── MusicBrainz enrichment ────────────────────────────────────────────────
    if not no_musicbrainz:
        print("  [MusicBrainz] Searching…")
        mb_extra = mb_search(meta["title"], artists, meta["album"])
        if mb_extra:
            print(f"  [MusicBrainz] Found: {mb_extra}")
            for k, v in mb_extra.items():
                if k == "genres":
                    if not meta["genres"]:
                        meta["genres"] = v
                elif k == "album" and meta["album"] == "" and v:
                    meta["album"] = sanitize(v)
                elif k == "date" and not meta["date"] and v:
                    meta["date"] = v[:4]
                else:
                    meta[k] = str(v) if not isinstance(v, list) else ", ".join(v)
        else:
            print("  [MusicBrainz] No match found.")
        time.sleep(1)

    # ── Post-MusicBrainz existence check (album name may have changed) ────────
    raw_dest = output_root / build_output_path(meta["title"], meta["album"], meta["artists"])
    if raw_dest != pre_dest:
        # MB changed the path — check again with the corrected album name
        existing = _artist_folder_has_track(raw_dest, output_root)
        if existing is not None:
            rel = str(existing.relative_to(output_root))
            if video_id not in downloaded_ids:
                record_downloaded(archive_path, video_id, rel)
                downloaded_ids[video_id] = rel
                print(f"  [SKIP] Found before download (post-MB): {existing}  →  archived")
            else:
                print(f"  [SKIP] Found before download (post-MB): {existing}")
            return

    # ── Download ──────────────────────────────────────────────────────────────
    _PERMANENT_ERRORS = re.compile(
        r"Video unavailable|This video is not available|"
        r"Private video|This video has been removed|"
        r"age.?restricted|Sign in to confirm your age|"
        r"members.only|This video is available to this channel|"
        r"has been terminated|account associated with this video",
        re.IGNORECASE,
    )

    try:
        ydl.download([info["webpage_url"]])
    except Exception as exc:
        msg = str(exc)
        print(f"  [ERROR] Download failed: {msg}", file=sys.stderr)
        if _PERMANENT_ERRORS.search(msg):
            record_downloaded(archive_path, video_id, "UNAVAILABLE")
            downloaded_ids[video_id] = "UNAVAILABLE"
            print(f"  [Archive] Marked as UNAVAILABLE: {video_id}")
        return

    opus_tmp = tmp_dir / f"{video_id}.opus"
    if not opus_tmp.exists():
        # yt-dlp swallowed the error (ignoreerrors=True) — check its output
        # for known permanent failures by inspecting the info dict flags.
        reason = (
            info.get("availability")        # e.g. "needs_auth", "premium_only"
            or info.get("live_status")      # e.g. "is_upcoming"
        )
        _PERMANENT_AVAILABILITY = {"needs_auth", "premium_only", "subscriber_only", "unavailable"}
        if reason in _PERMANENT_AVAILABILITY:
            record_downloaded(archive_path, video_id, "UNAVAILABLE")
            downloaded_ids[video_id] = "UNAVAILABLE"
            print(f"  [Archive] Marked as UNAVAILABLE ({reason}): {video_id}")
        else:
            print(f"  [WARN] Expected file not found after download: {opus_tmp}", file=sys.stderr)
        return

    # ── Build final destination ───────────────────────────────────────────────
    print(f"  Destination: {raw_dest}")
    raw_dest.parent.mkdir(parents=True, exist_ok=True)

    dest = resolve_dest(raw_dest, if_exists, output_root)

    # resolve_dest returns an existing path (≠ raw_dest) when the track was
    # found via the artist-folder scan.  Archive it so future runs skip via
    # the fast ID check instead of re-scanning the folder.
    if dest is not None and dest.exists() and dest != raw_dest:
        rel = str(dest.relative_to(output_root))
        if video_id not in downloaded_ids:
            record_downloaded(archive_path, video_id, rel)
            downloaded_ids[video_id] = rel
            print(f"  [Archive] Recorded existing file: {rel}")
        for leftover in tmp_dir.glob(f"{video_id}.*"):
            leftover.unlink(missing_ok=True)
        return

    if raw_dest.exists():
        rel = str(raw_dest.relative_to(output_root))
        record_downloaded(archive_path, video_id, rel)
        downloaded_ids[video_id] = rel

    if dest is None:
        for leftover in tmp_dir.glob(f"{video_id}.*"):
            leftover.unlink(missing_ok=True)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    # opus_tmp.rename(dest)
    shutil.move(str(opus_tmp), str(dest))
    print(f"  Saved → {dest}")

    rel = str(dest.relative_to(output_root))
    record_downloaded(archive_path, video_id, rel)
    downloaded_ids[video_id] = rel
    log_downloaded(log_path, meta, rel)

    if newly_downloaded is not None:
        artists_label = artists_str(meta.get("artists") or [])
        title_label   = meta.get("title", "Unknown Title")
        newly_downloaded.append(f"{artists_label} – {title_label}")

    # ── Tag and clean up ──────────────────────────────────────────────────────
    _tag_opus(dest, meta, tmp_dir, video_id)

    for leftover in tmp_dir.glob(f"{video_id}.*"):
        leftover.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cover art helpers
# ---------------------------------------------------------------------------

def _square_crop(image_bytes: bytes) -> bytes:
    """Center-crop an image to square and return as JPEG bytes."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    size = min(w, h)
    left = (w - size) // 2
    top  = (h - size) // 2
    img  = img.crop((left, top, left + size, top + size))
    out  = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def fetch_cover_art(mb_release_id: str) -> bytes | None:
    """
    Fetch front cover art from the Cover Art Archive.
    Returns raw image bytes, or None if unavailable.
    """
    import requests as _requests
    url = f"https://coverartarchive.org/release/{mb_release_id}/front-500"
    try:
        r = _requests.get(url, timeout=10, allow_redirects=True)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            print(f"  [CoverArt] Fetched from Cover Art Archive ({len(r.content)//1024} KB)")
            return r.content
        print(f"  [CoverArt] CAA returned {r.status_code} for release {mb_release_id}")
    except Exception as exc:
        print(f"  [CoverArt] Fetch failed: {exc}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Opus tagging
# ---------------------------------------------------------------------------

def _tag_opus(dest: Path, meta: dict, tmp_dir: Path, video_id: str) -> None:
    try:
        audio = OggOpus(dest)
    except Exception as exc:
        print(f"  [WARN] Could not open for tagging: {exc}", file=sys.stderr)
        return

    def set_tag(key: str, value: str) -> None:
        if value and value.strip():
            audio[key] = [value.strip()]

    set_tag("title",       meta["title"])
    set_tag("album",       meta["album"])
    set_tag("albumartist", meta["album_artist"])
    set_tag("date",        meta["date"])
    set_tag("tracknumber", meta["track"])
    set_tag("discnumber",  meta["disc"])
    set_tag("comment",     meta["comment"])
    set_tag("description", meta["description"][:500])

    if meta["artists"]:
        audio["artist"] = meta["artists"]
    if meta["genres"]:
        audio["genre"] = meta["genres"]

    for key in (
        "youtube_id", "youtube_channel_id", "youtube_playlist_id",
        "youtube_view_count", "youtube_like_count",
        "isrc", "label", "catalog_number",
        "mb_recording_id", "mb_release_id", "mb_artist_ids",
    ):
        val = meta.get(key, "")
        if val:
            set_tag(key, str(val))

    # ── Cover art: CAA first, cropped thumbnail fallback ─────────────────────
    cover_bytes: bytes | None = None

    mb_release_id = meta.get("mb_release_id", "")
    if mb_release_id:
        cover_bytes = fetch_cover_art(mb_release_id)

    if not cover_bytes:
        thumb_path = _find_thumbnail(tmp_dir, video_id)
        if thumb_path:
            try:
                cover_bytes = _square_crop(thumb_path.read_bytes())
                print("  [CoverArt] Using cropped YouTube thumbnail")
            except Exception as exc:
                print(f"  [WARN] Thumbnail crop failed: {exc}", file=sys.stderr)

    if cover_bytes:
        try:
            pic = Picture()
            pic.type = 3
            pic.desc = "Cover"
            pic.data = cover_bytes
            pic.mime = "image/jpeg"
            audio["metadata_block_picture"] = [
                base64.b64encode(pic.write()).decode("ascii")
            ]
        except Exception as exc:
            print(f"  [WARN] Cover art embedding failed: {exc}", file=sys.stderr)

    audio.save()
    print(f"  Tagged: {list(audio.keys())}")


def _find_thumbnail(tmp_dir: Path, video_id: str) -> Path | None:
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        p = tmp_dir / f"{video_id}{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Main download orchestration
# ---------------------------------------------------------------------------

def download(
    urls: list[str],
    output_root: Path,
    no_musicbrainz: bool = False,
    if_exists: IfExists = "skip",
    po_token: str | None = None,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    bgutil_url: str | None = None,
) -> None:
    archive_path = output_root / "downloaded.txt"
    downloaded_ids = reconcile_archive(archive_path, output_root)
    print(f"[Archive] {len(downloaded_ids)} IDs loaded from {archive_path}")

    log_path = rotate_session_log(output_root)
    print(f"[Log] Session log: {log_path}")

    newly_downloaded: list[str] = []

    tmp_dir = output_root / ".tmp_ytdl"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts: dict = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "0",
            },
        ],
        "writethumbnail": True,
        "embedthumbnail": False,
        "writeinfojson": True,
        "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
        "paths": {"temp": str(tmp_dir)},
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": True,
        "extract_flat": False,
        "sleep_interval": 1,
        "max_sleep_interval": 5,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_music", "mweb"],
                **({"po_token": [f"mweb.gvs+{po_token}"]} if po_token else {}),
            },
            **({"youtubepot-bgutilhttp": {"base_url": [bgutil_url]}} if bgutil_url else {}),
        },
    }

    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for url in urls:
            print(f"\n{'='*60}\nProcessing: {url}\n{'='*60}")
            entries, playlist = resolve_entries(ydl, url, downloaded_ids, output_root, archive_path)
            for entry in entries:
                _process_entry(
                    ydl, entry, tmp_dir, output_root,
                    no_musicbrainz, if_exists, archive_path, downloaded_ids, log_path,
                    newly_downloaded,
                )
            if playlist is not None:
                write_m3u8(playlist, downloaded_ids, output_root)

    # ── ntfy notification ─────────────────────────────────────────────────────
    count = len(newly_downloaded)
    if count == 0:
        print("\nNo new songs were downloaded.")
        # ntfy_title = "Download complete – no new songs"
        # ntfy_body  = "All tracks were already in the archive."
    else:
        ntfy_title = f"Downloaded {count} song{'s' if count != 1 else ''}"
        preview    = newly_downloaded[:5]
        lines      = [f"{i+1}. {song}" for i, song in enumerate(preview)]
        if count > 5:
            lines.append(f"… and {count - 5} more")
        ntfy_body = "\n".join(lines)

        ntfy.send_ntfy_notification(ntfy_title, ntfy_body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    # All configuration via environment variables (or .env file).
    #
    # Required:
    #   YTDL_URLS            Space-separated YouTube Music URLs
    #
    # Optional:
    #   YTDL_OUTPUT_DIR      Root output directory          (default: /music)
    #   YTDL_IF_EXISTS       skip|overwrite|rename          (default: skip)
    #   YTDL_NO_MUSICBRAINZ  1 to disable MusicBrainz       (default: off)
    #   YTDL_PO_TOKEN        Manual GVS PO Token for mweb client
    #   YTDL_COOKIES         Path to Netscape cookies file
    #   YTDL_BGUTIL_URL      bgutil HTTP server URL         (default: http://bgutil-provider:4416)
    #   MUSICBRAINZ_CONTACT  Email for MB user-agent string

    try:
        # OLD
        # raw_urls = os.environ.get("YTDL_URLS", "").strip()
        # if not raw_urls:
        #     print("ERROR: YTDL_URLS is not set. Nothing to do.", file=sys.stderr)
        #     sys.exit(1)
        #
        # output_root = Path(os.environ.get("YTDL_OUTPUT_DIR", "/music")).expanduser().resolve()
        # output_root.mkdir(parents=True, exist_ok=True)


        # NEW
        # OUTPUT_DIRS is a space-separated list of directories to process in order.
        # at root of each directory, there should be a "urls.txt" file containing the URLs to download.
        # newline separated, one URL per line.  Blank lines and lines starting with # are ignored.
        # if urls.txt is missing or empty, that directory download is skipped, however other functions still run.
        output_dirs = os.environ.get("OUTPUT_DIRS", "/music").strip().split()
        output_dirs = [Path(d).expanduser().resolve() for d in output_dirs]

        for output_root in output_dirs:
            if not output_root.exists():
                print(f"Output directory does not exist: {output_root}. Skipping.", file=sys.stderr)
            urls_file = output_root / "urls.txt"
            if urls_file.exists():
                with urls_file.open("r", encoding="utf-8") as f:
                    raw_urls = f.read().strip()
                    process_urls = [line.strip() for line in raw_urls.splitlines() if line.strip() and not line.strip().startswith("#")]

                download(
                    urls=process_urls,
                    output_root=output_root,
                    no_musicbrainz=os.environ.get("YTDL_NO_MUSICBRAINZ", "").strip() == "1",
                    if_exists=os.environ.get("YTDL_IF_EXISTS", "skip").strip(),
                    po_token=os.environ.get("YTDL_PO_TOKEN") or None,
                    cookies=os.environ.get("YTDL_COOKIES") or None,
                    cookies_from_browser=None,
                    bgutil_url=os.environ.get("YTDL_BGUTIL_URL", "http://bgutil-provider:4416").strip(),
                )

            print("Running rsgain...")
            do_rsgain.do_rsgain(output_root)

            # print("Running fuzzy_dupes...")
            # groups = fuzzy_dupes.get_dupes(threshold=90, playlist_path=Path("/music/playlists/dupes.m3u8"))

            removed_ids = check_archive.check_archive(output_root)

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        title = "EXCEPTION in Spotify Sync"
        ntfy.send_ntfy_notification(title, str(e))

    print("\nDone.")


if __name__ == "__main__":
    main()
