import argparse
import os
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
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

TITLE_THRESHOLD = 0.92
ARTIST_THRESHOLD = 0.92
DURATION_TOLERANCE = 5

# If more than this many positions are wrong, rebuilding the
# playlist is substantially faster than individual reordering.
REBUILD_MISMATCH_THRESHOLD = 100

# Spotify allows a maximum of 100 URIs in add/replace requests.
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
DESTINATION_PLAYLIST_ID = ARGS.destination_playlist_id
LOG_PATH = ARGS.log

PROJECT_DIR = Path(__file__).resolve().parent


# ============================================================
# LOGGING
# ============================================================

def resolve_log_path(value):
    value = str(value).strip().lstrip("/\\")

    if not value:
        raise ValueError("--log cannot be empty")

    path = (PROJECT_DIR / value).resolve()

    try:
        path.relative_to(PROJECT_DIR)
    except ValueError:
        raise ValueError(
            "--log must stay inside the project directory"
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

        print(f"Log written to {self.path}")


LOGGER = Logger(LOG_PATH)


# ============================================================
# SPOTIFY
# ============================================================

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

API = "https://api.spotify.com/v1"


def get_access_token():
    print("Refreshing Spotify token...")

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

    return response.json()["access_token"]


def spotify_request(
    method,
    url,
    token,
    **kwargs,
):
    headers = kwargs.pop("headers", {})

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
            retry_after_header = (
                response.headers.get(
                    "Retry-After",
                    "5",
                )
            )

            try:
                retry_after = int(
                    retry_after_header
                )
            except ValueError:
                retry_after = 5

            print(
                f"Spotify rate limit. "
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
# TEXT / MATCHING
# ============================================================

def normalize_text(value):
    if not value:
        return ""

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        c
        for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = value.replace(
        "&",
        " and ",
    )

    value = re.sub(
        r"\b(feat\.?|ft\.?|featuring)\b",
        " ",
        value,
    )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def artists_text(track):
    return " ".join(
        normalize_text(
            artist.get("name", "")
        )
        for artist in track.get(
            "artists",
            [],
        )
        if artist.get("name")
    ).strip()


def artists_text_from_candidate(track):
    return normalize_text(
        track.get(
            "artist",
            "",
        )
    )


def similarity(a, b):
    return SequenceMatcher(
        None,
        normalize_text(a),
        normalize_text(b),
    ).ratio()


def duplicate(candidate, existing):
    if not candidate or not existing:
        return False

    candidate_id = candidate.get(
        "track_id"
    )

    existing_id = existing.get(
        "id"
    )

    if (
        candidate_id
        and candidate_id == existing_id
    ):
        return True

    title_score = similarity(
        candidate.get(
            "title",
            "",
        ),
        existing.get(
            "name",
            "",
        ),
    )

    if title_score < TITLE_THRESHOLD:
        return False

    artist_score = SequenceMatcher(
        None,
        artists_text_from_candidate(
            candidate
        ),
        artists_text(existing),
    ).ratio()

    if artist_score < ARTIST_THRESHOLD:
        return False

    candidate_seconds = candidate.get(
        "duration",
        0,
    )

    existing_seconds = (
        existing.get(
            "duration_ms",
            0,
        )
        / 1000
    )

    return (
        abs(
            candidate_seconds
            - existing_seconds
        )
        <= DURATION_TOLERANCE
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

    # Remove duplicate Spotify IDs.
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
# DESTINATION PLAYLIST
# ============================================================

def get_playlist_items(token):
    print(
        "Reading destination playlist..."
    )

    url = (
        f"{API}/playlists/"
        f"{DESTINATION_PLAYLIST_ID}/items"
    )

    params = {
        "limit": 50,
        "fields": (
            "items("
            "item("
            "id,"
            "type,"
            "uri,"
            "name,"
            "artists,"
            "duration_ms"
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

            if not track.get(
                "id"
            ):
                continue

            tracks.append(track)

        print(
            f"  Read page {page}: "
            f"{len(tracks)} tracks"
        )

        url = data.get(
            "next"
        )

        # `next` already contains
        # the query parameters.
        params = None

    print(
        f"Destination playlist contains "
        f"{len(tracks)} tracks."
    )

    return tracks


# ============================================================
# DUPLICATE DETECTION
# ============================================================

def find_duplicate(
    candidate,
    existing_tracks,
):
    candidate_id = candidate[
        "track_id"
    ]

    # Fast path: exact Spotify ID.
    for track in existing_tracks:

        if track.get("id") == candidate_id:
            return track, "exact"

    # Slower fuzzy comparison.
    for track in existing_tracks:

        if duplicate(
            candidate,
            track,
        ):
            return track, "fuzzy"

    return None, None


def determine_new_tracks(
    archive,
    existing,
):
    new_tracks = []

    exact = 0
    fuzzy = 0

    # Maps archive Spotify ID to the
    # actual destination Spotify ID.
    #
    # Normally they are identical.
    # For fuzzy matches they can differ.
    destination_mapping = {}

    # Keep a working list so that two
    # archive tracks don't both get
    # classified as new when the first
    # one has already been accepted.
    comparison_tracks = list(
        existing
    )

    print(
        "Comparing archive with "
        "destination..."
    )

    for index, candidate in enumerate(
        archive,
        start=1,
    ):

        match, match_type = (
            find_duplicate(
                candidate,
                comparison_tracks,
            )
        )

        if match:

            destination_mapping[
                candidate["track_id"]
            ] = match["id"]

            if match_type == "exact":
                exact += 1
            else:
                fuzzy += 1

        else:

            new_tracks.append(
                candidate
            )

            destination_mapping[
                candidate["track_id"]
            ] = candidate["track_id"]

            # Add a synthetic track to the
            # comparison list so duplicates
            # inside the archive are avoided.
            comparison_tracks.append(
                {
                    "id": candidate[
                        "track_id"
                    ],
                    "name": candidate[
                        "title"
                    ],
                    "artists": [
                        {
                            "name": candidate[
                                "artist"
                            ]
                        }
                    ],
                    "duration_ms": (
                        candidate[
                            "duration"
                        ]
                        * 1000
                    ),
                }
            )

        if (
            index % 250 == 0
            or index == len(archive)
        ):
            print(
                f"  Compared "
                f"{index}/{len(archive)}"
            )

    print()
    print(
        f"Exact duplicates: {exact}"
    )

    print(
        f"Fuzzy duplicates: {fuzzy}"
    )

    print(
        f"New tracks:       "
        f"{len(new_tracks)}"
    )

    return (
        new_tracks,
        exact,
        fuzzy,
        destination_mapping,
    )


# ============================================================
# ADD NEW TRACKS
# ============================================================

def add_tracks_at_front(
    tracks,
    token,
):
    """
    Add new tracks to the beginning of
    the playlist in newest -> oldest order.

    This is important for future runs:
    new archive tracks naturally land at
    the front, so the playlist doesn't need
    a massive reorder every day.
    """

    if not tracks:
        return 0

    if DRY_RUN:
        print(
            f"DRY RUN: would add "
            f"{len(tracks)} tracks."
        )

        return len(tracks)

    # Archive is oldest -> newest.
    #
    # We want newest -> oldest at the
    # beginning of the destination.
    ordered = list(
        reversed(tracks)
    )

    total = len(ordered)

    print(
        f"Adding {total} new tracks "
        f"to the front..."
    )

    added = 0

    # We process from newest -> oldest.
    #
    # Every batch is inserted at position 0.
    # To preserve the overall newest ->
    # oldest order, batches themselves need
    # to be inserted carefully.
    #
    # Easiest safe approach:
    # add all batches in reverse batch order.
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

    for batch_number, batch in enumerate(
        reversed(batches),
        start=1,
    ):

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
            f"{added}/{total} new tracks"
        )

    return added


# ============================================================
# DESIRED ORDER
# ============================================================

def build_desired_order(
    archive,
    current,
    destination_mapping,
):
    """
    Desired order:

        archive tracks newest -> oldest
        followed by any tracks that aren't
        represented by the archive.

    This preserves the permanent-playlist
    behavior while keeping unrelated tracks
    at the end.
    """

    desired = []

    used_destination_ids = set()

    # Newest -> oldest.
    for candidate in reversed(
        archive
    ):

        archive_id = candidate[
            "track_id"
        ]

        destination_id = (
            destination_mapping.get(
                archive_id
            )
        )

        if not destination_id:
            continue

        if destination_id in (
            used_destination_ids
        ):
            continue

        desired.append(
            destination_id
        )

        used_destination_ids.add(
            destination_id
        )

    # Preserve non-archive tracks after
    # the archive tracks.
    for track in current:

        track_id = track.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in (
            used_destination_ids
        ):
            continue

        desired.append(
            track_id
        )

        used_destination_ids.add(
            track_id
        )

    return desired


# ============================================================
# ORDER ANALYSIS
# ============================================================

def count_mismatches(
    current_ids,
    desired_ids,
):
    length = min(
        len(current_ids),
        len(desired_ids),
    )

    return sum(
        1
        for index in range(length)
        if current_ids[index]
        != desired_ids[index]
    )


# ============================================================
# PLAYLIST REBUILD
# ============================================================

def rebuild_playlist(
    desired_ids,
    token,
):
    """
    Fast path for badly disordered playlists.

    Spotify allows up to 100 URIs in a replace
    request and up to 100 URIs in an add request.

    We therefore:

        1. Replace the playlist with the
           first 100 desired tracks.
        2. Append the remaining tracks in
           100-track batches.

    This is dramatically faster than issuing
    one reorder request per track.

    IMPORTANT:
    If a later request fails, the playlist may
    temporarily contain only part of the desired
    list. Requests are retried by spotify_request,
    but a hard failure still aborts the workflow.
    """

    if not desired_ids:
        raise RuntimeError(
            "Desired playlist is empty."
        )

    if DRY_RUN:
        print(
            "DRY RUN: playlist would be "
            "rebuilt."
        )

        return 0

    total = len(desired_ids)

    print()
    print(
        "Large reorder detected."
    )

    print(
        f"Rebuilding {total} playlist items "
        f"using 100-item batches..."
    )

    # --------------------------------------------------------
    # First 100: replace the playlist.
    # --------------------------------------------------------

    first_batch = desired_ids[
        :BATCH_SIZE
    ]

    first_uris = [
        f"spotify:track:{track_id}"
        for track_id in first_batch
    ]

    print(
        f"  Replacing first "
        f"{len(first_batch)} items..."
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
                f"{DESTINATION_PLAYLIST_ID}"
                f"/items"
            ),
            token,
            json={
                "uris": uris,
            },
        )

        completed += len(batch)

        print(
            f"  Rebuilt "
            f"{completed}/{total}"
        )

    print(
        "Playlist rebuild complete."
    )

    return (
        1
        + (
            len(remaining)
            + BATCH_SIZE
            - 1
        )
        // BATCH_SIZE
        if remaining
        else 1
    )


# ============================================================
# SMALL REORDER
# ============================================================

def reorder_small(
    current_ids,
    desired_ids,
    token,
):
    """
    For small changes, use Spotify's native
    range reorder operation.

    This avoids rebuilding the whole playlist
    when only a small number of positions differ.
    """

    if current_ids == desired_ids:
        print(
            "Playlist is already correctly "
            "ordered."
        )

        return 0, 0.0

    if DRY_RUN:
        print(
            "DRY RUN: playlist would be "
            "reordered."
        )

        return 0, 0.0

    print(
        "Performing targeted reorder..."
    )

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

    total = len(desired_ids)

    for target in range(total):

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
                "Desired track was not found "
                "in the current playlist."
            )

        # Extend the move into the longest
        # contiguous desired block available
        # at the source position.
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

        spotify_response = spotify_request(
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

        snapshot = (
            spotify_response.json()
            ["snapshot_id"]
        )

        block = working[
            source:source + range_length
        ]

        del working[
            source:source + range_length
        ]

        working[
            target:target
        ] = block

        moves += 1

        print(
            f"  Reorder move "
            f"{moves}: "
            f"position {source} -> {target}, "
            f"{range_length} item(s)"
        )

    duration = (
        time.perf_counter()
        - started
    )

    print(
        f"Targeted reorder complete: "
        f"{moves} move(s) in "
        f"{duration:.2f}s."
    )

    return moves, duration


# ============================================================
# VERIFY
# ============================================================

def verify_playlist(
    desired_ids,
    token,
):
    print(
        "Verifying final playlist..."
    )

    current = get_playlist_items(
        token
    )

    current_ids = [
        track["id"]
        for track in current
    ]

    if current_ids != desired_ids:

        # Provide useful diagnostics.
        mismatch_count = count_mismatches(
            current_ids,
            desired_ids,
        )

        raise RuntimeError(
            "Playlist verification failed. "
            f"{mismatch_count} positions differ."
        )

    print(
        "Playlist verification passed."
    )

    return len(current)


# ============================================================
# SUMMARY
# ============================================================

def write_summary(
    archive_count,
    playlist_count,
    exact,
    fuzzy,
    new_count,
    added,
    reorder_moves,
    reorder_duration,
    rebuild_used,
):
    LOGGER.add(
        "=" * 60
    )

    LOGGER.add(
        "Spotify Playlist Archive Sync"
    )

    LOGGER.add(
        "Newest -> Oldest"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Archive tracks:       "
        f"{archive_count}"
    )

    LOGGER.add(
        f"Playlist tracks:      "
        f"{playlist_count}"
    )

    LOGGER.add(
        f"Exact duplicates:     "
        f"{exact}"
    )

    LOGGER.add(
        f"Fuzzy duplicates:     "
        f"{fuzzy}"
    )

    LOGGER.add(
        f"New tracks found:     "
        f"{new_count}"
    )

    LOGGER.add(
        f"Tracks added:         "
        f"{added}"
    )

    LOGGER.add(
        f"Rebuild used:         "
        f"{rebuild_used}"
    )

    LOGGER.add(
        f"Reorder moves:        "
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
        "Newest -> Oldest"
    )

    print("=" * 60)

    # --------------------------------------------------------
    # 1. Read archive.
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
    # 3. Read destination.
    # --------------------------------------------------------

    current = get_playlist_items(
        token
    )

    # Keep the original destination
    # before any changes.
    original_current = list(
        current
    )

    # --------------------------------------------------------
    # 4. Find new tracks.
    # --------------------------------------------------------

    (
        new_tracks,
        exact,
        fuzzy,
        destination_mapping,
    ) = determine_new_tracks(
        archive,
        current,
    )

    # --------------------------------------------------------
    # 5. Add new tracks at the front.
    #
    # This is important for future runs.
    # --------------------------------------------------------

    added = add_tracks_at_front(
        new_tracks,
        token,
    )

    # If tracks were added, fetch the actual
    # Spotify playlist again.
    #
    # If nothing was added, we can continue
    # using the existing playlist.
    if added:
        current = get_playlist_items(
            token
        )

    # --------------------------------------------------------
    # 6. Build exact desired order.
    # --------------------------------------------------------

    print(
        "Calculating desired playlist order..."
    )

    desired_ids = build_desired_order(
        archive,
        current,
        destination_mapping,
    )

    current_ids = [
        track["id"]
        for track in current
    ]

    if len(current_ids) != len(
        desired_ids
    ):
        raise RuntimeError(
            "Current and desired playlist "
            "lengths differ unexpectedly."
        )

    if set(current_ids) != set(
        desired_ids
    ):
        raise RuntimeError(
            "Current and desired playlist "
            "contents differ unexpectedly."
        )

    # --------------------------------------------------------
    # 7. Check whether anything needs doing.
    # --------------------------------------------------------

    if current_ids == desired_ids:

        print(
            "Playlist is already correctly "
            "ordered."
        )

        reorder_moves = 0
        reorder_duration = 0.0
        rebuild_used = False

    else:

        mismatches = count_mismatches(
            current_ids,
            desired_ids,
        )

        print(
            f"Playlist order differs at "
            f"{mismatches} position(s)."
        )

        # ----------------------------------------------------
        # 8. Large mismatch:
        #    rebuild using 100-item batches.
        # ----------------------------------------------------

        if (
            mismatches
            >= REBUILD_MISMATCH_THRESHOLD
        ):

            started = time.perf_counter()

            rebuild_count = (
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
            reorder_moves = rebuild_count

        # ----------------------------------------------------
        # 9. Small mismatch:
        #    use targeted range moves.
        # ----------------------------------------------------

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
    # 10. Verify everything.
    # --------------------------------------------------------

    final_count = verify_playlist(
        desired_ids,
        token,
    )

    # --------------------------------------------------------
    # 11. Write log.
    # --------------------------------------------------------

    write_summary(
        archive_count=len(archive),
        playlist_count=final_count,
        exact=exact,
        fuzzy=fuzzy,
        new_count=len(new_tracks),
        added=added,
        reorder_moves=reorder_moves,
        reorder_duration=reorder_duration,
        rebuild_used=rebuild_used,
    )

    print(
        "Done."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
