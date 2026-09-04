import os
import re
import time
from datetime import datetime

import requests


# ============================================================
# CONFIG
# ============================================================

ARCHIVE_URL = (
    "https://raw.githubusercontent.com/"
    "mackorone/spotify-playlist-archive-2/"
    "refs/heads/main/playlists/cumulative/"
    "37i9dQZF1DXdgz8ZB7c2CP.md"
)

DEST_PLAYLIST_ID = "0d9fkV0DPxsq5Ag7IP8obL"

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
                f"Rate limited. Waiting {retry_after} seconds..."
            )

            time.sleep(retry_after)
            continue

        response.raise_for_status()

        return response


# ============================================================
# READ CREAMY ARCHIVE
# ============================================================

def get_archive_tracks():
    print("Downloading Creamy cumulative archive...")

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
            for column in line.strip("|").split("|")
        ]

        # Expected:
        #
        # 0 = Title
        # 1 = Artist(s)
        # 2 = Album
        # 3 = Length
        # 4 = Added
        # 5 = Removed

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

    # Oldest → newest
    track_rows.sort(
        key=lambda row: row["added"]
    )

    # Remove duplicate Spotify IDs.
    seen = set()
    track_ids = []

    for row in track_rows:
        track_id = row["track_id"]

        if track_id in seen:
            continue

        seen.add(track_id)
        track_ids.append(track_id)

    print(
        f"Found {len(track_ids)} unique Creamy tracks "
        f"in chronological order."
    )

    return track_ids


# ============================================================
# READ PLAYLIST
# ============================================================

def get_playlist_snapshot(access_token):
    """
    Get the current playlist snapshot ID.

    Spotify requires the snapshot ID when reordering.
    """

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
    Return the playlist's current items in their exact order.

    Example:

        [
            "track_id_A",
            "track_id_B",
            "track_id_C",
        ]
    """

    print("Reading destination playlist...")

    track_ids = []

    url = (
        f"https://api.spotify.com/v1/"
        f"playlists/{DEST_PLAYLIST_ID}/items"
    )

    params = {
        "limit": 50,
        "fields": "items(item(type,id)),next",
    }

    while url:
        response = spotify_request(
            "GET",
            url,
            access_token,
            params=params,
        )

        data = response.json()

        for item in data.get("items", []):
            track = item.get("item")

            if not track:
                continue

            if track.get("type") != "track":
                continue

            track_id = track.get("id")

            if track_id:
                track_ids.append(track_id)

        url = data.get("next")

        # The next URL already contains its query parameters.
        params = None

    print(
        f"Found {len(track_ids)} tracks "
        f"in destination playlist."
    )

    return track_ids


# ============================================================
# ADD MISSING TRACKS
# ============================================================

def add_missing_tracks(
    archive_tracks,
    existing_tracks,
    access_token,
):
    existing_set = set(existing_tracks)

    missing_tracks = [
        track_id
        for track_id in archive_tracks
        if track_id not in existing_set
    ]

    if not missing_tracks:
        print("No new Creamy tracks to add.")
        return

    print(
        f"Found {len(missing_tracks)} new Creamy tracks."
    )

    total = len(missing_tracks)

    # Spotify allows up to 100 items per request.
    #
    # These are temporarily appended. We will immediately
    # reorder the entire playlist afterward.
    for start in range(0, total, 100):
        batch = missing_tracks[start:start + 100]

        uris = [
            f"spotify:track:{track_id}"
            for track_id in batch
        ]

        print(
            f"Adding {start + 1}-{start + len(batch)} "
            f"of {total} new tracks..."
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

    print(
        f"Added {total} new Creamy tracks."
    )


# ============================================================
# COMPUTE DESIRED ORDER
# ============================================================

def compute_desired_order(
    archive_tracks,
    current_tracks,
):
    """
    Build the final desired playlist order.

    All Creamy tracks appear first, in chronological order.

    Any tracks that already exist in the destination playlist
    but aren't present in the Creamy archive are preserved and
    placed after the Creamy tracks.

    This means the script NEVER deletes an existing track.
    """

    archive_set = set(archive_tracks)

    # Creamy tracks in historical order.
    desired = list(archive_tracks)

    # Preserve anything else already in the playlist.
    #
    # We don't know a historical Creamy date for these, so
    # leave them after the Creamy archive in their current
    # relative order.
    extras = [
        track_id
        for track_id in current_tracks
        if track_id not in archive_set
    ]

    desired.extend(extras)

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

    We do NOT replace or delete the playlist contents.

    Algorithm:

        For each desired position:
            - If the correct track is already there, do nothing.
            - Otherwise find the desired track later in the
              current playlist.
            - Move that track to the desired position.

    This produces the requested ordering while preserving
    the actual playlist items.
    """

    if current_tracks == desired_tracks:
        print("Playlist is already in the correct order.")
        return

    print()
    print("Calculating playlist reorder...")
    print(
        f"Current items: {len(current_tracks)}"
    )
    print(
        f"Desired items: {len(desired_tracks)}"
    )

    # Work on a local representation of the playlist.
    current = list(current_tracks)

    snapshot_id = get_playlist_snapshot(
        access_token
    )

    moves = 0

    for target_position in range(len(desired_tracks)):
        desired_track = desired_tracks[target_position]

        # Already correct.
        if current[target_position] == desired_track:
            continue

        # Find the desired track later in the playlist.
        try:
            current_position = current.index(
                desired_track,
                target_position + 1,
            )

        except ValueError:
            # This should not happen because desired_tracks
            # was constructed from the current playlist plus
            # the archive.
            print(
                f"WARNING: Could not find "
                f"{desired_track} in playlist."
            )
            continue

        print(
            f"Move #{moves + 1}: "
            f"position {current_position} "
            f"→ {target_position}"
        )

        # Spotify's API uses:
        #
        # range_start   = current position
        # range_length  = number of items to move
        # insert_before = destination position
        #
        # We move exactly one item.
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

        # Spotify returns the new snapshot ID.
        snapshot_id = response.json()["snapshot_id"]

        # Update our local representation so subsequent
        # calculations are based on the playlist's new order.
        track = current.pop(current_position)

        current.insert(
            target_position,
            track,
        )

        moves += 1

    print()
    print(
        f"Playlist reordered using {moves} move(s)."
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Creamy → Permanent Playlist")
    print("=" * 60)
    print()

    # --------------------------------------------------------
    # 1. Get every track ever seen in Creamy.
    #    Sorted oldest → newest.
    # --------------------------------------------------------

    archive_tracks = get_archive_tracks()

    # --------------------------------------------------------
    # 2. Authenticate.
    # --------------------------------------------------------

    print("Refreshing Spotify access token...")

    access_token = get_access_token()

    # --------------------------------------------------------
    # 3. Read the current playlist.
    # --------------------------------------------------------

    existing_tracks = get_playlist_items(
        access_token
    )

    # --------------------------------------------------------
    # 4. Add anything missing.
    #
    # New tracks are temporarily appended. They will be
    # reordered into their historical Creamy position below.
    # --------------------------------------------------------

    add_missing_tracks(
        archive_tracks,
        existing_tracks,
        access_token,
    )

    # --------------------------------------------------------
    # 5. Re-read the playlist.
    #
    # This is important because the playlist has potentially
    # changed after adding tracks.
    # --------------------------------------------------------

    current_tracks = get_playlist_items(
        access_token
    )

    # --------------------------------------------------------
    # 6. Compute exactly what the playlist should look like.
    # --------------------------------------------------------

    desired_tracks = compute_desired_order(
        archive_tracks,
        current_tracks,
    )

    print()
    print(
        f"Archive:        {len(archive_tracks)} tracks"
    )
    print(
        f"Playlist:       {len(current_tracks)} tracks"
    )
    print(
        f"Desired order:  {len(desired_tracks)} tracks"
    )

    # --------------------------------------------------------
    # 7. Reorder using Spotify's move API.
    #
    # Nothing is removed or replaced.
    # --------------------------------------------------------

    reorder_playlist(
        current_tracks,
        desired_tracks,
        access_token,
    )

    print()
    print("Done.")


if __name__ == "__main__":
    main()
