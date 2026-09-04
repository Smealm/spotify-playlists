import os
import re
import time

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

    # Match ONLY Spotify track URLs.
    #
    # Example:
    #
    # https://open.spotify.com/track/6JeDB7vnShGrxmpBT3thpY
    #
    # Result:
    #
    # 6JeDB7vnShGrxmpBT3thpY

    pattern = re.compile(
        r"https://open\.spotify\.com/track/"
        r"([A-Za-z0-9]+)"
    )

    track_ids = pattern.findall(markdown)

    # Remove duplicate track IDs while preserving
    # the order in which they appear in the archive.
    track_ids = list(dict.fromkeys(track_ids))

    print(
        f"Found {len(track_ids)} unique tracks "
        f"in the archive."
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
    # 1. Get every track ever seen in Creamy
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
    # 4. Find tracks we haven't added yet
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
    # 5. Append them
    # --------------------------------------------------------

    add_tracks(
        new_tracks,
        access_token,
    )

    print()
    print("Done.")


if __name__ == "__main__":
    main()