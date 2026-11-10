#!/usr/bin/env python3
"""Keep a Spotify playlist that mirrors another playlist's full history.

The destination playlist is built in two layers:

1. The current source playlist order, exactly as it is on Spotify today.
2. Every track from Spotify Playlist Archive's cumulative history, newest
   first, minus anything already in layer 1.

Duplicates are removed with the same rule as JMPerez/spotify-dedup: same
Spotify ID, or same name + primary artist with a duration within 2 seconds.

Usage:
    python archive-playlists.py \
        --archive-playlist-id 37i9dQZF1DXdgz8ZB7c2CP \
        --destination-playlist-id 0d9fkV0DPxsq5Ag7IP8obL \
        --log logs/creamy-output.txt
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

from spotify_utils import (
    BATCH_SIZE,
    DuplicateTracker,
    Logger,
    Spotify,
    SpotifyError,
    Track,
    apply_moves,
    get_access_token,
    plan_moves,
    playlist_entry_track,
    remove_playlist_positions,
    track_from_spotify,
)

ARCHIVE_URL = (
    "https://raw.githubusercontent.com/mackorone/spotify-playlist-archive-2/"
    "refs/heads/main/playlists/{kind}/{playlist_id}.md"
)

TRACK_URL_RE = re.compile(r"https://open\.spotify\.com/track/([A-Za-z0-9]+)")
TRACK_LINK_RE = re.compile(
    r"\[(?P<title>.*?)\]\(https://open\.spotify\.com/track/[A-Za-z0-9]+\)"
)
ARTIST_LINK_RE = re.compile(
    r"\[(?P<name>[^\]]+)\]\(https://open\.spotify\.com/artist/[A-Za-z0-9]+\)"
)
LENGTH_RE = re.compile(r"^(?P<minutes>\d+):(?P<seconds>[0-5]\d)$")
DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})")
TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|?$")
MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|\"'])")

# Placeholder for playlist entries that are not tracks (episodes, local
# files, unavailable tracks). They cannot be managed, so they are pinned in
# place instead of being removed.
UNMANAGED_PREFIX = "@unmanaged:"


@dataclass(frozen=True)
class ArchiveEntry:
    track: Track
    added: date | None


# ============================================================
# MARKDOWN PARSING
# ============================================================


def fetch_text(url: str) -> str:
    print(f"Downloading {url}")
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def split_table_row(line: str) -> list[str]:
    """Split a markdown table row on unescaped pipes."""
    cells: list[str] = []
    current: list[str] = []
    escaped = False

    for char in line.strip():
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)

    cells.append("".join(current))

    if cells and not cells[0].strip():
        cells = cells[1:]

    if cells and not cells[-1].strip():
        cells = cells[:-1]

    return [cell.strip() for cell in cells]


def unescape_markdown(text: str) -> str:
    return MARKDOWN_ESCAPE_RE.sub(r"\1", text.strip())


def read_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Return the header and rows of the first markdown table in a file."""
    lines = [line for line in text.splitlines() if line.startswith("|")]

    if len(lines) < 2:
        return [], []

    header = [unescape_markdown(cell).lower() for cell in split_table_row(lines[0])]
    rows = [
        split_table_row(line)
        for line in lines[1:]
        if not TABLE_SEPARATOR_RE.match(line)
    ]

    return header, [row for row in rows if len(row) >= len(header)]


def column_index(header: list[str], *names: str) -> int | None:
    for name in names:
        if name in header:
            return header.index(name)

    return None


def parse_length(value: str) -> int:
    match = LENGTH_RE.match(value.replace("\\", "").strip())

    if not match:
        return 0

    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))

    return (minutes * 60 + seconds) * 1000


def parse_added(value: str) -> date | None:
    match = DATE_RE.match(value.strip())

    if not match:
        return None

    try:
        return datetime.strptime(match.group("date"), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_artists(cell: str) -> tuple[str, ...]:
    artists = tuple(
        unescape_markdown(match.group("name")) for match in ARTIST_LINK_RE.finditer(cell)
    )

    if artists:
        return artists

    # Fallback for unexpected formatting: strip links, then split on commas.
    plain = re.sub(r"\[[^\]]*\]\([^)]*\)", "", cell)
    return tuple(part.strip() for part in plain.split(",") if part.strip())


def parse_archive(text: str, kind: str) -> list[ArchiveEntry]:
    """Parse a cumulative or pretty archive file into entries."""
    header, rows = read_table(text)

    if not header:
        raise ValueError(f"The {kind} archive has no markdown table.")

    title_index = column_index(header, "title")
    artist_index = column_index(header, "artist(s)", "artists", "artist")
    length_index = column_index(header, "length", "duration")
    added_index = column_index(header, "added") if kind == "cumulative" else None

    if title_index is None or artist_index is None or length_index is None:
        raise ValueError(f"The {kind} archive is missing expected columns: {header}")

    entries: list[ArchiveEntry] = []
    skipped = 0

    for cells in rows:
        title_cell = cells[title_index]
        id_match = TRACK_URL_RE.search(title_cell)
        title_match = TRACK_LINK_RE.search(title_cell)

        if not id_match or not title_match:
            skipped += 1
            continue

        title = unescape_markdown(title_match.group("title"))
        added = parse_added(cells[added_index]) if added_index is not None else None

        if not title or (added_index is not None and added is None):
            skipped += 1
            continue

        entries.append(
            ArchiveEntry(
                track=Track(
                    id=id_match.group(1),
                    name=title,
                    artists=parse_artists(cells[artist_index]),
                    duration_ms=parse_length(cells[length_index]),
                ),
                added=added,
            )
        )

    suffix = f" ({skipped} row(s) skipped)" if skipped else ""
    print(f"  {kind} archive: {len(entries)} track row(s){suffix}")

    return entries


def read_history(playlist_id: str) -> list[Track]:
    """Return every archived track once, oldest addition first."""
    entries = parse_archive(
        fetch_text(ARCHIVE_URL.format(kind="cumulative", playlist_id=playlist_id)),
        "cumulative",
    )

    if not entries:
        return []

    entries.sort(key=lambda entry: entry.added)

    # The same track ID can be added, removed and added again. Keep the
    # newest occurrence so the history sorts by its latest appearance.
    newest_by_id: dict[str, ArchiveEntry] = {}

    for entry in reversed(entries):
        newest_by_id.setdefault(entry.track.id, entry)

    return [entry.track for entry in reversed(newest_by_id.values())]


def read_pretty(playlist_id: str) -> list[Track]:
    """Read the archive's snapshot of the current playlist order."""
    entries = parse_archive(
        fetch_text(ARCHIVE_URL.format(kind="pretty", playlist_id=playlist_id)),
        "pretty",
    )

    tracks: list[Track] = []
    seen: set[str] = set()

    for entry in entries:
        if entry.track.id in seen:
            continue

        seen.add(entry.track.id)
        tracks.append(entry.track)

    return tracks


# ============================================================
# PLAYLIST STATE
# ============================================================


def managed_track(entry: dict) -> Track | None:
    """Return the entry's track, or None when it cannot be managed."""
    return track_from_spotify(playlist_entry_track(entry))


def read_current_order(
    spotify: Spotify,
    playlist_id: str,
    logger: Logger,
) -> list[Track] | None:
    """Read the source playlist's live order, or None if it is unavailable."""
    try:
        entries = spotify.playlist_items(playlist_id, label="source playlist")
    except SpotifyError as error:
        logger.write(f"Live source playlist unavailable: {error}")
        return None

    return [track for track in (managed_track(entry) for entry in entries) if track]


def read_archive_playlist(
    spotify: Spotify,
    playlist_id: str,
) -> tuple[list[str], dict[int, str], int]:
    """Return the playlist as tokens, removable positions and unmanaged count."""
    entries = spotify.playlist_items(playlist_id, label="archive playlist")

    tokens: list[str] = []
    uri_by_position: dict[int, str] = {}
    unmanaged = 0

    for position, entry in enumerate(entries):
        track = managed_track(entry)

        if track is None:
            unmanaged += 1
            tokens.append(f"{UNMANAGED_PREFIX}{position}")
            continue

        tokens.append(track.id)
        uri_by_position[position] = (
            playlist_entry_track(entry) or {}
        ).get("uri") or f"spotify:track:{track.id}"

    return tokens, uri_by_position, unmanaged


def build_target(
    current: list[Track],
    history: list[Track],
) -> tuple[list[Track], Counter]:
    """Layer the current order on top of the history, dropping duplicates."""
    tracker = DuplicateTracker()
    stats: Counter = Counter()
    target: list[Track] = []

    for tracks in (current, list(reversed(history))):
        for track in tracks:
            reason = tracker.check(track)

            if reason:
                stats[reason] += 1
                continue

            tracker.add(track)
            target.append(track)

    return target, stats


def desired_with_unmanaged(
    working: list[str],
    target_ids: list[str],
) -> list[str]:
    """Insert unmanaged tokens back where they currently sit."""
    desired: list[str] = []
    index = 0

    for token in working:
        if token.startswith(UNMANAGED_PREFIX):
            desired.append(token)
            continue

        if index >= len(target_ids):
            raise RuntimeError(
                "More managed tracks in the playlist than in the target archive."
            )

        desired.append(target_ids[index])
        index += 1

    desired.extend(target_ids[index:])

    return desired


# ============================================================
# MAIN
# ============================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror a playlist's current order and full history."
    )
    parser.add_argument(
        "--archive-playlist-id",
        required=True,
        help="Spotify ID of the playlist being archived.",
    )
    parser.add_argument(
        "--destination-playlist-id",
        required=True,
        help="Spotify ID of the archive playlist to maintain.",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Optional log file path inside the repository.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without touching Spotify.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = Logger(args.log)
    started = time.perf_counter()

    logger.write("=" * 70)
    logger.write("Spotify Playlist Archive Sync")
    logger.write("Current playlist order on top, full history below")
    logger.write("Duplicates: same Spotify ID, or same name/artist/duration")
    logger.write(f"Started: {datetime.now(timezone.utc).isoformat()}")
    logger.write("=" * 70)

    if args.dry_run:
        logger.write("Dry run: no Spotify changes will be made.")

    try:
        logger.write("\nReading the archived history...")
        history = read_history(args.archive_playlist_id)

        if not history:
            raise RuntimeError(
                "The cumulative archive returned no tracks; refusing to modify "
                "the archive playlist."
            )

        logger.write(f"History tracks: {len(history)} unique Spotify IDs")

        logger.write("\nAuthenticating with Spotify...")
        spotify = Spotify(get_access_token())

        logger.write("\nReading the source playlist's current order...")
        current = read_current_order(spotify, args.archive_playlist_id, logger)

        if not current:
            logger.write("Falling back to the archive's pretty snapshot.")
            current = read_pretty(args.archive_playlist_id)

        if not current:
            raise RuntimeError(
                "The source playlist order is empty; refusing to modify the "
                "archive playlist."
            )

        logger.write(f"Current order tracks: {len(current)}")

        target, duplicate_stats = build_target(current, history)
        target_ids = [track.id for track in target]
        target_set = set(target_ids)
        duplicates = sum(duplicate_stats.values())

        logger.write(f"Target archive: {len(target)} unique tracks")
        logger.write(
            f"Duplicates dropped: {duplicates} "
            f"(same id: {duplicate_stats['same-id']}, "
            f"same name/artist/duration: {duplicate_stats['same-name-artist']})"
        )

        logger.write("\nReading the archive playlist...")
        tokens, uri_by_position, unmanaged = read_archive_playlist(
            spotify,
            args.destination_playlist_id,
        )
        logger.write(
            f"Archive playlist before: {len(tokens)} item(s)"
            + (f" ({unmanaged} unmanaged)" if unmanaged else "")
        )

        removal_positions: list[int] = []
        kept: set[str] = set()

        for position, token in enumerate(tokens):
            if token.startswith(UNMANAGED_PREFIX):
                continue

            if token in target_set and token not in kept:
                kept.add(token)
                continue

            removal_positions.append(position)

        snapshot_id = spotify.snapshot_id(args.destination_playlist_id)

        snapshot_id = remove_playlist_positions(
            spotify,
            args.destination_playlist_id,
            uri_by_position,
            removal_positions,
            snapshot_id=snapshot_id,
            logger=logger,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            removed = set(removal_positions)
            working = [
                token for position, token in enumerate(tokens)
                if position not in removed
            ]
        else:
            # Never trust a local guess here: whether Spotify honours per-item
            # positions differs between API versions, so look at the result.
            tokens, uri_by_position, unmanaged = read_archive_playlist(
                spotify,
                args.destination_playlist_id,
            )
            working = list(tokens)

        managed = [token for token in working if not token.startswith(UNMANAGED_PREFIX)]
        counts = Counter(managed)

        leftovers = [token for token in counts if token not in target_set]
        repeated = [token for token, count in counts.items() if count > 1]

        if leftovers or repeated:
            raise RuntimeError(
                "Spotify still contains playlist items that should have been "
                f"removed ({len(leftovers)} unexpected, {len(repeated)} repeated); "
                "aborting before changing the order."
            )

        present = set(counts)
        missing = [
            track for track in target if track.id not in present
        ]

        if missing:
            logger.write(f"Adding {len(missing)} missing track(s)...")
        else:
            logger.write("No tracks are missing from the archive playlist.")

        for start in range(0, len(missing), BATCH_SIZE):
            batch = missing[start : start + BATCH_SIZE]

            if not args.dry_run:
                snapshot_id = spotify.add_items(
                    args.destination_playlist_id,
                    [f"spotify:track:{track.id}" for track in batch],
                    snapshot_id=snapshot_id,
                )

            working.extend(track.id for track in batch)
            logger.write(f"  added {min(start + len(batch), len(missing))}/{len(missing)}")

        desired = desired_with_unmanaged(working, target_ids)
        moves = plan_moves(working, desired)

        logger.write(
            f"Reorder plan: {len(moves)} operation(s) for {len(desired)} item(s)."
        )

        snapshot_id, reorder_seconds = apply_moves(
            spotify,
            args.destination_playlist_id,
            moves,
            snapshot_id=snapshot_id,
            logger=logger,
            dry_run=args.dry_run,
        )

        if not args.dry_run:
            verify_archive(
                spotify,
                args.destination_playlist_id,
                target_ids,
                unmanaged,
                logger,
            )

        logger.write("\n" + "=" * 70)
        logger.write(f"Archive tracks: {len(target)}")
        logger.write(f"Items removed: {len(removal_positions)}")
        logger.write(f"Tracks added: {len(missing)}")
        logger.write(f"Reorder operations: {len(moves)}")
        logger.write(f"Reorder duration: {reorder_seconds:.2f} seconds")
        logger.write(
            f"Finished: {datetime.now(timezone.utc).isoformat()} "
            f"({time.perf_counter() - started:.1f}s total)"
        )

        if args.dry_run:
            logger.write("Dry run completed; nothing was changed on Spotify.")

        logger.write("=" * 70)

        return 0

    except Exception as error:  # noqa: BLE001 - surface any failure in the log
        logger.write("\n" + "=" * 70)
        logger.write("SYNC FAILED")
        logger.write("=" * 70)
        logger.write(str(error))
        return 1

    finally:
        logger.save()


def verify_archive(
    spotify: Spotify,
    playlist_id: str,
    target_ids: list[str],
    unmanaged: int,
    logger: Logger,
) -> None:
    """Re-read the playlist and confirm it matches the target exactly."""
    logger.write("\nVerifying the archive playlist...")
    entries = spotify.playlist_items(playlist_id, label="archive playlist")
    final_ids = [track.id for track in (managed_track(entry) for entry in entries) if track]

    if final_ids != target_ids:
        shared = min(len(final_ids), len(target_ids))
        mismatch = next(
            (
                index
                for index in range(shared)
                if final_ids[index] != target_ids[index]
            ),
            shared,
        )
        actual = final_ids[mismatch] if mismatch < len(final_ids) else "<missing>"
        expected = (
            target_ids[mismatch] if mismatch < len(target_ids) else "<none>"
        )

        raise RuntimeError(
            "Verification failed: the archive playlist does not match the "
            f"target order (first mismatch at position {mismatch + 1}; "
            f"expected {expected}, found {actual}; "
            f"{len(target_ids)} expected vs {len(final_ids)} found)."
        )

    if len(entries) != len(target_ids) + unmanaged:
        raise RuntimeError(
            "Verification failed: unexpected item count "
            f"({len(entries)} in the playlist vs "
            f"{len(target_ids) + unmanaged} expected)."
        )

    logger.write(f"Verification passed: {len(target_ids)} tracks in order.")


if __name__ == "__main__":
    raise SystemExit(main())
