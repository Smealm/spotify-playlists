import argparse
import os
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

import requests


# ============================================================
# CONFIG
# ============================================================

# ------------------------------------------------------------
# Dedup settings
# ------------------------------------------------------------

# If True, nothing will be added or reordered.
#
# VERY strongly recommended for the first run.
DRY_RUN = False


# Duration tolerance used by fuzzy matching.
#
# Example:
#   5 means tracks may differ by up to 5 seconds.
DURATION_TOLERANCE_SECONDS = 5


# Minimum title similarity.
TITLE_SIMILARITY_THRESHOLD = 0.92


# Artist matching threshold.
ARTIST_SIMILARITY_THRESHOLD = 0.92


# Spotify API batch size.
# Spotify supports up to 50 track IDs for the tracks endpoint.
TRACK_METADATA_BATCH_SIZE = 50


# ============================================================
# COMMAND-LINE ARGUMENTS
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Mirror a Spotify Playlist Archive cumulative "
            "playlist into a permanent Spotify playlist."
        )
    )

    parser.add_argument(
        "--archive-url",
        required=True,
        help=(
            "Raw GitHub URL of the Spotify Playlist Archive "
            "cumulative Markdown file."
        ),
    )

    parser.add_argument(
        "--destination-playlist-id",
        required=True,
        help="Spotify playlist ID to push the archive into.",
    )

    return parser.parse_args()


ARGS = parse_arguments()

ARCHIVE_URL = ARGS.archive_url
DEST_PLAYLIST_ID = ARGS.destination_playlist_id


# ============================================================
# SPOTIFY CREDENTIALS
# ============================================================

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]


# ============================================================
# SPOTIFY AUTH
# ============================================================

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


# ============================================================
# SPOTIFY API
# ============================================================

def spotify_request(method, url, access_token, **kwargs):
    headers = kwargs.pop("headers", {})

    headers["Authorization"] = f"Bearer {access_token}"

    while True:
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
                f"Rate limited. Waiting "
                f"{retry_after} seconds..."
            )

            time.sleep(retry_after)
            continue

        response.raise_for_status()

        return response


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(value):
    """
    Normalize text for fuzzy duplicate matching.
    """

    if not value:
        return ""

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )

    value = value.lower()

    # Normalize ampersands.
    value = value.replace("&", " and ")

    # Remove common featuring notation.
    value = re.sub(
        r"\b(feat\.?|ft\.?|featuring)\b",
        " ",
        value,
    )

    # Remove punctuation.
    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    # Collapse whitespace.
    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def normalize_artist_names(artists):
    normalized = [
        normalize_text(artist)
        for artist in artists
        if artist
    ]

    return [
        artist
        for artist in normalized
        if artist
    ]


def artist_string(track):
    artists = track.get("artists", [])

    names = [
        artist.get("name", "")
        for artist in artists
        if artist.get("name")
    ]

    return " ".join(
        normalize_artist_names(names)
    )


# ============================================================
# TRACK ID HELPERS
# ============================================================

def normalize_track_id(value):
    """
    Convert a Spotify track ID, URI, or URL into a raw ID.
    """

    if not value:
        return None

    value = value.strip()

    # spotify:track:ABC123
    if value.startswith("spotify:track:"):
        return value.split(":")[-1]

    # https://open.spotify.com/track/ABC123
    match = re.search(
        r"open\.spotify\.com/track/([A-Za-z0-9]+)",
        value,
    )

    if match:
        return match.group(1)

    # Raw Spotify ID.
    if re.fullmatch(
        r"[A-Za-z0-9]+",
        value,
    ):
        return value

    return None


# ============================================================
# TRACK SIMILARITY
# ============================================================

def title_similarity(a, b):
    return SequenceMatcher(
        None,
        normalize_text(a),
        normalize_text(b),
    ).ratio()


def artist_similarity(a, b):
    return SequenceMatcher(
        None,
        artist_string(a),
        artist_string(b),
    ).ratio()


def is_fuzzy_duplicate(candidate, existing):
    """
    Determine whether two Spotify tracks represent the
    same song using metadata matching.

    Conditions:

        1. title similarity
        2. artist similarity
        3. duration within tolerance
    """

    candidate_duration = (
        candidate.get("duration_ms", 0)
        / 1000
    )

    existing_duration = (
        existing.get("duration_ms", 0)
        / 1000
    )

    duration_difference = abs(
        candidate_duration
        - existing_duration
    )

    if (
        duration_difference
        > DURATION_TOLERANCE_SECONDS
    ):
        return False

    title_score = title_similarity(
        candidate.get("name", ""),
        existing.get("name", ""),
    )

    if title_score < TITLE_SIMILARITY_THRESHOLD:
        return False

    artist_score = artist_similarity(
        candidate,
        existing,
    )

    if artist_score < ARTIST_SIMILARITY_THRESHOLD:
        return False

    return True


# ============================================================
# READ CUMULATIVE ARCHIVE
# ============================================================

def get_archive_tracks():
    print(
        "Downloading cumulative playlist archive..."
    )

    response = requests.get(
        ARCHIVE_URL,
        timeout=30,
    )

    response.raise_for_status()

    markdown = response.text

    track_pattern = re.compile(
        r"https://open\.spotify\.com/track/"
        r"([A-Za-z0-9]+)"
    )

    track_rows = []

    for line in markdown.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue

        match = track_pattern.search(line)

        if not match:
            continue

        track_id = match.group(1)

        columns = [
            column.strip()
            for column
            in line.strip("|").split("|")
        ]

        if len(columns) < 5:
            continue

        added_string = columns[4]

        if added_string.lower() == "added":
            continue

        if set(added_string) <= {"-", ":"}:
            continue

        try:
            added_date = datetime.strptime(
                added_string,
                "%Y-%m-%d",
            )

        except ValueError:
            print(
                f"Warning: couldn't parse Added date "
                f"'{added_string}' for {track_id}"
            )

            added_date = datetime.max

        track_rows.append(
            {
                "track_id": track_id,
                "added": added_date,
            }
        )

    # Oldest → newest.
    track_rows.sort(
        key=lambda row: row["added"]
    )

    # Remove duplicate Spotify IDs while preserving
    # chronological order.
    seen = set()
    track_ids = []

    for row in track_rows:
        track_id = row["track_id"]

        if track_id in seen:
            continue

        seen.add(track_id)
        track_ids.append(track_id)

    print(
        f"Found {len(track_ids)} unique archive tracks "
        f"in chronological order."
    )

    return track_ids


# ============================================================
# READ PLAYLIST
# ============================================================

def get_playlist_snapshot(access_token):
    response = spotify_request(
        "GET",
        f"https://api.spotify.com/v1/"
        f"playlists/{DEST_PLAYLIST_ID}",
        access_token,
        params={
            "fields": "snapshot_id",
        },
    )

    return response.json()["snapshot_id"]


def get_playlist_items(access_token):
    """
    Read every playlist item in exact order.

    Returns Spotify track IDs.

    Non-track playlist items are ignored.
    """

    print(
        "Reading destination playlist..."
    )

    track_ids = []

    url = (
        f"https://api.spotify.com/v1/"
        f"playlists/{DEST_PLAYLIST_ID}/items"
    )

    params = {
        "limit": 50,
        "fields": "items(item(type,id,uri)),next",
    }

    while url:
        response = spotify_request(
            "GET",
            url,
            access_token,
            params=params,
        )

        data = response.json()

        for item in data.get(
            "items",
            [],
        ):
            track = item.get("item")

            if not track:
                continue

            if track.get("type") != "track":
                continue

            track_id = normalize_track_id(
                track.get("uri")
            )

            if not track_id:
                track_id = normalize_track_id(
                    track.get("id")
                )

            if track_id:
                track_ids.append(track_id)

        url = data.get("next")

        # next already contains query parameters.
        params = None

    print(
        f"Found {len(track_ids)} tracks "
        f"in destination playlist."
    )

    return track_ids


# ============================================================
# GET SPOTIFY TRACK METADATA
# ============================================================

def get_track_metadata(
    track_ids,
    access_token,
):
    """
    Fetch Spotify metadata in batches.

    Returns:

        {
            "spotify_id": track_object
        }
    """

    track_ids = [
        normalize_track_id(track_id)
        for track_id in track_ids
    ]

    track_ids = [
        track_id
        for track_id in track_ids
        if track_id
    ]

    # Preserve order while removing duplicates.
    unique_ids = list(
        dict.fromkeys(track_ids)
    )

    tracks = {}

    total = len(unique_ids)

    if not total:
        return tracks

    print(
        f"Fetching Spotify metadata for "
        f"{total} tracks..."
    )

    for start in range(
        0,
        total,
        TRACK_METADATA_BATCH_SIZE,
    ):
        batch = unique_ids[
            start:start + TRACK_METADATA_BATCH_SIZE
        ]

        response = spotify_request(
            "GET",
            "https://api.spotify.com/v1/tracks",
            access_token,
            params={
                "ids": ",".join(batch),
            },
        )

        data = response.json()

        for track in data.get(
            "tracks",
            [],
        ):
            if not track:
                continue

            track_id = track.get("id")

            if track_id:
                tracks[track_id] = track

        print(
            f"Metadata: "
            f"{min(start + len(batch), total)}"
            f"/{total}"
        )

    return tracks


# ============================================================
# DUPLICATE INDEX
# ============================================================

class DuplicateIndex:
    """
    In-memory index of tracks already represented in the
    destination playlist.

    Matching:

        1. Exact Spotify ID
        2. Fuzzy metadata
    """

    def __init__(self):
        self.tracks = {}
        self.ids = set()

    def add(self, track):
        track_id = track.get("id")

        if not track_id:
            return

        self.tracks[track_id] = track
        self.ids.add(track_id)

    def contains_exact(self, track_id):
        return track_id in self.ids

    def find_fuzzy_duplicate(self, candidate):
        for existing in self.tracks.values():
            if is_fuzzy_duplicate(
                candidate,
                existing,
            ):
                return existing

        return None

    def find_duplicate(self, candidate):
        """
        Exact match first, fuzzy match second.

        Returns:

            (existing_track, match_type)

        or:

            (None, None)
        """

        candidate_id = candidate.get("id")

        # Exact Spotify ID.
        if self.contains_exact(candidate_id):
            return (
                self.tracks[candidate_id],
                "exact",
            )

        # Fuzzy metadata match.
        duplicate = self.find_fuzzy_duplicate(
            candidate
        )

        if duplicate:
            return (
                duplicate,
                "fuzzy",
            )

        return (
            None,
            None,
        )


# ============================================================
# DETERMINE TRACKS TO ADD
# ============================================================

def determine_tracks_to_add(
    archive_tracks,
    archive_metadata,
    existing_metadata,
):
    """
    Compare every archive track against the destination
    playlist.

    A track is skipped if:

        - exact Spotify ID already exists, OR
        - a fuzzy duplicate already exists.

    The duplicate index is updated immediately whenever a
    track is accepted.
    """

    duplicate_index = DuplicateIndex()

    # Existing playlist is the initial source of truth.
    for track in existing_metadata.values():
        duplicate_index.add(track)

    tracks_to_add = []

    exact_duplicates = 0
    fuzzy_duplicates = 0
    missing_metadata = 0

    print()
    print(
        "Running Dedup-style duplicate detection..."
    )
    print()

    for position, track_id in enumerate(
        archive_tracks,
        start=1,
    ):
        candidate = archive_metadata.get(
            track_id
        )

        if not candidate:
            print(
                f"WARNING: Spotify metadata unavailable "
                f"for {track_id}. Skipping."
            )

            missing_metadata += 1
            continue

        duplicate, match_type = (
            duplicate_index.find_duplicate(
                candidate
            )
        )

        if duplicate:
            if match_type == "exact":
                exact_duplicates += 1

                print(
                    f"[{position}/{len(archive_tracks)}] "
                    f"EXACT duplicate: "
                    f"{candidate.get('name')} "
                    f"— "
                    f"{artist_string(candidate)}"
                )

            else:
                fuzzy_duplicates += 1

                print(
                    f"[{position}/{len(archive_tracks)}] "
                    f"FUZZY duplicate: "
                    f"{candidate.get('name')} "
                    f"— "
                    f"{artist_string(candidate)} "
                    f"≈ "
                    f"{duplicate.get('name')} "
                    f"— "
                    f"{artist_string(duplicate)}"
                )

            continue

        # This is genuinely new.
        #
        # Add it immediately so another archive entry cannot
        # add a duplicate later in the same run.
        duplicate_index.add(candidate)

        tracks_to_add.append(candidate)

        print(
            f"[{position}/{len(archive_tracks)}] "
            f"NEW: "
            f"{candidate.get('name')} "
            f"— "
            f"{artist_string(candidate)}"
        )

    print()
    print("Deduplication results:")
    print(
        f"  Exact duplicates:   {exact_duplicates}"
    )
    print(
        f"  Fuzzy duplicates:   {fuzzy_duplicates}"
    )
    print(
        f"  Missing metadata:    {missing_metadata}"
    )
    print(
        f"  Tracks to add:       {len(tracks_to_add)}"
    )

    return tracks_to_add


# ============================================================
# ADD TRACKS
# ============================================================

def add_tracks(
    tracks_to_add,
    access_token,
):
    """
    Add genuinely new tracks to Spotify.

    Tracks are temporarily appended and reordered afterward.
    """

    if not tracks_to_add:
        print()
        print(
            "No new tracks need to be added."
        )
        return

    if DRY_RUN:
        print()
        print("DRY RUN enabled.")
        print(
            f"Would add {len(tracks_to_add)} tracks."
        )
        return

    total = len(tracks_to_add)

    print()
    print(
        f"Adding {total} new tracks..."
    )

    for start in range(
        0,
        total,
        100,
    ):
        batch = tracks_to_add[
            start:start + 100
        ]

        # Final live duplicate check.
        current_ids = get_playlist_items(
            access_token
        )

        current_metadata = get_track_metadata(
            current_ids,
            access_token,
        )

        current_index = DuplicateIndex()

        for track in current_metadata.values():
            current_index.add(track)

        safe_batch = []

        for candidate in batch:
            duplicate, match_type = (
                current_index.find_duplicate(
                    candidate
                )
            )

            if duplicate:
                print(
                    f"FINAL CHECK: skipping "
                    f"{match_type} duplicate: "
                    f"{candidate.get('name')} "
                    f"— "
                    f"{artist_string(candidate)}"
                )

                continue

            safe_batch.append(candidate)

            # Make the next candidate see this one too.
            current_index.add(candidate)

        if not safe_batch:
            print(
                "Nothing new in this batch "
                "after final duplicate check."
            )

            continue

        uris = [
            f"spotify:track:{track['id']}"
            for track in safe_batch
        ]

        print(
            f"Adding {len(safe_batch)} tracks..."
        )

        spotify_request(
            "POST",
            f"https://api.spotify.com/v1/"
            f"playlists/{DEST_PLAYLIST_ID}/items",
            access_token,
            json={
                "uris": uris,
            },
        )

    print()
    print(
        "Finished adding tracks."
    )


# ============================================================
# BUILD FINAL DESIRED ORDER
# ============================================================

def build_desired_order(
    archive_tracks,
    archive_metadata,
    current_tracks,
    current_metadata,
):
    """
    Build the final playlist order.

    Archive tracks are placed:

        NEWEST → OLDEST

    Non-archive tracks are preserved afterward in their
    existing relative order.

    Nothing is deleted.
    """

    current_index = DuplicateIndex()

    for track in current_metadata.values():
        current_index.add(track)

    desired = []
    used_ids = set()

    # --------------------------------------------------------
    # Archive tracks newest → oldest.
    # --------------------------------------------------------

    for archive_id in reversed(archive_tracks):
        archive_track = archive_metadata.get(
            archive_id
        )

        if not archive_track:
            continue

        duplicate, match_type = (
            current_index.find_duplicate(
                archive_track
            )
        )

        if not duplicate:
            print(
                f"WARNING: Could not locate "
                f"archive track in playlist: "
                f"{archive_track.get('name')} "
                f"— "
                f"{artist_string(archive_track)}"
            )

            continue

        actual_id = duplicate.get("id")

        if actual_id in used_ids:
            continue

        desired.append(actual_id)
        used_ids.add(actual_id)

    # --------------------------------------------------------
    # Preserve non-archive tracks after the archive.
    # --------------------------------------------------------

    for track_id in current_tracks:
        if track_id in used_ids:
            continue

        desired.append(track_id)
        used_ids.add(track_id)

    return desired


# ============================================================
# REORDER PLAYLIST
# ============================================================

def reorder_playlist(
    current_tracks,
    desired_tracks,
    access_token,
):
    """
    Reorder the playlist using Spotify's range/insertion API.

    No items are removed or replaced.
    """

    if current_tracks == desired_tracks:
        print()
        print(
            "Playlist is already in the correct order."
        )
        return

    print()
    print(
        "Calculating playlist reorder..."
    )

    print(
        f"Current items: {len(current_tracks)}"
    )

    print(
        f"Desired items: {len(desired_tracks)}"
    )

    if len(current_tracks) != len(desired_tracks):
        raise RuntimeError(
            "Current and desired playlist lengths "
            "do not match. Refusing to reorder."
        )

    if set(current_tracks) != set(desired_tracks):
        raise RuntimeError(
            "Current and desired playlist contents "
            "do not match. Refusing to reorder."
        )

    if DRY_RUN:
        print()
        print(
            "DRY RUN: playlist would be reordered."
        )
        return

    current = list(current_tracks)

    snapshot_id = get_playlist_snapshot(
        access_token
    )

    moves = 0

    for target_position in range(
        len(desired_tracks)
    ):
        desired_track = desired_tracks[
            target_position
        ]

        # Already correct.
        if current[target_position] == desired_track:
            continue

        try:
            current_position = current.index(
                desired_track,
                target_position + 1,
            )

        except ValueError:
            raise RuntimeError(
                f"Could not find track "
                f"{desired_track} while reordering."
            )

        print(
            f"Move #{moves + 1}: "
            f"position {current_position} "
            f"→ {target_position}"
        )

        response = spotify_request(
            "PUT",
            f"https://api.spotify.com/v1/"
            f"playlists/{DEST_PLAYLIST_ID}/items",
            access_token,
            json={
                "range_start": current_position,
                "insert_before": target_position,
                "range_length": 1,
                "snapshot_id": snapshot_id,
            },
        )

        snapshot_id = response.json()[
            "snapshot_id"
        ]

        track = current.pop(
            current_position
        )

        current.insert(
            target_position,
            track,
        )

        moves += 1

    print()
    print(
        f"Playlist reordered using "
        f"{moves} move(s)."
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print(
        "Spotify Playlist Archive → Permanent Playlist"
    )
    print(
        "Dedup-style importer"
    )
    print(
        "Newest → Oldest ordering"
    )
    print("=" * 60)
    print()

    print(
        f"Archive: {ARCHIVE_URL}"
    )

    print(
        f"Destination playlist: {DEST_PLAYLIST_ID}"
    )

    print()

    if DRY_RUN:
        print(
            "!!! DRY RUN MODE ENABLED !!!"
        )
        print(
            "No Spotify playlist changes will be made."
        )
        print()

    # --------------------------------------------------------
    # 1. Read cumulative archive.
    # --------------------------------------------------------

    archive_tracks = get_archive_tracks()

    if not archive_tracks:
        raise RuntimeError(
            "Archive returned no tracks. "
            "Refusing to modify playlist."
        )

    # --------------------------------------------------------
    # 2. Authenticate.
    # --------------------------------------------------------

    print(
        "Refreshing Spotify access token..."
    )

    access_token = get_access_token()

    # --------------------------------------------------------
    # 3. Read destination playlist.
    # --------------------------------------------------------

    existing_tracks = get_playlist_items(
        access_token
    )

    # --------------------------------------------------------
    # 4. Fetch existing playlist metadata.
    # --------------------------------------------------------

    existing_metadata = get_track_metadata(
        existing_tracks,
        access_token,
    )

    # --------------------------------------------------------
    # 5. Fetch archive metadata.
    # --------------------------------------------------------

    archive_metadata = get_track_metadata(
        archive_tracks,
        access_token,
    )

    # --------------------------------------------------------
    # 6. Determine tracks that need to be added.
    # --------------------------------------------------------

    tracks_to_add = determine_tracks_to_add(
        archive_tracks,
        archive_metadata,
        existing_metadata,
    )

    # --------------------------------------------------------
    # 7. Add genuinely new tracks.
    # --------------------------------------------------------

    add_tracks(
        tracks_to_add,
        access_token,
    )

    # --------------------------------------------------------
    # 8. Re-read playlist after additions.
    # --------------------------------------------------------

    current_tracks = get_playlist_items(
        access_token
    )

    # --------------------------------------------------------
    # 9. Fetch fresh metadata.
    # --------------------------------------------------------

    current_metadata = get_track_metadata(
        current_tracks,
        access_token,
    )

    # --------------------------------------------------------
    # 10. Build desired order.
    #
    # NEWEST → OLDEST
    # --------------------------------------------------------

    desired_tracks = build_desired_order(
        archive_tracks,
        archive_metadata,
        current_tracks,
        current_metadata,
    )

    # --------------------------------------------------------
    # 11. Summary.
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(
        f"Archive tracks:       {len(archive_tracks)}"
    )

    print(
        f"Playlist tracks:      {len(current_tracks)}"
    )

    print(
        f"New tracks added:     {len(tracks_to_add)}"
    )

    print(
        f"Desired playlist:     {len(desired_tracks)}"
    )

    print(
        "Ordering:             NEWEST → OLDEST"
    )

    # --------------------------------------------------------
    # 12. Reorder.
    # --------------------------------------------------------

    reorder_playlist(
        current_tracks,
        desired_tracks,
        access_token,
    )

    print()
    print("Done.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()