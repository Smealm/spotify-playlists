#!/usr/bin/env python3
"""Sort a Spotify playlist so the newest additions come first.

Duplicate entries are removed first, using the same rule as
JMPerez/spotify-dedup: same Spotify ID, or same name + primary artist with a
duration within 2 seconds. Entries are then ordered by their ``added_at``
timestamp, newest first.

Usage:
    python reorder-playlists.py \
        --destination-playlist-id 5sahCBpr8p5Bzte0QgB4gh \
        --log logs/what-is-this-output.txt
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime, timezone

from spotify_utils import (
    BATCH_SIZE,
    Logger,
    Spotify,
    apply_moves,
    find_duplicates,
    get_access_token,
    plan_moves,
    playlist_entry_track,
    remove_playlist_positions,
    track_from_spotify,
)

UNMANAGED_PREFIX = "@unmanaged:"
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_added_at(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reorder a playlist so the newest additions come first."
    )
    parser.add_argument(
        "--destination-playlist-id",
        required=True,
        help="Spotify ID of the playlist to reorder.",
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


def read_playlist(spotify: Spotify, playlist_id: str) -> list[dict]:
    """Return every playlist entry with its position, timestamp and track."""
    entries = spotify.playlist_items(playlist_id, label="playlist")
    items: list[dict] = []

    for position, entry in enumerate(entries):
        raw = playlist_entry_track(entry)
        track = track_from_spotify(raw)

        items.append(
            {
                "position": position,
                # Unique and stable for the reorder planner.
                "token": track.id if track else f"{UNMANAGED_PREFIX}{position}",
                "added_at": parse_added_at(entry.get("added_at")),
                "track": track,
                "uri": (raw or {}).get("uri")
                or (f"spotify:track:{track.id}" if track else None),
            }
        )

    return items


def newest_first(items: list[dict]) -> list[dict]:
    """Sort by added_at, newest first. Python's stable sort keeps ties in place."""
    return sorted(
        items,
        key=lambda item: item["added_at"] or EPOCH,
        reverse=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = Logger(args.log)
    started = time.perf_counter()

    logger.write("=" * 70)
    logger.write("Spotify Playlist Reorder")
    logger.write("Newest added -> oldest added, duplicates removed")
    logger.write(f"Started: {datetime.now(timezone.utc).isoformat()}")
    logger.write("=" * 70)

    if args.dry_run:
        logger.write("Dry run: no Spotify changes will be made.")

    try:
        logger.write("\nAuthenticating with Spotify...")
        spotify = Spotify(get_access_token())

        playlist = spotify.playlist(args.destination_playlist_id)
        owner = (playlist.get("owner") or {}).get("display_name") or "Unknown"

        logger.write("Authentication successful.")
        logger.write("")
        logger.write(f"Playlist: {playlist.get('name', 'Unknown playlist')}")
        logger.write(f"Owner: {owner}")
        logger.write(f"Playlist ID: {args.destination_playlist_id}")

        logger.write("\nReading playlist items...")
        items = read_playlist(spotify, args.destination_playlist_id)

        if not items:
            logger.write("Playlist is empty. Nothing to do.")
            return 0

        logger.write(f"Found {len(items)} playlist item(s).\n")

        duplicates = find_duplicates([item["track"] for item in items])

        if duplicates:
            logger.write(f"Removing {len(duplicates)} duplicate(s)...")

            for duplicate in duplicates[:20]:
                logger.write(
                    f"  [{duplicate.reason}] {duplicate.track.describe()} "
                    f"(position {duplicate.position + 1})"
                )

            if len(duplicates) > 20:
                logger.write(f"  ...and {len(duplicates) - 20} more.")
        else:
            logger.write("No duplicates found.")

        duplicate_positions = [duplicate.position for duplicate in duplicates]
        snapshot_id = spotify.snapshot_id(args.destination_playlist_id)

        snapshot_id = remove_playlist_positions(
            spotify,
            args.destination_playlist_id,
            {
                item["position"]: item["uri"]
                for item in items
                if item["uri"] is not None
            },
            duplicate_positions,
            snapshot_id=snapshot_id,
            logger=logger,
            dry_run=args.dry_run,
        )

        removed = set(duplicate_positions)
        kept_items = [item for item in items if item["position"] not in removed]

        if args.dry_run:
            remaining = kept_items
        else:
            # Read back what Spotify actually did, because removing a URI may
            # remove every copy of it instead of just the duplicate.
            remaining = read_playlist(spotify, args.destination_playlist_id)
            present = {item["track"].id for item in remaining if item["track"]}
            lost = [
                item
                for item in kept_items
                if item["track"] and item["track"].id not in present
            ]

            if lost:
                logger.write(
                    f"Restoring {len(lost)} track(s) Spotify removed entirely."
                )

                for start in range(0, len(lost), BATCH_SIZE):
                    batch = lost[start : start + BATCH_SIZE]
                    snapshot_id = spotify.add_items(
                        args.destination_playlist_id,
                        [f"spotify:track:{item['track'].id}" for item in batch],
                        snapshot_id=snapshot_id,
                    )

                remaining = remaining + lost

            counts = Counter(item["token"] for item in remaining)
            repeated = [
                token
                for token, count in counts.items()
                if count > 1 and not token.startswith(UNMANAGED_PREFIX)
            ]

            if repeated:
                raise RuntimeError(
                    f"Spotify still contains {len(repeated)} duplicated entr(ies) "
                    "after the removal pass; aborting before reordering."
                )

        desired = newest_first(remaining)

        logger.write("")
        logger.write("Desired order (first 10):")
        logger.write("-" * 70)

        for position, item in enumerate(desired[:10], start=1):
            track = item["track"]
            name = track.describe() if track else "Unmanageable item"
            logger.write(f"{position:>3}. {name}")
            logger.write(f"     added_at={item['added_at'].isoformat() if item['added_at'] else 'unknown'}")

        if len(desired) > 10:
            logger.write(f"... and {len(desired) - 10} more")

        logger.write("-" * 70)

        moves = plan_moves(
            [item["token"] for item in remaining],
            [item["token"] for item in desired],
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
            verify_order(
                spotify,
                args.destination_playlist_id,
                desired,
                logger,
            )

        logger.write("\n" + "=" * 70)
        logger.write(f"Duplicates removed: {len(duplicates)}")
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
        logger.write("REORDER FAILED")
        logger.write("=" * 70)
        logger.write(str(error))
        return 1

    finally:
        logger.save()


def verify_order(
    spotify: Spotify,
    playlist_id: str,
    desired: list[dict],
    logger: Logger,
) -> None:
    """Re-read the playlist and confirm the managed tracks are in order."""
    logger.write("\nVerifying the playlist...")
    entries = spotify.playlist_items(playlist_id, label="playlist")

    final_ids = [
        track.id
        for track in (track_from_spotify(playlist_entry_track(entry)) for entry in entries)
        if track
    ]
    expected_ids = [item["track"].id for item in desired if item["track"]]

    if final_ids != expected_ids:
        shared = min(len(final_ids), len(expected_ids))
        mismatch = next(
            (
                index
                for index in range(shared)
                if final_ids[index] != expected_ids[index]
            ),
            shared,
        )
        actual = final_ids[mismatch] if mismatch < len(final_ids) else "<missing>"
        expected = (
            expected_ids[mismatch] if mismatch < len(expected_ids) else "<none>"
        )

        raise RuntimeError(
            "Verification failed: the playlist order does not match "
            f"(first mismatch at position {mismatch + 1}; "
            f"expected {expected}, found {actual})."
        )

    if len(entries) != len(desired):
        raise RuntimeError(
            "Verification failed: unexpected item count "
            f"({len(entries)} in the playlist vs {len(desired)} expected)."
        )

    logger.write(f"Verification passed: {len(expected_ids)} tracks in order.")


if __name__ == "__main__":
    raise SystemExit(main())
