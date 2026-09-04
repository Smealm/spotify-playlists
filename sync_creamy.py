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

        # Spotify rate limiting
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
# READ CUMULATIVE ARCHIVE
# ============================================================

def get_archive_tracks():
    print("Downloading Creamy cumulative archive...")

    response = requests.get(
        ARCHIVE_URL,
        timeout=30,
    )

    response.raise_for_status()

    markdown = response.text

    # --------------------------------------------------------
    # The cumulative archive is a Markdown table:
    #
    # | Title | Artist(s) | Album | Length | Added | Removed |
    #
    # We extract:
    #
    #   Spotify track ID
    #   Added date
    #
    # Then sort by Added date so the tracks are returned in
    # the same chronological order in which they were added
    # to Creamy.
    # --------------------------------------------------------

    track_rows = []

    track_pattern = re.compile(
        r"https://open\.spotify\.com/track/"
        r"([A-Za-z0-9]+)"
    )

    for line in markdown.splitlines():
        line = line.strip()

        # Only process Markdown table rows.
        if not line.startswith("|"):
            continue

        # Extract the Spotify track ID.
        match = track_pattern.search(line)

        if not match:
            continue

        track_id = match.group(1)

        # Split the Markdown table row into columns.
        columns = [
            column.strip()
            for column in line.strip("|").split("|")
        ]

        # We expect:
        #
        # 0 = Title
        # 1 = Artist(s)
        # 2 = Album
        # 3 = Length
        # 4 = Added
        # 5 = Removed
        #
        if len(columns) < 5:
            continue

        added_string = columns[4]

        # Ignore the table header/separator.
        if added_string.lower() == "added":
            continue

        if set(added_string) <= {"-", ":"}:
            continue

        # Try to parse the Added date.
        #
        # The archive uses dates such as:
        #
        # 2021-12-21
        #
        # If the date can't be parsed, put the track at the
        # end rather than crashing the entire sync.
        try:
            added_date = datetime.strptime(
                added_string,
                "%Y-%m-%d",
            )
        except ValueError:
            print(
                f"Warning: couldn't parse Added date "
                f"'{added_string}' for track {track_id}"
            )

            added_date = datetime.max

        track_rows.append(
            {
                "track_id": track_id,
                "added": added_date,
            }
        )

    # --------------------------------------------------------
    # Sort chronologically:
    #
    # oldest Added date → newest Added date
    # --------------------------------------------------------

    track_rows.sort(
        key=lambda row: row["added"]
    )

    # --------------------------------------------------------
    # Remove duplicate track IDs while preserving the
    # chronological order.
    # --------------------------------------------------------

    seen = set()
    track_ids = []

    for row in track_rows:
        track_id = row["track_id"]

        if track_id in seen:
            continue

        seen.add(track_id)
        track_ids.append(track_id)

    print(
        f"Found {len(track_ids)} unique tracks "
        f"in chronological order."
    )

    if track_ids:
        print(
            f"First added: {track_rows[0]['added'].date()}"
        )

        valid_dates = [
            row["added"]
            for row in track_rows
            if row["added"] != datetime.max
        ]

        if valid_dates:
            print(
                f"Last added:  {max(valid_dates).date()}"
            )

    return track_ids


# ============================================================
# READ DESTINATION PLAYLIST
# ============================================================

def get_existing_tracks(access_token):
    print("Reading your Creamy archive playlist...")

    track_ids = set()

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
                track_ids.add(track_id)

        # Spotify gives us the next URL.
        url = data.get("next")

        # `next` already contains its own query parameters.
        params = None

    print(
        f"Found {len(track_ids)} existing tracks "
        f"in your playlist."
    )

    return track_ids


# ============================================================
# ADD TRACKS
# ============================================================

def add_tracks(track_ids, access_token):
    if not track_ids:
        print("Nothing new to add.")
        return

    total = len(track_ids)

    # Spotify permits a maximum of 100 items per request.
    for start in range(0, total, 100):
        batch = track_ids[start:start + 100]

        uris = [
            f"spotify:track:{track_id}"
            for track_id in batch
        ]

        print(
            f"Adding {start + 1}-{start + len(batch)} "
            f"of {total}..."
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

    print(f"Added {total} tracks.")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Creamy → Permanent Playlist")
    print("=" * 60)
    print()

    # --------------------------------------------------------
    # 1. Get every track ever seen in Creamy,
    #    sorted by original Added date.
    # --------------------------------------------------------

    archive_tracks = get_archive_tracks()

    # --------------------------------------------------------
    # 2. Authenticate
    # --------------------------------------------------------

    print("Refreshing Spotify access token...")

    access_token = get_access_token()

    # --------------------------------------------------------
    # 3. Get tracks already in your playlist
    # --------------------------------------------------------

    existing_tracks = get_existing_tracks(
        access_token
    )

    # --------------------------------------------------------
    # 4. Find tracks we haven't added yet.
    #
    # Because archive_tracks is chronological, new tracks
    # will also be added chronologically.
    # --------------------------------------------------------

    new_tracks = [
        track_id
        for track_id in archive_tracks
        if track_id not in existing_tracks
    ]

    print()
    print(f"Archive:        {len(archive_tracks)} tracks")
    print(f"Already added:  {len(existing_tracks)} tracks")
    print(f"New:            {len(new_tracks)} tracks")
    print()

    # --------------------------------------------------------
    # 5. Append them in original Creamy order.
    # --------------------------------------------------------

    add_tracks(
        new_tracks,
        access_token,
    )

    print()
    print("Done.")


if __name__ == "__main__":
    main()
