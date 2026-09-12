import argparse
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
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
# DEDUP CONFIG
# ============================================================

# Strong metadata match:
#
# Same normalized title + same normalized artists + duration
# within this tolerance.
METADATA_DURATION_TOLERANCE_MS = 5000

# Fuzzy match:
#
# A fuzzy duplicate must satisfy ALL of these:
#
#   title similarity >= FUZZY_TITLE_THRESHOLD
#   artist similarity >= FUZZY_ARTIST_THRESHOLD
#   duration difference <= FUZZY_DURATION_TOLERANCE_MS
#
# These are intentionally conservative because a false positive
# is worse than leaving an obscure duplicate in the archive.
FUZZY_TITLE_THRESHOLD = 0.94
FUZZY_ARTIST_THRESHOLD = 0.94
FUZZY_DURATION_TOLERANCE_MS = 5000

# Do not fuzzy-match extremely short tracks. Tiny duration
# differences on 30-60 second tracks can otherwise become noisy.
FUZZY_MIN_DURATION_MS = 90000


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
# GENERAL TEXT / TRACK HELPERS
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


def duration_ms(seconds):
    return int(seconds * 1000)


def normalize_text(value):
    """
    Normalize text for duplicate detection.

    This deliberately does NOT remove meaningful words such as
    remix/live/acoustic/etc. Those can represent different
    recordings.
    """

    value = str(value or "").strip()

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = (
        value
        .replace("’", "'")
        .replace("`", "'")
        .replace("&", "and")
    )

    value = value.lower()

    # Normalize common separators.
    value = re.sub(
        r"\s*&\s*",
        " and ",
        value,
    )

    # Collapse punctuation spacing but don't remove all
    # punctuation. We want to avoid turning distinct titles
    # into identical strings unnecessarily.
    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalize_title(title):
    return normalize_text(title)


def extract_artist_names(value):
    """
    Extract artist names from the archive markdown.

    Example:

        [What So Not](artist-url), [Jack Blom](artist-url)

    becomes:

        ["What So Not", "Jack Blom"]
    """

    artists = re.findall(
        r"\[([^\]]+)\]"
        r"\(https://open\.spotify\.com/artist/"
        r"[A-Za-z0-9]+\)",
        value,
    )

    if artists:
        return artists

    # Fallback for unexpected archive formatting.
    cleaned = re.sub(
        r"\[[^\]]+\]\([^)]+\)",
        "",
        value,
    )

    return [
        item.strip()
        for item in cleaned.split(",")
        if item.strip()
    ]


def normalize_artists(artists):
    normalized = [
        normalize_text(artist)
        for artist in artists
        if normalize_text(artist)
    ]

    return " | ".join(
        normalized
    )


def track_fingerprint(track):
    """
    Strong normalized metadata fingerprint.

    Spotify ID is deliberately NOT part of this fingerprint.
    """

    return (
        normalize_title(
            track.get("title", "")
        ),
        normalize_artists(
            track.get("artists", [])
        ),
    )


def duration_difference_ms(a, b):
    return abs(
        int(a.get("duration_ms", 0))
        - int(b.get("duration_ms", 0))
    )


def similarity(a, b):
    return SequenceMatcher(
        None,
        normalize_text(a),
        normalize_text(b),
    ).ratio()


def fuzzy_scores(a, b):
    title_score = similarity(
        a.get("title", ""),
        b.get("title", ""),
    )

    artist_score = similarity(
        normalize_artists(
            a.get("artists", [])
        ),
        normalize_artists(
            b.get("artists", [])
        ),
    )

    duration_diff = (
        duration_difference_ms(a, b)
    )

    return (
        title_score,
        artist_score,
        duration_diff,
    )


def strong_metadata_match(a, b):
    """
    Strong non-fuzzy duplicate detection.

    Same normalized title + same normalized artist list +
    duration within tolerance.
    """

    if (
        track_fingerprint(a)
        != track_fingerprint(b)
    ):
        return False

    return (
        duration_difference_ms(a, b)
        <= METADATA_DURATION_TOLERANCE_MS
    )


def fuzzy_duplicate_match(a, b):
    """
    Conservative fuzzy duplicate detection.

    All three conditions must pass.
    """

    duration_a = int(
        a.get("duration_ms", 0)
    )

    duration_b = int(
        b.get("duration_ms", 0)
    )

    if (
        duration_a < FUZZY_MIN_DURATION_MS
        or duration_b < FUZZY_MIN_DURATION_MS
    ):
        return False

    (
        title_score,
        artist_score,
        duration_diff,
    ) = fuzzy_scores(a, b)

    return (
        title_score
        >= FUZZY_TITLE_THRESHOLD
        and
        artist_score
        >= FUZZY_ARTIST_THRESHOLD
        and
        duration_diff
        <= FUZZY_DURATION_TOLERANCE_MS
    )


def classify_duplicate(a, b):
    """
    Return:

        "id"
        "metadata"
        "fuzzy"
        None

    Exact Spotify ID is the strongest possible match.
    """

    if (
        a.get("track_id")
        and
        a.get("track_id")
        == b.get("track_id")
    ):
        return "id"

    if strong_metadata_match(
        a,
        b,
    ):
        return "metadata"

    if fuzzy_duplicate_match(
        a,
        b,
    ):
        return "fuzzy"

    return None


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

    tracks = []
    seen_ids = set()

    for line in response.text.splitlines():

        if not line.startswith("|"):
            continue

        match = TRACK_URL_RE.search(
            line
        )

        if not match:
            continue

        track_id = match.group(1)

        if track_id in seen_ids:
            continue

        columns = [
            column.strip()
            for column
            in line.strip("|").split("|")
        ]

        # Expected:
        #
        # # | Title | Artist(s) | Album | Length
        #
        if len(columns) < 5:
            continue

        title = columns[1]
        artist_column = columns[2]
        duration = columns[4]

        artists = extract_artist_names(
            artist_column
        )

        duration_seconds = parse_duration(
            duration
        )

        tracks.append(
            {
                "track_id": track_id,
                "title": title,
                "artists": artists,
                "duration": duration,
                "duration_ms": duration_ms(
                    duration_seconds
                ),
                "source": "pretty",
                "added": None,
            }
        )

        seen_ids.add(track_id)

    print(
        f"Pretty archive contains "
        f"{len(tracks)} unique tracks."
    )

    return tracks


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
        artist_column = columns[1]
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

        artists = extract_artist_names(
            artist_column
        )

        duration_seconds = parse_duration(
            duration
        )

        tracks.append(
            {
                "track_id": match.group(1),
                "title": title,
                "artists": artists,
                "duration": duration,
                "duration_ms": duration_ms(
                    duration_seconds
                ),
                "source": "cumulative",
                "added": added_date,
            }
        )

    # The cumulative archive is normalized internally
    # oldest -> newest.
    tracks.sort(
        key=lambda track: track["added"]
    )

    # Exact Spotify-ID dedup first.
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
        f"{len(result)} unique Spotify IDs."
    )

    return result


# ============================================================
# ARCHIVE DEDUPLICATION
# ============================================================

def choose_better_canonical(
    existing,
    candidate,
):
    """
    Decide which archive track should represent a duplicate
    cluster.

    Priority:

        1. PRETTY
        2. newest cumulative track

    This function is mainly defensive because the archive
    tracks are already processed in the correct priority order.
    """

    existing_source = existing.get(
        "source"
    )

    candidate_source = candidate.get(
        "source"
    )

    if (
        candidate_source == "pretty"
        and existing_source != "pretty"
    ):
        return candidate

    if (
        existing_source == "pretty"
        and candidate_source != "pretty"
    ):
        return existing

    existing_added = existing.get(
        "added"
    )

    candidate_added = candidate.get(
        "added"
    )

    if (
        candidate_added
        and
        (
            not existing_added
            or candidate_added > existing_added
        )
    ):
        return candidate

    return existing


def find_archive_duplicate(
    track,
    canonical_tracks,
):
    """
    Find the strongest matching canonical track.

    Matching priority:

        1. Exact Spotify ID
        2. Exact normalized metadata + duration
        3. Conservative fuzzy metadata
    """

    # --------------------------------------------------------
    # Exact Spotify ID.
    # --------------------------------------------------------

    for canonical in canonical_tracks:

        if (
            track["track_id"]
            == canonical["track_id"]
        ):
            return (
                canonical,
                "id",
            )

    # --------------------------------------------------------
    # Strong normalized metadata.
    # --------------------------------------------------------

    for canonical in canonical_tracks:

        if strong_metadata_match(
            track,
            canonical,
        ):
            return (
                canonical,
                "metadata",
            )

    # --------------------------------------------------------
    # Fuzzy matching.
    #
    # This is deliberately only attempted after the stronger
    # checks have failed.
    # --------------------------------------------------------

    best_match = None
    best_score = None

    for canonical in canonical_tracks:

        if not fuzzy_duplicate_match(
            track,
            canonical,
        ):
            continue

        (
            title_score,
            artist_score,
            duration_diff,
        ) = fuzzy_scores(
            track,
            canonical,
        )

        score = (
            title_score
            + artist_score
            - (
                duration_diff
                / 1000000
            )
        )

        if (
            best_score is None
            or score > best_score
        ):
            best_score = score
            best_match = canonical

    if best_match is not None:
        return (
            best_match,
            "fuzzy",
        )

    return (
        None,
        None,
    )


def deduplicate_archive(
    pretty_tracks,
    cumulative_tracks,
):
    """
    Deduplicate the complete archive.

    Processing order is intentional:

        1. PRETTY in exact playlist order.
        2. CUMULATIVE newest -> oldest.

    Therefore:

        PRETTY always wins.

        If a duplicate exists only in cumulative,
        the newest cumulative version wins.

    Returns:

        canonical_pretty_tracks
        canonical_cumulative_tracks
        canonical_by_id
        archive_id_to_canonical_id
        statistics
    """

    canonical_tracks = []

    canonical_pretty_tracks = []
    canonical_cumulative_tracks = []

    archive_id_to_canonical_id = {}

    stats = Counter()

    duplicate_reports = []

    # --------------------------------------------------------
    # PRETTY FIRST
    # --------------------------------------------------------

    for track in pretty_tracks:

        canonical, match_type = (
            find_archive_duplicate(
                track,
                canonical_tracks,
            )
        )

        if canonical is None:

            canonical_tracks.append(
                track
            )

            canonical_pretty_tracks.append(
                track
            )

            archive_id_to_canonical_id[
                track["track_id"]
            ] = track["track_id"]

            stats["canonical_pretty"] += 1

            continue

        # A duplicate already exists.
        #
        # Because pretty is processed first, another pretty
        # occurrence loses to the first occurrence.
        stats[
            f"duplicate_{match_type}"
        ] += 1

        archive_id_to_canonical_id[
            track["track_id"]
        ] = canonical["track_id"]

        duplicate_reports.append(
            {
                "duplicate": track,
                "canonical": canonical,
                "match": match_type,
            }
        )

    # --------------------------------------------------------
    # CUMULATIVE NEWEST -> OLDEST
    #
    # The input is oldest -> newest, so reverse it.
    # --------------------------------------------------------

    for track in reversed(
        cumulative_tracks
    ):

        canonical, match_type = (
            find_archive_duplicate(
                track,
                canonical_tracks,
            )
        )

        if canonical is None:

            canonical_tracks.append(
                track
            )

            canonical_cumulative_tracks.append(
                track
            )

            archive_id_to_canonical_id[
                track["track_id"]
            ] = track["track_id"]

            stats[
                "canonical_cumulative"
            ] += 1

            continue

        # Already represented by a better canonical track.
        stats[
            f"duplicate_{match_type}"
        ] += 1

        archive_id_to_canonical_id[
            track["track_id"]
        ] = canonical["track_id"]

        duplicate_reports.append(
            {
                "duplicate": track,
                "canonical": canonical,
                "match": match_type,
            }
        )

    # --------------------------------------------------------
    # Report.
    # --------------------------------------------------------

    total_duplicates = sum(
        value
        for key, value
        in stats.items()
        if key.startswith("duplicate_")
    )

    stats[
        "total_duplicates"
    ] = total_duplicates

    print()
    print(
        "Archive deduplication:"
    )

    print(
        f"  Canonical pretty:       "
        f"{len(canonical_pretty_tracks)}"
    )

    print(
        f"  Canonical cumulative:   "
        f"{len(canonical_cumulative_tracks)}"
    )

    print(
        f"  Exact ID duplicates:    "
        f"{stats['duplicate_id']}"
    )

    print(
        f"  Metadata duplicates:    "
        f"{stats['duplicate_metadata']}"
    )

    print(
        f"  Fuzzy duplicates:       "
        f"{stats['duplicate_fuzzy']}"
    )

    print(
        f"  Total archive duplicates: "
        f"{total_duplicates}"
    )

    if duplicate_reports:
        print()
        print(
            "Archive duplicate examples:"
        )

        for report in duplicate_reports[
            :20
        ]:

            duplicate = report[
                "duplicate"
            ]

            canonical = report[
                "canonical"
            ]

            match_type = report[
                "match"
            ]

            print(
                f"  [{match_type}] "
                f"{duplicate['title']} "
                f"({duplicate['track_id']})"
            )

            print(
                f"      KEEP "
                f"{canonical['track_id']}"
            )

            if match_type == "fuzzy":

                (
                    title_score,
                    artist_score,
                    duration_diff,
                ) = fuzzy_scores(
                    duplicate,
                    canonical,
                )

                print(
                    f"      title={title_score:.3f} "
                    f"artist={artist_score:.3f} "
                    f"duration_diff="
                    f"{duration_diff}ms"
                )

            else:

                print(
                    f"      match={match_type}"
                )

        if len(duplicate_reports) > 20:
            print(
                f"  ...and "
                f"{len(duplicate_reports) - 20} "
                f"more."
            )

    # --------------------------------------------------------
    # The canonical tracks are currently:
    #
    #   pretty canonical tracks
    #   cumulative canonical tracks
    #
    # But cumulative canonical tracks were processed newest
    # -> oldest, which is exactly the order we need.
    # --------------------------------------------------------

    desired_tracks = (
        canonical_pretty_tracks
        + canonical_cumulative_tracks
    )

    desired_ids = [
        track["track_id"]
        for track in desired_tracks
    ]

    if len(desired_ids) != len(
        set(desired_ids)
    ):
        raise RuntimeError(
            "Archive deduplication failed: "
            "canonical target still contains "
            "duplicate Spotify IDs."
        )

    return (
        canonical_pretty_tracks,
        canonical_cumulative_tracks,
        desired_tracks,
        archive_id_to_canonical_id,
        stats,
    )


# ============================================================
# BUILD EXACT TARGET
# ============================================================

def build_target_order(
    pretty_tracks,
    cumulative_tracks,
):
    """
    Build the exact desired order from already-deduplicated
    archive tracks.

    PRETTY:
        exact current playlist order

    CUMULATIVE:
        newest -> oldest

    PRETTY always occupies the top layer.
    """

    desired = []
    used = set()

    # --------------------------------------------------------
    # PRETTY
    # --------------------------------------------------------

    for track in pretty_tracks:

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

    pretty_count = len(
        desired
    )

    # --------------------------------------------------------
    # CUMULATIVE ONLY
    # --------------------------------------------------------

    cumulative_only = []

    for track in cumulative_tracks:

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

            tracks.append(
                track
            )

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
# DESTINATION DEDUP / ARCHIVE MATCHING
# ============================================================

def make_destination_track(
    destination_track,
    archive_by_id,
):
    """
    Turn a Spotify destination item into our internal track
    representation.

    If the track exists in the archive, use the archive's
    metadata because it is richer and deterministic.

    Otherwise we retain the Spotify item's name but do not
    attempt fuzzy matching against incomplete metadata.
    """

    track_id = destination_track.get(
        "id"
    )

    archive_track = archive_by_id.get(
        track_id
    )

    if archive_track:
        return dict(
            archive_track
        )

    return {
        "track_id": track_id,
        "title": destination_track.get(
            "name",
            "",
        ),
        "artists": [],
        "duration": "",
        "duration_ms": 0,
        "source": "destination",
        "added": None,
    }


def find_destination_duplicate_groups(
    destination_tracks,
    archive_by_id,
    canonical_ids,
):
    """
    Find duplicate destination items.

    Matching is performed against canonical archive tracks.

    Important:
        - Exact Spotify IDs are always handled.
        - Metadata/fuzzy matching is only possible when the
          destination track has archive metadata.
    """

    duplicate_ids = set()
    duplicate_reports = []

    canonical_track_list = [
        archive_by_id[
            track_id
        ]
        for track_id in canonical_ids
        if track_id in archive_by_id
    ]

    for position, destination_track in enumerate(
        destination_tracks
    ):

        track_id = destination_track.get(
            "id"
        )

        if not track_id:
            continue

        destination_internal = (
            make_destination_track(
                destination_track,
                archive_by_id,
            )
        )

        # ----------------------------------------------------
        # Exact ID:
        # if it is canonical, it is fine.
        # If it isn't canonical but maps to one, it must be
        # removed.
        # ----------------------------------------------------

        if track_id not in canonical_ids:

            for canonical in canonical_track_list:

                match_type = classify_duplicate(
                    destination_internal,
                    canonical,
                )

                if match_type:

                    duplicate_ids.add(
                        track_id
                    )

                    duplicate_reports.append(
                        {
                            "position": position,
                            "duplicate": destination_internal,
                            "canonical": canonical,
                            "match": match_type,
                        }
                    )

                    break

    # --------------------------------------------------------
    # Exact duplicate occurrences already present in the
    # destination.
    # --------------------------------------------------------

    counts = Counter(
        track.get("id")
        for track in destination_tracks
        if track.get("id")
    )

    for track_id, count in counts.items():

        if count <= 1:
            continue

        # Remove the whole ID. The canonical copy will be
        # re-added if necessary.
        duplicate_ids.add(
            track_id
        )

        for position, track in enumerate(
            destination_tracks
        ):

            if track.get("id") != track_id:
                continue

            duplicate_reports.append(
                {
                    "position": position,
                    "duplicate": {
                        "track_id": track_id,
                        "title": track.get(
                            "name",
                            "",
                        ),
                        "artists": [],
                        "duration": "",
                        "duration_ms": 0,
                    },
                    "canonical": {
                        "track_id": track_id,
                        "title": track.get(
                            "name",
                            "",
                        ),
                        "artists": [],
                        "duration": "",
                        "duration_ms": 0,
                    },
                    "match": "id",
                }
            )

    return (
        duplicate_ids,
        duplicate_reports,
    )


def report_destination_duplicates(
    duplicate_reports,
):
    if not duplicate_reports:

        print(
            "Destination deduplication: "
            "no duplicate archive matches found."
        )

        return

    print()
    print(
        "Destination duplicate matches:"
    )

    for report in duplicate_reports[
        :20
    ]:

        duplicate = report[
            "duplicate"
        ]

        canonical = report[
            "canonical"
        ]

        match_type = report[
            "match"
        ]

        print(
            f"  [{match_type}] "
            f"{duplicate.get('title', 'Unknown')} "
            f"({duplicate.get('track_id')})"
        )

        print(
            f"      KEEP "
            f"{canonical.get('track_id')}"
        )

        if match_type == "fuzzy":

            (
                title_score,
                artist_score,
                duration_diff,
            ) = fuzzy_scores(
                duplicate,
                canonical,
            )

            print(
                f"      title={title_score:.3f} "
                f"artist={artist_score:.3f} "
                f"duration_diff="
                f"{duration_diff}ms"
            )

    if len(duplicate_reports) > 20:
        print(
            f"  ...and "
            f"{len(duplicate_reports) - 20} "
            f"more."
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

    Removing an entire ID is intentional. If that ID represents
    a duplicate/replaced Spotify release, the canonical ID will
    be restored during the normal addition phase.
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

    Batches are inserted in reverse order so the final order
    remains identical to PRETTY.
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

        1. Find earliest incorrect position.
        2. Find desired track in remaining playlist.
        3. Find longest contiguous target block.
        4. Move <= 100 items.
        5. Simulate locally.
        6. Continue.

    This is a greedy maximal-block strategy, not a claim of
    mathematical global minimum for arbitrary permutations.
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
    archive_duplicate_count,
    archive_exact_id_duplicates,
    archive_metadata_duplicates,
    archive_fuzzy_duplicates,
    destination_duplicate_count,
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
        "Deduplication: exact ID + metadata + fuzzy"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Cumulative Spotify IDs:       "
        f"{cumulative_count}"
    )

    LOGGER.add(
        f"Pretty Spotify IDs:            "
        f"{pretty_count}"
    )

    LOGGER.add(
        f"Target tracks:                 "
        f"{target_count}"
    )

    LOGGER.add(
        f"Original destination:          "
        f"{original_count}"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Archive duplicate total:       "
        f"{archive_duplicate_count}"
    )

    LOGGER.add(
        f"Archive exact-ID duplicates:   "
        f"{archive_exact_id_duplicates}"
    )

    LOGGER.add(
        f"Archive metadata duplicates:   "
        f"{archive_metadata_duplicates}"
    )

    LOGGER.add(
        f"Archive fuzzy duplicates:      "
        f"{archive_fuzzy_duplicates}"
    )

    LOGGER.add(
        f"Destination duplicate matches: "
        f"{destination_duplicate_count}"
    )

    LOGGER.add("")

    LOGGER.add(
        f"IDs removed/normalized:        "
        f"{removed_count}"
    )

    LOGGER.add(
        f"Cumulative tracks added:       "
        f"{added_cumulative_count}"
    )

    LOGGER.add(
        f"Pretty tracks added:           "
        f"{added_pretty_count}"
    )

    LOGGER.add(
        f"Final playlist:                "
        f"{final_count}"
    )

    LOGGER.add("")

    LOGGER.add(
        f"Reorder operations:            "
        f"{reorder_operations}"
    )

    LOGGER.add(
        f"Reorder duration:              "
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
        "Deduplication: exact ID + metadata + fuzzy"
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
    # This is authoritative current playlist order.
    # --------------------------------------------------------

    pretty_tracks = read_pretty()

    if not pretty_tracks:

        raise RuntimeError(
            "Pretty archive returned zero tracks. "
            "Refusing to modify playlist."
        )

    # --------------------------------------------------------
    # 2. Read CUMULATIVE.
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
    # 3. Deduplicate archive.
    #
    # PRETTY is processed first.
    #
    # CUMULATIVE is processed newest -> oldest.
    #
    # Therefore:
    #
    #   pretty > newest cumulative
    # --------------------------------------------------------

    (
        canonical_pretty_tracks,
        canonical_cumulative_tracks,
        desired_archive_tracks,
        archive_id_to_canonical_id,
        dedup_stats,
    ) = deduplicate_archive(
        pretty_tracks,
        cumulative_tracks,
    )

    # --------------------------------------------------------
    # 4. Build exact target.
    # --------------------------------------------------------

    (
        desired_ids,
        cumulative_only_ids,
    ) = build_target_order(
        canonical_pretty_tracks,
        canonical_cumulative_tracks,
    )

    target_set = set(
        desired_ids
    )

    # All canonical archive tracks by Spotify ID.
    archive_by_id = {
        track["track_id"]: track
        for track
        in desired_archive_tracks
    }

    # --------------------------------------------------------
    # 5. Authenticate.
    # --------------------------------------------------------

    token = get_access_token()

    # --------------------------------------------------------
    # 6. Read destination.
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
    # 7. Find destination duplicates/replaced Spotify IDs.
    #
    # Example:
    #
    # destination:
    #     0ZrkXUzjbmmNO8mrqK6Kc2
    #
    # archive canonical:
    #     4ErJ2mnFmvQIWdsL6KNDnq
    #
    # The old destination ID is removed and the canonical
    # pretty ID is subsequently added.
    # --------------------------------------------------------

    (
        destination_duplicate_ids,
        destination_duplicate_reports,
    ) = find_destination_duplicate_groups(
        current_tracks,
        archive_by_id,
        desired_ids,
    )

    report_destination_duplicates(
        destination_duplicate_reports
    )

    # --------------------------------------------------------
    # 8. Count exact duplicate IDs in destination.
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

    # --------------------------------------------------------
    # 9. Remove anything outside the canonical target.
    #
    # Also remove:
    #
    #   - duplicate destination IDs
    #   - old Spotify IDs matched to canonical archive IDs
    #
    # The canonical ID will be restored by the normal add phase.
    # --------------------------------------------------------

    extra_ids = {
        track_id
        for track_id in counts
        if track_id not in target_set
    }

    remove_ids = (
        duplicate_ids
        |
        extra_ids
        |
        destination_duplicate_ids
    )

    print()

    print(
        f"Destination IDs to remove/normalize: "
        f"{len(remove_ids)}"
    )

    print(
        f"  Exact duplicate IDs: "
        f"{len(duplicate_ids)}"
    )

    print(
        f"  Destination-only IDs: "
        f"{len(extra_ids)}"
    )

    print(
        f"  Archive duplicate/replacement IDs: "
        f"{len(destination_duplicate_ids)}"
    )

    # --------------------------------------------------------
    # 10. Get current Spotify snapshot.
    # --------------------------------------------------------

    snapshot = get_playlist_snapshot(
        token
    )

    # --------------------------------------------------------
    # 11. Remove extras / duplicates / replaced IDs.
    # --------------------------------------------------------

    (
        snapshot,
        remove_requests,
    ) = remove_track_ids(
        list(remove_ids),
        token,
        snapshot,
    )

    # --------------------------------------------------------
    # 12. Simulate post-removal state.
    # --------------------------------------------------------

    working_ids = [
        track_id
        for track_id in current_ids
        if track_id not in remove_ids
    ]

    # --------------------------------------------------------
    # 13. Add missing CUMULATIVE-only tracks first.
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
    # 14. Add missing PRETTY tracks second.
    #
    # This ensures the current playlist is always layered on top
    # of historical tracks.
    # --------------------------------------------------------

    (
        working_ids,
        snapshot,
        added_pretty_count,
        pretty_add_requests,
    ) = add_pretty_tracks(
        [
            track["track_id"]
            for track
            in canonical_pretty_tracks
        ],
        working_ids,
        token,
        snapshot,
    )

    # --------------------------------------------------------
    # 15. Sanity-check local state.
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
    # 16. Calculate exact chunked reorder plan.
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
    # 17. Execute exact move plan.
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
    # 18. Verify exact final Spotify state.
    # --------------------------------------------------------

    final_count = verify_playlist(
        desired_ids,
        token,
    )

    # --------------------------------------------------------
    # 19. Write log.
    # --------------------------------------------------------

    write_summary(
        cumulative_count=len(
            cumulative_tracks
        ),
        pretty_count=len(
            pretty_tracks
        ),
        target_count=len(
            desired_ids
        ),
        original_count=original_count,
        archive_duplicate_count=(
            dedup_stats[
                "total_duplicates"
            ]
        ),
        archive_exact_id_duplicates=(
            dedup_stats[
                "duplicate_id"
            ]
        ),
        archive_metadata_duplicates=(
            dedup_stats[
                "duplicate_metadata"
            ]
        ),
        archive_fuzzy_duplicates=(
            dedup_stats[
                "duplicate_fuzzy"
            ]
        ),
        destination_duplicate_count=len(
            destination_duplicate_reports
        ),
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
