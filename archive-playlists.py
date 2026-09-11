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
        raise ValueError("--log must stay inside the project")

    return path


class Logger:
    def __init__(self, path):
        self.path = resolve_log_path(path)
        self.lines = []

    def add(self, text=""):
        self.lines.append(str(text))

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["access_token"]


def spotify_request(method, url, token, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"

    for attempt in range(5):
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=30,
            **kwargs,
        )

        if response.status_code == 429:
            retry_after = int(
                response.headers.get("Retry-After", "5")
            )

            print(
                f"Rate limited. Waiting {retry_after}s..."
            )

            time.sleep(retry_after)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("Spotify rate limit persisted after 5 retries")


# ============================================================
# TEXT / MATCHING
# ============================================================

def normalize_text(value):
    if not value:
        return ""

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()
    value = value.replace("&", " and ")

    value = re.sub(
        r"\b(feat\.?|ft\.?|featuring)\b",
        " ",
        value,
    )

    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def artists_text(track):
    return " ".join(
        normalize_text(a.get("name", ""))
        for a in track.get("artists", [])
        if a.get("name")
    ).strip()


def similarity(a, b):
    return SequenceMatcher(
        None,
        normalize_text(a),
        normalize_text(b),
    ).ratio()


def duplicate(candidate, existing):
    if not candidate or not existing:
        return False

    # Exact Spotify ID is always a duplicate.
    if (
        candidate.get("track_id")
        and candidate.get("track_id") == existing.get("id")
    ):
        return True

    title_score = similarity(
        candidate.get("title", ""),
        existing.get("name", ""),
    )

    if title_score < TITLE_THRESHOLD:
        return False

    artist_score = SequenceMatcher(
        None,
        artists_text_from_candidate(candidate),
        artists_text(existing),
    ).ratio()

    if artist_score < ARTIST_THRESHOLD:
        return False

    candidate_seconds = candidate.get("duration", 0)
    existing_seconds = existing.get("duration_ms", 0) / 1000

    return (
        abs(candidate_seconds - existing_seconds)
        <= DURATION_TOLERANCE
    )


def artists_text_from_candidate(track):
    return normalize_text(
        track.get("artist", "")
    )


# ============================================================
# ARCHIVE
# ============================================================

TRACK_URL_RE = re.compile(
    r"https://open\.spotify\.com/track/([A-Za-z0-9]+)"
)


def parse_duration(value):
    match = re.match(
        r"^\s*(\d+):(\d{2})\s*$",
        value,
    )

    if not match:
        return 0

    minutes = int(match.group(1))
    seconds = int(match.group(2))

    return minutes * 60 + seconds


def read_archive():
    url = ARCHIVE_URL.format(
        playlist_id=ARCHIVE_PLAYLIST_ID
    )

    print(f"Downloading archive:\n{url}")

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    tracks = []

    for line in response.text.splitlines():
        if not line.startswith("|"):
            continue

        match = TRACK_URL_RE.search(line)

        if not match:
            continue

        columns = [
            c.strip()
            for c in line.strip("|").split("|")
        ]

        # Expected:
        # Title | Artist(s) | Album | Length | Added | Removed
        if len(columns) < 5:
            continue

        title = columns[0]
        artist = columns[1]
        duration = columns[3]
        added = columns[4]

        if not re.match(r"^\d{4}-\d{2}-\d{2}", added):
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
                "duration": parse_duration(duration),
                "added": added_date,
            }
        )

    # Oldest -> newest.
    tracks.sort(
        key=lambda x: x["added"]
    )

    # Remove duplicate Spotify IDs while preserving order.
    seen = set()
    result = []

    for track in tracks:
        if track["track_id"] in seen:
            continue

        seen.add(track["track_id"])
        result.append(track)

    print(
        f"Archive contains {len(result)} unique tracks."
    )

    return result


# ============================================================
# DESTINATION PLAYLIST
# ============================================================

def get_playlist_items(token):
    url = f"{API}/playlists/{DESTINATION_PLAYLIST_ID}/items"

    params = {
        "limit": 50,
        "fields": (
            "items(item(id,type,uri,name,artists,duration_ms)),next"
        ),
    }

    tracks = []

    while url:
        response = spotify_request(
            "GET",
            url,
            token,
            params=params,
        )

        data = response.json()

        for item in data.get("items", []):
            track = item.get("item")

            if not track:
                continue

            if track.get("type") != "track":
                continue

            if not track.get("id"):
                continue

            tracks.append(track)

        url = data.get("next")
        params = None

    print(
        f"Destination playlist contains {len(tracks)} tracks."
    )

    return tracks


# ============================================================
# FIND DUPLICATES / NEW TRACKS
# ============================================================

def find_duplicate(candidate, existing_tracks):
    # Fast exact-ID check.
    candidate_id = candidate["track_id"]

    for track in existing_tracks:
        if track.get("id") == candidate_id:
            return track, "exact"

    # Fuzzy check.
    for track in existing_tracks:
        if duplicate(candidate, track):
            return track, "fuzzy"

    return None, None


def determine_new_tracks(archive, existing):
    new_tracks = []

    exact = 0
    fuzzy = 0

    for candidate in archive:
        match, match_type = find_duplicate(
            candidate,
            existing,
        )

        if match:
            if match_type == "exact":
                exact += 1
            else:
                fuzzy += 1

            continue

        new_tracks.append(candidate)

        # Prevent duplicates inside the same archive run.
        existing.append(
            {
                "id": candidate["track_id"],
                "name": candidate["title"],
                "artists": [
                    {"name": candidate["artist"]}
                ],
                "duration_ms": (
                    candidate["duration"] * 1000
                ),
            }
        )

    print()
    print(f"Exact duplicates: {exact}")
    print(f"Fuzzy duplicates: {fuzzy}")
    print(f"New tracks:       {len(new_tracks)}")

    return new_tracks, exact, fuzzy


# ============================================================
# ADD TRACKS
# ============================================================

def add_tracks(tracks, token):
    if not tracks:
        return 0

    if DRY_RUN:
        print(
            f"DRY RUN: would add {len(tracks)} tracks."
        )
        return len(tracks)

    added = 0

    for start in range(0, len(tracks), 100):
        batch = tracks[start:start + 100]

        uris = [
            f"spotify:track:{track['track_id']}"
            for track in batch
        ]

        spotify_request(
            "POST",
            f"{API}/playlists/"
            f"{DESTINATION_PLAYLIST_ID}/items",
            token,
            json={
                "uris": uris,
            },
        )

        added += len(batch)

        print(
            f"Added {added}/{len(tracks)} tracks."
        )

    return added


# ============================================================
# DESIRED ORDER
# ============================================================

def build_desired_order(archive, current):
    """
    Archive tracks: newest -> oldest.

    Tracks that are not part of the archive are kept after
    the archive tracks in their current relative order.
    """

    desired = []
    used = set()

    # Newest -> oldest.
    for candidate in reversed(archive):
        match, _ = find_duplicate(
            candidate,
            current,
        )

        if not match:
            continue

        track_id = match["id"]

        if track_id in used:
            continue

        desired.append(track_id)
        used.add(track_id)

    # Preserve everything else.
    for track in current:
        track_id = track["id"]

        if track_id in used:
            continue

        desired.append(track_id)
        used.add(track_id)

    return desired


# ============================================================
# REORDER
# ============================================================

def reorder(current, desired, token):
    current_ids = [track["id"] for track in current]

    if current_ids == desired:
        print("Playlist is already correctly ordered.")
        return 0, 0.0

    if len(current_ids) != len(desired):
        raise RuntimeError(
            "Playlist length changed unexpectedly."
        )

    if set(current_ids) != set(desired):
        raise RuntimeError(
            "Current and desired playlist contents differ."
        )

    if DRY_RUN:
        print("DRY RUN: playlist would be reordered.")
        return 0, 0.0

    # Get current snapshot.
    response = spotify_request(
        "GET",
        f"{API}/playlists/{DESTINATION_PLAYLIST_ID}",
        token,
        params={
            "fields": "snapshot_id",
        },
    )

    snapshot = response.json()["snapshot_id"]

    working = list(current_ids)
    moves = 0
    started = time.perf_counter()

    for target, wanted in enumerate(desired):
        if working[target] == wanted:
            continue

        source = working.index(
            wanted,
            target + 1,
        )

        response = spotify_request(
            "PUT",
            f"{API}/playlists/"
            f"{DESTINATION_PLAYLIST_ID}/items",
            token,
            json={
                "range_start": source,
                "insert_before": target,
                "range_length": 1,
                "snapshot_id": snapshot,
            },
        )

        snapshot = response.json()["snapshot_id"]

        item = working.pop(source)
        working.insert(target, item)

        moves += 1

    duration = time.perf_counter() - started

    print(
        f"Reordered playlist using "
        f"{moves} move(s) in {duration:.2f}s."
    )

    return moves, duration


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
    moves,
    duration,
):
    LOGGER.add("=" * 60)
    LOGGER.add("Spotify Playlist Archive Sync")
    LOGGER.add("Newest -> Oldest")
    LOGGER.add("")
    LOGGER.add(f"Archive tracks:       {archive_count}")
    LOGGER.add(f"Playlist tracks:      {playlist_count}")
    LOGGER.add(f"Exact duplicates:     {exact}")
    LOGGER.add(f"Fuzzy duplicates:     {fuzzy}")
    LOGGER.add(f"New tracks found:     {new_count}")
    LOGGER.add(f"Tracks added:         {added}")
    LOGGER.add(f"Reorder moves:        {moves}")
    LOGGER.add(
        f"Reorder duration:     {duration:.2f} seconds"
    )
    LOGGER.write()


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Spotify Playlist Archive Sync")
    print("Newest -> Oldest")
    print("=" * 60)

    archive = read_archive()

    if not archive:
        raise RuntimeError(
            "Archive returned zero tracks. "
            "Refusing to modify playlist."
        )

    print("Refreshing Spotify token...")
    token = get_access_token()

    print("Reading destination playlist...")
    current = get_playlist_items(token)

    # Work on a copy because determine_new_tracks adds
    # newly accepted tracks to this list for same-run dedup.
    comparison_tracks = list(current)

    new_tracks, exact, fuzzy = determine_new_tracks(
        archive,
        comparison_tracks,
    )

    added = add_tracks(
        new_tracks,
        token,
    )

    # Always re-read after additions.
    current = get_playlist_items(token)

    desired = build_desired_order(
        archive,
        current,
    )

    moves, duration = reorder(
        current,
        desired,
        token,
    )

    write_summary(
        archive_count=len(archive),
        playlist_count=len(current),
        exact=exact,
        fuzzy=fuzzy,
        new_count=len(new_tracks),
        added=added,
        moves=moves,
        duration=duration,
    )

    print("Done.")


if __name__ == "__main__":
    main()
