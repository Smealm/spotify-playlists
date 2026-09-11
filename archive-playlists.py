import argparse
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

CUMULATIVE_URL = (
    "https://raw.githubusercontent.com/"
    "mackorone/spotify-playlist-archive-2/"
    "refs/heads/main/playlists/cumulative/{playlist_id}.md"
)

PRETTY_URL = (
    "https://raw.githubusercontent.com/"
    "mackorone/spotify-playlist-archive-2/"
    "refs/heads/main/playlists/pretty/{playlist_id}.md"
)

DRY_RUN = False

# Spotify's current playlist APIs allow a maximum of
# 100 items per add/reorder/replace request.
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
# ARCHIVE PARSING
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


def dedupe_ids_in_order(ids):
    """
    Exact Spotify track-ID deduplication.

    Keeps the first occurrence.
    """

    seen = set()
    result = []

    for track_id in ids:

        if not track_id:
            continue

        if track_id in seen:
            continue

        seen.add(track_id)
        result.append(track_id)

    return result


# ============================================================
# PRETTY ARCHIVE
# ============================================================

def read_pretty():
    url = PRETTY_URL.format(
        playlist_id=ARCHIVE_PLAYLIST_ID
    )

    print()
    print(
        "Downloading pretty archive:"
    )

    print(url)

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    # The pretty archive already stores the playlist
    # in its exact current order.
    #
    # Track URLs occur in the table in playlist order.
    raw_ids = TRACK_URL_RE.findall(
        response.text
    )

    track_ids = dedupe_ids_in_order(
        raw_ids
    )

    print(
        f"Pretty archive contains "
        f"{len(track_ids)} unique tracks."
    )

    return track_ids


# ============================================================
# CUMULATIVE ARCHIVE
# ============================================================

def read_cumulative():
    url = CUMULATIVE_URL.format(
        playlist_id=ARCHIVE_PLAYLIST_ID
    )

    print()
    print(
        "Downloading cumulative archive:"
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

        # Expected cumulative columns:
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

    # The cumulative archive is normalized internally
    # oldest -> newest.
    tracks.sort(
        key=lambda track: track["added"]
    )

    seen = set()
    result = []

    for track in tracks:

        track_id = track[
            "track_id"
        ]

        if track_id in seen:
            continue

        seen.add(track_id)
        result.append(track)

    print(
        f"Cumulative archive contains "
        f"{len(result)} unique tracks."
    )

    return result


# ============================================================
# BUILD EXACT TARGET
# ============================================================

def build_target_order(
    pretty_ids,
    cumulative_tracks,
):
    """
    Final desired order:

        1. PRETTY tracks
           exact current playlist order

        2. CUMULATIVE tracks not present in PRETTY
           newest -> oldest

    Every Spotify track ID appears exactly once.
    """

    desired = []
    used = set()

    # --------------------------------------------------------
    # Tier 1: PRETTY
    # --------------------------------------------------------

    for track_id in pretty_ids:

        if track_id in used:
            continue

        desired.append(
            track_id
        )

        used.add(
            track_id
        )

    pretty_count = len(
        desired
    )

    # --------------------------------------------------------
    # Tier 2: CUMULATIVE
    #
    # Cumulative is oldest -> newest internally,
    # so reverse it to get newest -> oldest.
    # --------------------------------------------------------

    cumulative_only = []

    for track in reversed(
        cumulative_tracks
    ):

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

        cumulative_only.append(
            track_id
        )

    print()
    print(
        f"Target pretty section: "
        f"{pretty_count} tracks."
    )

    print(
        f"Target cumulative-only section: "
        f"{len(cumulative_only)} tracks."
    )

    print(
        f"Target playlist: "
        f"{len(desired)} unique tracks."
    )

    return (
        desired,
        cumulative_only,
    )


# ============================================================
# DESTINATION PLAYLIST
# ============================================================

def get_playlist_items(
    token,
    playlist_id,
    label="playlist",
):
    print()
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


def get_playlist_snapshot(
    token,
):
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

    return response.json()[
        "snapshot_id"
    ]


# ============================================================
# DEDUPLICATION
# ============================================================

def find_duplicate_items(
    tracks,
):
    """
    Exact Spotify track-ID deduplication.

    The first occurrence is retained conceptually.
    """

    seen = set()
    unique = []
    duplicates = []

    for position, track in enumerate(
        tracks
    ):

        track_id = track.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in seen:

            duplicates.append(
                {
                    "position": position,
                    "track": track,
                }
            )

            continue

        seen.add(
            track_id
        )

        unique.append(
            track
        )

    return (
        unique,
        duplicates,
    )


def report_duplicates(
    duplicate_items,
):
    count = len(
        duplicate_items
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

    for item in duplicate_items[
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
# REMOVE UNWANTED / DUPLICATE TRACK IDS
# ============================================================

def remove_track_ids(
    track_ids,
    token,
    snapshot,
):
    """
    Remove unwanted IDs in batches of 100.

    We deliberately remove an entire duplicate ID when it
    occurs more than once, then add exactly one copy back
    during the normal missing-track phase.

    This avoids trying to target individual duplicate
    occurrences.
    """

    track_ids = dedupe_ids_in_order(
        track_ids
    )

    if not track_ids:
        return (
            snapshot,
            0,
        )

    print()
    print(
        f"Removing {len(track_ids)} "
        f"unwanted/duplicate track ID(s)..."
    )

    if DRY_RUN:
        print(
            "DRY RUN: no tracks would be removed."
        )

        return (
            snapshot,
            0,
        )

    request_count = 0

    for start in range(
        0,
        len(track_ids),
        BATCH_SIZE,
    ):

        batch = track_ids[
            start:start + BATCH_SIZE
        ]

        payload = {
            "items": [
                {
                    "uri": (
                        f"spotify:track:{track_id}"
                    )
                }
                for track_id in batch
            ],
            "snapshot_id": snapshot,
        }

        response = spotify_request(
            "DELETE",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}"
                f"/items"
            ),
            token,
            json=payload,
        )

        data = response.json()

        snapshot = data.get(
            "snapshot_id",
            snapshot,
        )

        request_count += 1

        print(
            f"  Removed "
            f"{min(start + len(batch), len(track_ids))}"
            f"/{len(track_ids)}"
        )

    return (
        snapshot,
        request_count,
    )


# ============================================================
# ADD MISSING CUMULATIVE TRACKS
# ============================================================

def add_cumulative_tracks(
    track_ids,
    current_ids,
    token,
    snapshot,
):
    """
    Add missing cumulative-only tracks first.

    They are appended in their final historical order.

    This establishes the historical layer before the
    pretty layer is inserted at the top.
    """

    current_set = set(
        current_ids
    )

    missing = [
        track_id
        for track_id in track_ids
        if track_id not in current_set
    ]

    if not missing:
        print()
        print(
            "No missing cumulative-only tracks."
        )

        return (
            current_ids,
            snapshot,
            0,
            0,
        )

    print()
    print(
        f"Adding {len(missing)} missing "
        f"cumulative-only tracks..."
    )

    if DRY_RUN:
        working = list(
            current_ids
        )

        working.extend(
            missing
        )

        print(
            "DRY RUN: cumulative tracks "
            "would be appended."
        )

        return (
            working,
            snapshot,
            len(missing),
            0,
        )

    working = list(
        current_ids
    )

    request_count = 0

    for start in range(
        0,
        len(missing),
        BATCH_SIZE,
    ):

        batch = missing[
            start:start + BATCH_SIZE
        ]

        uris = [
            f"spotify:track:{track_id}"
            for track_id in batch
        ]

        response = spotify_request(
            "POST",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}"
                f"/items"
            ),
            token,
            json={
                "uris": uris,
            },
        )

        data = response.json()

        snapshot = data.get(
            "snapshot_id",
            snapshot,
        )

        working.extend(
            batch
        )

        request_count += 1

        print(
            f"  Added cumulative "
            f"{min(start + len(batch), len(missing))}"
            f"/{len(missing)}"
        )

    return (
        working,
        snapshot,
        len(missing),
        request_count,
    )


# ============================================================
# ADD MISSING PRETTY TRACKS
# ============================================================

def add_pretty_tracks(
    pretty_ids,
    current_ids,
    token,
    snapshot,
):
    """
    Add missing PRETTY tracks at position 0.

    Because Spotify inserts each batch in the order supplied,
    batches are inserted in reverse order.

    Example:

        pretty:
            A B C D E F

        batches:
            A B C
            D E F

        insert D E F at 0
        insert A B C at 0

        result:
            A B C D E F
    """

    current_set = set(
        current_ids
    )

    missing = [
        track_id
        for track_id in pretty_ids
        if track_id not in current_set
    ]

    if not missing:
        print()
        print(
            "No missing pretty tracks."
        )

        return (
            current_ids,
            snapshot,
            0,
            0,
        )

    print()
    print(
        f"Adding {len(missing)} missing "
        f"pretty tracks at the top..."
    )

    batches = [
        missing[
            start:start + BATCH_SIZE
        ]
        for start in range(
            0,
            len(missing),
            BATCH_SIZE,
        )
    ]

    working = list(
        current_ids
    )

    request_count = 0

    for batch in reversed(
        batches
    ):

        if DRY_RUN:

            working[
                0:0
            ] = batch

            continue

        uris = [
            f"spotify:track:{track_id}"
            for track_id in batch
        ]

        response = spotify_request(
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

        data = response.json()

        snapshot = data.get(
            "snapshot_id",
            snapshot,
        )

        working[
            0:0
        ] = batch

        request_count += 1

        print(
            f"  Inserted pretty batch "
            f"of {len(batch)} at position 0"
        )

    if DRY_RUN:
        print(
            "DRY RUN: pretty tracks "
            "would be inserted at the top."
        )

    return (
        working,
        snapshot,
        len(missing),
        request_count,
    )


# ============================================================
# CHUNKED MOVE PLANNER
# ============================================================

def plan_chunked_moves(
    current_ids,
    desired_ids,
):
    """
    Calculate the exact reorder operations needed to turn
    current_ids into desired_ids.

    Strategy:

        1. Find the earliest incorrect position.
        2. Locate the desired track in the remaining playlist.
        3. Starting there, find the longest contiguous run
           that exactly matches the desired target.
        4. Move that entire run.
        5. Never move more than 100 items.
        6. Simulate the move locally.
        7. Continue from the next unresolved position.

    Because the simulation is updated after every planned move,
    every range_start/insert_before value is calculated against
    the playlist state that exists immediately before that move.

    This is dramatically fewer operations than moving individual
    tracks.

    It is a greedy block-move planner: it maximizes the useful
    contiguous target block at each first mismatch. It does not
    claim a mathematical global minimum for arbitrary permutations,
    but it is deterministic and optimized for the archive's
    layered structure.
    """

    current = list(
        current_ids
    )

    desired = list(
        desired_ids
    )

    if len(current) != len(desired):

        raise ValueError(
            "Move planner requires current and desired "
            "playlists to contain the same number of items."
        )

    if set(current) != set(desired):

        raise ValueError(
            "Move planner requires current and desired "
            "playlists to contain the same track IDs."
        )

    operations = []

    position = 0

    while position < len(desired):

        # Already correct.
        if (
            current[position]
            == desired[position]
        ):
            position += 1
            continue

        wanted = desired[
            position
        ]

        try:
            source = current.index(
                wanted,
                position + 1,
            )
        except ValueError:
            raise RuntimeError(
                "Move planner could not find "
                f"desired track {wanted}."
            )

        # Find the largest contiguous block beginning at
        # `source` that exactly matches the target beginning
        # at `position`.
        range_length = 1

        while (
            range_length < BATCH_SIZE
            and
            source + range_length
            < len(current)
            and
            position + range_length
            < len(desired)
            and
            current[
                source + range_length
            ]
            ==
            desired[
                position + range_length
            ]
        ):
            range_length += 1

        block = current[
            source:
            source + range_length
        ]

        # Spotify's insert_before is evaluated against the
        # playlist after the moved range is removed.
        #
        # We always process the earliest mismatch and therefore
        # source > position. Inserting at `position` is exact.
        del current[
            source:
            source + range_length
        ]

        current[
            position:position
        ] = block

        operations.append(
            {
                "range_start": source,
                "insert_before": position,
                "range_length": range_length,
            }
        )

        position += range_length

    if current != desired:

        raise RuntimeError(
            "Move planner failed to produce "
            "the exact desired order."
        )

    return operations


# ============================================================
# EXECUTE CHUNKED MOVES
# ============================================================

def execute_chunked_moves(
    operations,
    token,
    snapshot,
):
    """
    Execute the precomputed move plan.

    Every operation moves <= 100 tracks.

    Spotify returns a new snapshot after every mutation,
    which becomes the snapshot for the next move.
    """

    if not operations:

        print()
        print(
            "No reorder operations required."
        )

        return (
            snapshot,
            0,
            0.0,
        )

    print()
    print(
        f"Executing {len(operations)} "
        f"chunked reorder operation(s)..."
    )

    if DRY_RUN:

        total_items = sum(
            operation[
                "range_length"
            ]
            for operation in operations
        )

        print(
            f"DRY RUN: would execute "
            f"{len(operations)} move(s) "
            f"covering {total_items} moved items."
        )

        return (
            snapshot,
            len(operations),
            0.0,
        )

    started = time.perf_counter()

    for number, operation in enumerate(
        operations,
        start=1,
    ):

        range_start = operation[
            "range_start"
        ]

        insert_before = operation[
            "insert_before"
        ]

        range_length = operation[
            "range_length"
        ]

        if range_length > BATCH_SIZE:

            raise RuntimeError(
                "Planner generated a move larger "
                "than the Spotify 100-item limit."
            )

        response = spotify_request(
            "PUT",
            (
                f"{API}/playlists/"
                f"{DESTINATION_PLAYLIST_ID}"
                f"/items"
            ),
            token,
            json={
                "range_start": range_start,
                "insert_before": insert_before,
                "range_length": range_length,
                "snapshot_id": snapshot,
            },
        )

        data = response.json()

        snapshot = data.get(
            "snapshot_id",
            snapshot,
        )

        print(
            f"  Move {number}/{len(operations)}: "
            f"source={range_start}, "
            f"insert_before={insert_before}, "
            f"items={range_length}"
        )

    duration = (
        time.perf_counter()
        - started
    )

    print(
        f"Chunked reorder complete: "
        f"{len(operations)} move(s), "
        f"{duration:.2f}s"
    )

    return (
        snapshot,
        len(operations),
        duration,
    )


# ============================================================
# VERIFICATION
# ============================================================

def verify_playlist(
    desired_ids,
    token,
):
    print()
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

    if final_ids != desired_ids:

        # Give useful diagnostic information.
        shared = min(
            len(final_ids),
            len(desired_ids),
        )

        mismatch = None

        for index in range(
            shared
        ):

            if (
                final_ids[index]
                != desired_ids[index]
            ):

                mismatch = index
                break

        if mismatch is None:
            mismatch = shared

        actual = (
            final_ids[mismatch]
            if mismatch < len(final_ids)
            else "<missing>"
        )

        expected = (
            desired_ids[mismatch]
            if mismatch < len(desired_ids)
            else "<none>"
        )

        raise RuntimeError(
            "Playlist verification failed.\n"
            f"First mismatch at position "
            f"{mismatch + 1}.\n"
            f"Expected: {expected}\n"
            f"Actual:   {actual}\n"
            f"Expected count: {len(desired_ids)}\n"
            f"Actual count:   {len(final_ids)}"
        )

    if len(final_ids) != len(
        set(final_ids)
    ):

        raise RuntimeError(
            "Playlist verification failed: "
            "duplicate Spotify track IDs remain."
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
    cumulative_count,
    pretty_count,
    target_count,
    original_count,
    duplicate_count,
    removed_count,
    added_cumulative_count,
    added_pretty_count,
    final_count,
    reorder_operations,
    reorder_duration,
):
    LOGGER.add(
        "=" * 60
    )

    LOGGER.add(
        "Spotify Playlist Archive Sync"
    )

    LOGGER.add(
        "PRETTY order -> CUMULATIVE history"
    )

    LOGGER.add(
        "Deduplication: exact Spotify track ID"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Cumulative tracks:       "
        f"{cumulative_count}"
    )

    LOGGER.add(
        f"Pretty tracks:            "
        f"{pretty_count}"
    )

    LOGGER.add(
        f"Target tracks:            "
        f"{target_count}"
    )

    LOGGER.add(
        f"Original destination:     "
        f"{original_count}"
    )

    LOGGER.add(
        f"Duplicate items found:    "
        f"{duplicate_count}"
    )

    LOGGER.add(
        f"IDs removed/normalized:   "
        f"{removed_count}"
    )

    LOGGER.add(
        f"Cumulative tracks added:  "
        f"{added_cumulative_count}"
    )

    LOGGER.add(
        f"Pretty tracks added:      "
        f"{added_pretty_count}"
    )

    LOGGER.add(
        f"Final playlist:            "
        f"{final_count}"
    )

    LOGGER.add(
        f"Reorder operations:        "
        f"{reorder_operations}"
    )

    LOGGER.add(
        f"Reorder duration:          "
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
        "PRETTY current order -> CUMULATIVE history"
    )

    print(
        "Deduplication: exact Spotify track ID"
    )

    print(
        "Source playlist: GitHub archive"
    )

    print(
        "Destination playlist: Spotify"
    )

    print("=" * 60)

    # --------------------------------------------------------
    # 1. Read PRETTY.
    #
    # This is the authoritative current playlist order.
    # --------------------------------------------------------

    pretty_ids = read_pretty()

    if not pretty_ids:

        raise RuntimeError(
            "Pretty archive returned zero tracks. "
            "Refusing to modify playlist."
        )

    # --------------------------------------------------------
    # 2. Read CUMULATIVE.
    #
    # This supplies the historical tracks.
    # --------------------------------------------------------

    cumulative_tracks = (
        read_cumulative()
    )

    if not cumulative_tracks:

        raise RuntimeError(
            "Cumulative archive returned zero tracks. "
            "Refusing to modify playlist."
        )

    # --------------------------------------------------------
    # 3. Build exact target.
    #
    # PRETTY always wins the top layer.
    #
    # CUMULATIVE-only tracks follow newest -> oldest.
    # --------------------------------------------------------

    (
        desired_ids,
        cumulative_only_ids,
    ) = build_target_order(
        pretty_ids,
        cumulative_tracks,
    )

    target_set = set(
        desired_ids
    )

    # --------------------------------------------------------
    # 4. Authenticate.
    # --------------------------------------------------------

    token = get_access_token()

    # --------------------------------------------------------
    # 5. Read destination.
    # --------------------------------------------------------

    current_tracks = get_playlist_items(
        token,
        DESTINATION_PLAYLIST_ID,
        "destination playlist",
    )

    original_count = len(
        current_tracks
    )

    current_ids = [
        track["id"]
        for track in current_tracks
    ]

    # --------------------------------------------------------
    # 6. Get current Spotify snapshot.
    # --------------------------------------------------------

    snapshot = get_playlist_snapshot(
        token
    )

    # --------------------------------------------------------
    # 7. Detect duplicates.
    # --------------------------------------------------------

    (
        unique_current,
        duplicate_items,
    ) = find_duplicate_items(
        current_tracks
    )

    report_duplicates(
        duplicate_items
    )

    duplicate_count = len(
        duplicate_items
    )

    # --------------------------------------------------------
    # 8. Determine IDs that must be removed.
    #
    # Anything outside the target is removed.
    #
    # Duplicate IDs are also removed completely and then
    # re-added exactly once during the normal addition phase.
    # --------------------------------------------------------

    counts = Counter(
        current_ids
    )

    duplicate_ids = {
        track_id
        for track_id, count
        in counts.items()
        if count > 1
    }

    extra_ids = {
        track_id
        for track_id in counts
        if track_id not in target_set
    }

    remove_ids = (
        duplicate_ids
        | extra_ids
    )

    print()

    print(
        f"Destination IDs to remove/normalize: "
        f"{len(remove_ids)}"
    )

    if duplicate_ids:
        print(
            f"  Duplicate IDs: "
            f"{len(duplicate_ids)}"
        )

    if extra_ids:
        print(
            f"  Destination-only IDs: "
            f"{len(extra_ids)}"
        )

    # --------------------------------------------------------
    # 9. Remove extras and duplicate IDs.
    # --------------------------------------------------------

    (
        snapshot,
        remove_requests,
    ) = remove_track_ids(
        list(remove_ids),
        token,
        snapshot,
    )

    # Simulate the exact post-removal state locally.
    #
    # Every occurrence of a removed ID is considered gone.
    working_ids = [
        track_id
        for track_id in current_ids
        if track_id not in remove_ids
    ]

    # --------------------------------------------------------
    # 10. Add missing CUMULATIVE-only tracks first.
    # --------------------------------------------------------

    (
        working_ids,
        snapshot,
        added_cumulative_count,
        cumulative_add_requests,
    ) = add_cumulative_tracks(
        cumulative_only_ids,
        working_ids,
        token,
        snapshot,
    )

    # --------------------------------------------------------
    # 11. Add missing PRETTY tracks second.
    #
    # They are inserted at position 0.
    #
    # This establishes the layering:
    #
    #     PRETTY
    #     --------
    #     CUMULATIVE
    # --------------------------------------------------------

    (
        working_ids,
        snapshot,
        added_pretty_count,
        pretty_add_requests,
    ) = add_pretty_tracks(
        pretty_ids,
        working_ids,
        token,
        snapshot,
    )

    # --------------------------------------------------------
    # 12. Sanity-check the local state before planning.
    # --------------------------------------------------------

    if (
        len(working_ids)
        != len(desired_ids)
    ):

        raise RuntimeError(
            "Local playlist state does not contain "
            "the expected number of tracks before "
            "reorder planning.\n"
            f"Current:  {len(working_ids)}\n"
            f"Desired:  {len(desired_ids)}"
        )

    if set(working_ids) != target_set:

        raise RuntimeError(
            "Local playlist IDs do not match "
            "the desired target before reorder planning."
        )

    # --------------------------------------------------------
    # 13. Calculate exact chunked reorder plan.
    # --------------------------------------------------------

    print()
    print(
        "Calculating chunked reorder plan..."
    )

    operations = plan_chunked_moves(
        working_ids,
        desired_ids,
    )

    total_moved_items = sum(
        operation[
            "range_length"
        ]
        for operation in operations
    )

    largest_move = max(
        (
            operation[
                "range_length"
            ]
            for operation in operations
        ),
        default=0,
    )

    print(
        f"Planned reorder operations: "
        f"{len(operations)}"
    )

    print(
        f"Total moved items across operations: "
        f"{total_moved_items}"
    )

    print(
        f"Largest move: "
        f"{largest_move} item(s)"
    )

    if largest_move > BATCH_SIZE:

        raise RuntimeError(
            "Planner produced an operation "
            "larger than 100 items."
        )

    # --------------------------------------------------------
    # 14. Execute exact move plan.
    # --------------------------------------------------------

    (
        snapshot,
        reorder_operations,
        reorder_duration,
    ) = execute_chunked_moves(
        operations,
        token,
        snapshot,
    )

    # --------------------------------------------------------
    # 15. Verify exact final Spotify state.
    # --------------------------------------------------------

    final_count = verify_playlist(
        desired_ids,
        token,
    )

    # --------------------------------------------------------
    # 16. Write log.
    # --------------------------------------------------------

    write_summary(
        cumulative_count=len(
            cumulative_tracks
        ),
        pretty_count=len(
            pretty_ids
        ),
        target_count=len(
            desired_ids
        ),
        original_count=original_count,
        duplicate_count=duplicate_count,
        removed_count=len(
            remove_ids
        ),
        added_cumulative_count=(
            added_cumulative_count
        ),
        added_pretty_count=(
            added_pretty_count
        ),
        final_count=final_count,
        reorder_operations=(
            reorder_operations
        ),
        reorder_duration=(
            reorder_duration
        ),
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
