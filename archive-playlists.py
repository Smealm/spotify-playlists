import argparse
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

ARCHIVE_URL = (
    "https://raw.githubusercontent.com/"
    "mackorone/spotify-playlist-archive-2/"
    "refs/heads/main/playlists/cumulative/{playlist_id}.md"
)

DRY_RUN = False

BATCH_SIZE = 100


# ============================================================
# ARGUMENTS
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--archive-playlist-id",
        required=True,
    )

    parser.add_argument(
        "--destination-playlist-id",
        required=True,
    )

    parser.add_argument(
        "--log",
        required=True,
    )

    return parser.parse_args()


ARGS = parse_args()

# The archive playlist ID is also the live/main Spotify
# playlist ID by design.
ARCHIVE_PLAYLIST_ID = ARGS.archive_playlist_id

DESTINATION_PLAYLIST_ID = (
    ARGS.destination_playlist_id
)

LOG_PATH = ARGS.log

PROJECT_DIR = Path(__file__).resolve().parent


# ============================================================
# LOGGING
# ============================================================

def resolve_log_path(value):
    value = str(value).strip().lstrip("/\\")

    if not value:
        raise ValueError(
            "--log cannot be empty"
        )

    path = (
        PROJECT_DIR / value
    ).resolve()

    try:
        path.relative_to(PROJECT_DIR)
    except ValueError:
        raise ValueError(
            "--log must stay inside "
            "the project directory"
        )

    return path


class Logger:
    def __init__(self, path):
        self.path = resolve_log_path(path)
        self.lines = []

    def add(self, text=""):
        self.lines.append(str(text))

    def write(self):
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.path.write_text(
            "\n".join(self.lines) + "\n",
            encoding="utf-8",
        )

        print(
            f"Log written to {self.path}"
        )


LOGGER = Logger(LOG_PATH)


# ============================================================
# SPOTIFY
# ============================================================

CLIENT_ID = os.environ[
    "SPOTIFY_CLIENT_ID"
]

CLIENT_SECRET = os.environ[
    "SPOTIFY_CLIENT_SECRET"
]

REFRESH_TOKEN = os.environ[
    "SPOTIFY_REFRESH_TOKEN"
]

API = "https://api.spotify.com/v1"


def get_access_token():
    print(
        "Refreshing Spotify token..."
    )

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        auth=(
            CLIENT_ID,
            CLIENT_SECRET,
        ),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()[
        "access_token"
    ]


def spotify_request(
    method,
    url,
    token,
    **kwargs,
):
    headers = kwargs.pop(
        "headers",
        {},
    )

    headers["Authorization"] = (
        f"Bearer {token}"
    )

    for attempt in range(5):
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=30,
            **kwargs,
        )

        if response.status_code == 429:
            retry_after = response.headers.get(
                "Retry-After",
                "5",
            )

            try:
                retry_after = int(
                    retry_after
                )
            except ValueError:
                retry_after = 5

            print(
                "Spotify rate limited. "
                f"Waiting {retry_after}s..."
            )

            time.sleep(retry_after)

            continue

        response.raise_for_status()

        return response

    raise RuntimeError(
        "Spotify rate limit persisted "
        "after 5 retries."
    )


# ============================================================
# ARCHIVE
# ============================================================

TRACK_URL_RE = re.compile(
    r"https://open\.spotify\.com/track/"
    r"([A-Za-z0-9]+)"
)


def parse_duration(value):
    match = re.match(
        r"^\s*(\d+):(\d{2})\s*$",
        value,
    )

    if not match:
        return 0

    minutes = int(
        match.group(1)
    )

    seconds = int(
        match.group(2)
    )

    return (
        minutes * 60
        + seconds
    )


def read_archive():
    url = ARCHIVE_URL.format(
        playlist_id=ARCHIVE_PLAYLIST_ID
    )

    print(
        "Downloading archive:"
    )

    print(url)

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    tracks = []

    for line in response.text.splitlines():

        if not line.startswith("|"):
            continue

        match = TRACK_URL_RE.search(
            line
        )

        if not match:
            continue

        columns = [
            column.strip()
            for column
            in line.strip("|").split("|")
        ]

        # Expected:
        #
        # Title | Artist(s) | Album |
        # Length | Added | Removed

        if len(columns) < 5:
            continue

        title = columns[0]
        artist = columns[1]
        duration = columns[3]
        added = columns[4]

        if not re.match(
            r"^\d{4}-\d{2}-\d{2}",
            added,
        ):
            continue

        try:
            added_date = datetime.strptime(
                added[:10],
                "%Y-%m-%d",
            )
        except ValueError:
            continue

        tracks.append(
            {
                "track_id": match.group(1),
                "title": title,
                "artist": artist,
                "duration": parse_duration(
                    duration
                ),
                "added": added_date,
            }
        )

    # Oldest -> newest internally.
    tracks.sort(
        key=lambda track: track["added"]
    )

    # Deduplicate archive by exact
    # Spotify track ID.
    seen = set()
    result = []

    for track in tracks:

        track_id = track["track_id"]

        if track_id in seen:
            continue

        seen.add(track_id)
        result.append(track)

    print(
        f"Archive contains "
        f"{len(result)} unique tracks."
    )

    return result


# ============================================================
# SPOTIFY PLAYLIST ITEMS
# ============================================================

def get_playlist_items(
    token,
    playlist_id,
    label="playlist",
):
    print(
        f"Reading {label}..."
    )

    url = (
        f"{API}/playlists/"
        f"{playlist_id}/items"
    )

    params = {
        "limit": 50,
        "fields": (
            "items("
            "item("
            "id,"
            "type,"
            "uri,"
            "name"
            ")"
            "),"
            "next"
        ),
    }

    tracks = []

    page = 0

    while url:

        page += 1

        response = spotify_request(
            "GET",
            url,
            token,
            params=params,
        )

        data = response.json()

        page_items = data.get(
            "items",
            []
        )

        for item in page_items:

            track = item.get(
                "item"
            )

            if not track:
                continue

            if track.get(
                "type"
            ) != "track":
                continue

            track_id = track.get(
                "id"
            )

            if not track_id:
                continue

            tracks.append(track)

        print(
            f"  Read page {page}: "
            f"{len(tracks)} tracks"
        )

        url = data.get(
            "next"
        )

        params = None

    print(
        f"{label.capitalize()} contains "
        f"{len(tracks)} tracks."
    )

    return tracks


# ============================================================
# DEDUPLICATION
# ============================================================

def find_duplicate_items(tracks):
    """
    Spotify-Dedup-style detection.

    A duplicate is determined ONLY by the
    exact Spotify track ID.

    The first occurrence is kept.

    This intentionally does NOT use:
      - title similarity
      - artist similarity
      - duration similarity
      - album similarity
    """

    seen_ids = set()
    unique_tracks = []
    duplicate_tracks = []

    for position, track in enumerate(
        tracks
    ):

        track_id = track.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in seen_ids:

            duplicate_tracks.append(
                {
                    "position": position,
                    "track": track,
                }
            )

            continue

        seen_ids.add(track_id)
        unique_tracks.append(track)

    return (
        unique_tracks,
        duplicate_tracks,
    )


def report_duplicates(
    duplicate_tracks,
):
    count = len(
        duplicate_tracks
    )

    if count == 0:
        print(
            "Deduplication: no duplicates found."
        )

        return

    print(
        f"Deduplication: found "
        f"{count} duplicate item(s)."
    )

    for item in duplicate_tracks[
        :20
    ]:

        position = item[
            "position"
        ]

        track = item[
            "track"
        ]

        print(
            f"  Duplicate at position "
            f"{position + 1}: "
            f"{track.get('name', 'Unknown')} "
            f"({track.get('id')})"
        )

    if count > 20:
        print(
            f"  ...and {count - 20} more."
        )


# ============================================================
# DESIRED ORDER
# ============================================================

def build_desired_order(
    main_playlist,
    archive,
    destination_unique,
):
    """
    Build the final archive playlist order.

    Priority:

        1. Current live/main Spotify playlist
           in EXACTLY its current order.

        2. Historical archive tracks that are
           no longer in the main playlist,
           newest -> oldest.

        3. Any destination-only tracks that
           exist outside the archive/main playlist,
           preserving their current order.

    Every Spotify track ID appears at most once.

    Example:

        MAIN PLAYLIST
        A
        B
        C

        ARCHIVE
        Z
        Y
        C
        B
        A

        DESTINATION-ONLY
        X

        Result:

        A
        B
        C
        Z
        Y
        X
    """

    desired = []
    used = set()

    # --------------------------------------------------------
    # Tier 1:
    #
    # Current live playlist order.
    #
    # This is the most important part. The archive should
    # visually match the real Spotify playlist at the top.
    # --------------------------------------------------------

    for track in main_playlist:

        track_id = track.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in used:
            continue

        desired.append(
            track_id
        )

        used.add(
            track_id
        )

    main_ids = set(used)

    print(
        f"Main playlist section: "
        f"{len(main_ids)} unique tracks."
    )

    # --------------------------------------------------------
    # Tier 2:
    #
    # Historical archive.
    #
    # Archive is stored oldest -> newest, so reverse it
    # to produce newest -> oldest.
    #
    # Anything already present in the main playlist is
    # skipped because it already exists in tier 1.
    # --------------------------------------------------------

    historical_added = 0

    for track in reversed(archive):

        track_id = track[
            "track_id"
        ]

        if track_id in used:
            continue

        desired.append(
            track_id
        )

        used.add(
            track_id
        )

        historical_added += 1

    print(
        f"Historical section: "
        f"{historical_added} tracks."
    )

    # --------------------------------------------------------
    # Tier 3:
    #
    # Preserve anything already in the destination playlist
    # that isn't represented by the live playlist or archive.
    #
    # This prevents unrelated/custom destination tracks
    # from disappearing during a rebuild.
    # --------------------------------------------------------

    destination_only_added = 0

    for track in destination_unique:

        track_id = track.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in used:
            continue

        desired.append(
            track_id
        )

        used.add(
            track_id
        )

        destination_only_added += 1

    if destination_only_added:
        print(
            f"Destination-only section: "
            f"{destination_only_added} tracks."
        )

    print(
        f"Desired playlist contains "
        f"{len(desired)} unique tracks."
    )

    return desired


# ============================================================
# ADD NEW ARCHIVE TRACKS
# ============================================================

def add_new_tracks(
    new_tracks,
    token,
):
    """
    Add newly discovered archive tracks.

    This is retained for incremental updates.

    The final ordering is handled afterward by the
    main/historical ordering logic.

    Spotify accepts a maximum of 100 items
    per add request.
    """

    if not new_tracks:
        print(
            "No new archive tracks to add."
        )

        return 0

    ordered = list(
        reversed(new_tracks)
    )

    total = len(ordered)

    if DRY_RUN:
        print(
            f"DRY RUN: would add "
            f"{total} tracks."
        )

        return total

    print(
        f"Adding {total} new archive "
        f"track(s)..."
    )

    batches = [
        ordered[
            start:start + BATCH_SIZE
        ]
        for start in range(
            0,
            total,
            BATCH_SIZE,
        )
    ]

    added = 0

    # Insert older batches first.
    #
    # Then insert newer batches at position 0.
    #
    # This preserves newest -> oldest ordering
    # for the newly added tracks until the final
    # ordering pass runs.

    for batch in reversed(batches):

        uris = [
            f"spotify:track:{track['track_id']}"
            for track in batch
        ]

        spotify_request(
            "POST",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}"
                f"/items"
            ),
            token,
            json={
                "uris": uris,
                "position": 0,
            },
        )

        added += len(batch)

        print(
            f"  Added "
            f"{added}/{total}"
        )

    return added


# ============================================================
# REBUILD PLAYLIST
# ============================================================

def rebuild_playlist(
    desired_ids,
    token,
):
    """
    Rebuild the destination playlist in batches.

    First request:
        replace playlist with first 100

    Remaining requests:
        append another 100 at a time.

    This is dramatically faster than performing one
    reorder request per track on large playlists.
    """

    if not desired_ids:
        raise RuntimeError(
            "Desired playlist is empty."
        )

    total = len(
        desired_ids
    )

    print()
    print(
        f"Rebuilding playlist with "
        f"{total} unique items..."
    )

    if DRY_RUN:
        print(
            "DRY RUN: playlist would be rebuilt."
        )

        return 0

    request_count = 0

    # --------------------------------------------------------
    # First 100: replace entire playlist.
    # --------------------------------------------------------

    first_batch = desired_ids[
        :BATCH_SIZE
    ]

    first_uris = [
        f"spotify:track:{track_id}"
        for track_id in first_batch
    ]

    print(
        f"  Replacing playlist with "
        f"items 1-{len(first_batch)}..."
    )

    spotify_request(
        "PUT",
        (
            f"{API}/playlists/"
            f"{DESTINATION_PLAYLIST_ID}/items"
        ),
        token,
        json={
            "uris": first_uris,
        },
    )

    request_count += 1

    completed = len(
        first_batch
    )

    print(
        f"  Rebuilt "
        f"{completed}/{total}"
    )

    # --------------------------------------------------------
    # Remaining items: append.
    # --------------------------------------------------------

    remaining = desired_ids[
        BATCH_SIZE:
    ]

    for start in range(
        0,
        len(remaining),
        BATCH_SIZE,
    ):

        batch = remaining[
            start:start + BATCH_SIZE
        ]

        uris = [
            f"spotify:track:{track_id}"
            for track_id in batch
        ]

        spotify_request(
            "POST",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}/items"
            ),
            token,
            json={
                "uris": uris,
            },
        )

        request_count += 1

        completed += len(batch)

        print(
            f"  Rebuilt "
            f"{completed}/{total}"
        )

    print(
        f"Playlist rebuild complete "
        f"using {request_count} request(s)."
    )

    return request_count


# ============================================================
# TARGETED SMALL REORDER
# ============================================================

def reorder_small(
    current_ids,
    desired_ids,
    token,
):
    """
    Used only when the playlist is already mostly correct.

    Moves contiguous ranges instead of moving every track
    individually.
    """

    if current_ids == desired_ids:
        print(
            "Playlist is already correctly ordered."
        )

        return 0, 0.0

    if DRY_RUN:
        print(
            "DRY RUN: playlist would be reordered."
        )

        return 0, 0.0

    response = spotify_request(
        "GET",
        (
            f"{API}/playlists/"
            f"{DESTINATION_PLAYLIST_ID}"
        ),
        token,
        params={
            "fields": "snapshot_id"
        },
    )

    snapshot = response.json()[
        "snapshot_id"
    ]

    working = list(
        current_ids
    )

    moves = 0

    started = time.perf_counter()

    for target in range(
        len(desired_ids)
    ):

        wanted = desired_ids[
            target
        ]

        if working[target] == wanted:
            continue

        try:
            source = working.index(
                wanted,
                target + 1,
            )
        except ValueError:
            raise RuntimeError(
                "Desired track not found "
                "during reorder."
            )

        range_length = 1

        while (
            source + range_length
            < len(working)
            and target + range_length
            < len(desired_ids)
            and working[
                source + range_length
            ]
            == desired_ids[
                target + range_length
            ]
        ):
            range_length += 1

        response = spotify_request(
            "PUT",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}/items"
            ),
            token,
            json={
                "range_start": source,
                "insert_before": target,
                "range_length": range_length,
                "snapshot_id": snapshot,
            },
        )

        snapshot = response.json()[
            "snapshot_id"
        ]

        block = working[
            source:
            source + range_length
        ]

        del working[
            source:
            source + range_length
        ]

        working[
            target:target
        ] = block

        moves += 1

        print(
            f"  Move {moves}: "
            f"{source} -> {target}, "
            f"{range_length} item(s)"
        )

    duration = (
        time.perf_counter()
        - started
    )

    print(
        f"Targeted reorder complete: "
        f"{moves} move(s), "
        f"{duration:.2f}s"
    )

    return moves, duration


# ============================================================
# VERIFICATION
# ============================================================

def verify_playlist(
    desired_ids,
    token,
):
    print(
        "Verifying final playlist..."
    )

    final_tracks = get_playlist_items(
        token,
        DESTINATION_PLAYLIST_ID,
        "final destination playlist",
    )

    final_ids = [
        track["id"]
        for track in final_tracks
    ]

    # --------------------------------------------------------
    # Check exact order/content.
    # --------------------------------------------------------

    if final_ids != desired_ids:

        raise RuntimeError(
            "Playlist verification failed: "
            "final order/content does not "
            "match the desired playlist."
        )

    # --------------------------------------------------------
    # Check duplicates.
    # --------------------------------------------------------

    if len(final_ids) != len(
        set(final_ids)
    ):

        raise RuntimeError(
            "Playlist verification failed: "
            "duplicate Spotify track IDs "
            "are still present."
        )

    print(
        "Playlist verification passed."
    )

    print(
        f"Final playlist contains "
        f"{len(final_ids)} unique tracks."
    )

    return len(final_ids)


# ============================================================
# SUMMARY
# ============================================================

def write_summary(
    archive_count,
    original_playlist_count,
    unique_before_count,
    duplicate_count,
    main_playlist_count,
    historical_count,
    new_count,
    added_count,
    final_playlist_count,
    rebuild_used,
    reorder_moves,
    reorder_duration,
):
    LOGGER.add(
        "=" * 60
    )

    LOGGER.add(
        "Spotify Playlist Archive Sync"
    )

    LOGGER.add(
        "Main playlist order -> Historical archive"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Archive tracks:       "
        f"{archive_count}"
    )

    LOGGER.add(
        f"Original playlist:    "
        f"{original_playlist_count}"
    )

    LOGGER.add(
        f"Unique before dedup:  "
        f"{unique_before_count}"
    )

    LOGGER.add(
        f"Duplicates removed:   "
        f"{duplicate_count}"
    )

    LOGGER.add(
        f"Main playlist tracks: "
        f"{main_playlist_count}"
    )

    LOGGER.add(
        f"Historical tracks:    "
        f"{historical_count}"
    )

    LOGGER.add(
        f"New tracks found:     "
        f"{new_count}"
    )

    LOGGER.add(
        f"Tracks added:         "
        f"{added_count}"
    )

    LOGGER.add(
        f"Final playlist:       "
        f"{final_playlist_count}"
    )

    LOGGER.add(
        f"Rebuild used:         "
        f"{rebuild_used}"
    )

    LOGGER.add(
        f"Reorder operations:   "
        f"{reorder_moves}"
    )

    LOGGER.add(
        f"Reorder duration:     "
        f"{reorder_duration:.2f} seconds"
    )

    LOGGER.write()


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)

    print(
        "Spotify Playlist Archive Sync"
    )

    print(
        "Main playlist order -> Historical archive"
    )

    print(
        "Deduplication: exact Spotify track ID"
    )

    print("=" * 60)

    # --------------------------------------------------------
    # 1. Download cumulative archive.
    # --------------------------------------------------------

    archive = read_archive()

    if not archive:
        raise RuntimeError(
            "Archive returned zero tracks. "
            "Refusing to modify playlist."
        )

    # --------------------------------------------------------
    # 2. Authenticate.
    # --------------------------------------------------------

    token = get_access_token()

    # --------------------------------------------------------
    # 3. Read the LIVE/MAIN playlist.
    #
    # IMPORTANT:
    #
    # ARCHIVE_PLAYLIST_ID is the main playlist ID by design.
    #
    # Capture this BEFORE modifying the destination so newly
    # added historical tracks cannot contaminate the current
    # main-playlist ordering.
    # --------------------------------------------------------

    main_playlist = get_playlist_items(
        token,
        ARCHIVE_PLAYLIST_ID,
        "live main playlist",
    )

    main_playlist_unique, main_duplicates = (
        find_duplicate_items(
            main_playlist
        )
    )

    if main_duplicates:
        print()
        print(
            "WARNING: the live/main playlist itself "
            "contains duplicate track IDs."
        )

        report_duplicates(
            main_duplicates
        )

        print(
            "The first occurrence of each track "
            "will be used for main-playlist ordering."
        )

    main_playlist_count = len(
        main_playlist_unique
    )

    # --------------------------------------------------------
    # 4. Read destination archive playlist.
    # --------------------------------------------------------

    current = get_playlist_items(
        token,
        DESTINATION_PLAYLIST_ID,
        "destination playlist",
    )

    original_playlist_count = len(
        current
    )

    # --------------------------------------------------------
    # 5. Spotify-Dedup style dedup.
    # --------------------------------------------------------

    print(
        "Checking destination for "
        "duplicate Spotify track IDs..."
    )

    (
        unique_current,
        duplicate_items,
    ) = find_duplicate_items(
        current
    )

    report_duplicates(
        duplicate_items
    )

    duplicate_count = len(
        duplicate_items
    )

    print(
        f"Unique destination tracks: "
        f"{len(unique_current)}"
    )

    # --------------------------------------------------------
    # 6. Find archive tracks that aren't already
    #    represented in the destination.
    # --------------------------------------------------------

    existing_ids = {
        track["id"]
        for track in unique_current
    }

    new_tracks = [
        track
        for track in archive
        if track["track_id"]
        not in existing_ids
    ]

    print()
    print(
        f"Archive tracks: "
        f"{len(archive)}"
    )

    print(
        f"Existing unique matches: "
        f"{len(archive) - len(new_tracks)}"
    )

    print(
        f"New archive tracks: "
        f"{len(new_tracks)}"
    )

    # --------------------------------------------------------
    # 7. Add new historical tracks.
    # --------------------------------------------------------

    added_count = add_new_tracks(
        new_tracks,
        token,
    )

    # --------------------------------------------------------
    # 8. Re-read destination after adding.
    #
    # We do NOT re-read main_playlist here.
    #
    # The original main_playlist snapshot is authoritative
    # for the current/main section.
    # --------------------------------------------------------

    if added_count:

        current = get_playlist_items(
            token,
            DESTINATION_PLAYLIST_ID,
            "destination playlist after additions",
        )

        (
            unique_current,
            duplicate_items,
        ) = find_duplicate_items(
            current
        )

        duplicate_count = len(
            duplicate_items
        )

    # --------------------------------------------------------
    # 9. Build desired order.
    #
    # IMPORTANT:
    #
    #   Tier 1 = live main playlist
    #   Tier 2 = historical archive
    #   Tier 3 = destination-only tracks
    #
    # Main playlist tracks always win over historical
    # ordering because they are currently active.
    # --------------------------------------------------------

    print(
        "Calculating desired playlist order..."
    )

    desired_ids = build_desired_order(
        main_playlist_unique,
        archive,
        unique_current,
    )

    current_ids = [
        track["id"]
        for track in current
    ]

    # --------------------------------------------------------
    # Calculate historical section count for logging.
    # --------------------------------------------------------

    main_ids = {
        track["id"]
        for track in main_playlist_unique
    }

    historical_ids = []

    historical_seen = set()

    for track in reversed(archive):

        track_id = track["track_id"]

        if track_id in main_ids:
            continue

        if track_id in historical_seen:
            continue

        historical_seen.add(
            track_id
        )

        historical_ids.append(
            track_id
        )

    historical_count = len(
        historical_ids
    )

    # --------------------------------------------------------
    # 10. Determine whether a rebuild is required.
    #
    # Rebuild when:
    #
    #   - duplicates exist
    #   - 100+ positions differ
    #
    # This keeps the common case fast while allowing small
    # changes to use targeted reordering.
    # --------------------------------------------------------

    if duplicate_count > 0:

        print()
        print(
            f"{duplicate_count} duplicate(s) "
            "detected."
        )

        print(
            "Forcing full playlist rebuild "
            "to remove duplicates."
        )

        started = time.perf_counter()

        rebuild_requests = rebuild_playlist(
            desired_ids,
            token,
        )

        reorder_duration = (
            time.perf_counter()
            - started
        )

        rebuild_used = True
        reorder_moves = rebuild_requests

    else:

        # ----------------------------------------------------
        # Already correct?
        # ----------------------------------------------------

        if (
            current_ids == desired_ids
        ):

            print(
                "Playlist is already correctly "
                "ordered and deduplicated."
            )

            rebuild_used = False
            reorder_moves = 0
            reorder_duration = 0.0

        else:

            # ------------------------------------------------
            # Count positional differences.
            # ------------------------------------------------

            shared_length = min(
                len(current_ids),
                len(desired_ids),
            )

            mismatch_count = sum(
                1
                for index in range(
                    shared_length
                )
                if current_ids[index]
                != desired_ids[index]
            )

            mismatch_count += abs(
                len(current_ids)
                - len(desired_ids)
            )

            print(
                f"Playlist differs from desired "
                f"order/content at "
                f"{mismatch_count} position(s)."
            )

            # ------------------------------------------------
            # Large change:
            #
            # Rebuild in ~100-item batches.
            # ------------------------------------------------

            if mismatch_count >= 100:

                started = (
                    time.perf_counter()
                )

                rebuild_requests = (
                    rebuild_playlist(
                        desired_ids,
                        token,
                    )
                )

                reorder_duration = (
                    time.perf_counter()
                    - started
                )

                rebuild_used = True
                reorder_moves = (
                    rebuild_requests
                )

            # ------------------------------------------------
            # Small change:
            #
            # Use targeted Spotify reorder operations.
            # ------------------------------------------------

            else:

                (
                    reorder_moves,
                    reorder_duration,
                ) = reorder_small(
                    current_ids,
                    desired_ids,
                    token,
                )

                rebuild_used = False

    # --------------------------------------------------------
    # 11. Verify exact final state.
    # --------------------------------------------------------

    final_playlist_count = (
        verify_playlist(
            desired_ids,
            token,
        )
    )

    # --------------------------------------------------------
    # 12. Write log.
    # --------------------------------------------------------

    write_summary(
        archive_count=len(archive),
        original_playlist_count=(
            original_playlist_count
        ),
        unique_before_count=(
            len(unique_current)
        ),
        duplicate_count=(
            duplicate_count
        ),
        main_playlist_count=(
            main_playlist_count
        ),
        historical_count=(
            historical_count
        ),
        new_count=len(new_tracks),
        added_count=added_count,
        final_playlist_count=(
            final_playlist_count
        ),
        rebuild_used=rebuild_used,
        reorder_moves=reorder_moves,
        reorder_duration=reorder_duration,
    )

    print()
    print(
        "Done."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
