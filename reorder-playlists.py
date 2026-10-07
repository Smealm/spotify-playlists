#!/usr/bin/env python3

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import requests


SPOTIFY_ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_URL = "https://api.spotify.com/v1"


# ============================================================
# LOGGING
# ============================================================

class Logger:
    def __init__(self, path):
        self.path = path

        if self.path:
            directory = os.path.dirname(self.path)

            if directory:
                os.makedirs(directory, exist_ok=True)

    def write(self, message=""):
        print(message)

        if self.path:
            with open(self.path, "a", encoding="utf-8") as file:
                file.write(message + "\n")


# ============================================================
# SPOTIFY AUTH
# ============================================================

def get_access_token():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")

    missing = []

    if not client_id:
        missing.append("SPOTIFY_CLIENT_ID")

    if not client_secret:
        missing.append("SPOTIFY_CLIENT_SECRET")

    if not refresh_token:
        missing.append("SPOTIFY_REFRESH_TOKEN")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
        )

    response = requests.post(
        SPOTIFY_ACCOUNTS_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(client_id, client_secret),
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"Spotify authentication failed "
            f"({response.status_code}): {response.text}"
        )

    data = response.json()

    access_token = data.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Spotify authentication succeeded but "
            "no access token was returned."
        )

    return access_token


# ============================================================
# SPOTIFY API CLIENT
# ============================================================

class Spotify:
    def __init__(self, access_token):
        self.session = requests.Session()

        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def request(self, method, endpoint, **kwargs):
        url = f"{SPOTIFY_API_URL}{endpoint}"

        response = self.session.request(
            method,
            url,
            timeout=30,
            **kwargs,
        )

        # Handle Spotify rate limiting.
        if response.status_code == 429:
            retry_after = int(
                response.headers.get("Retry-After", "5")
            )

            print(
                f"Spotify rate limit reached. "
                f"Waiting {retry_after} seconds..."
            )

            time.sleep(retry_after)

            response = self.session.request(
                method,
                url,
                timeout=30,
                **kwargs,
            )

        if not response.ok:
            raise RuntimeError(
                f"Spotify API request failed "
                f"({response.status_code}): {response.text}"
            )

        if response.status_code == 204:
            return None

        return response.json()

    def get_playlist(self, playlist_id):
        return self.request(
            "GET",
            f"/playlists/{playlist_id}",
            params={
                "fields": (
                    "name,"
                    "owner(display_name),"
                    "snapshot_id,"
                    "items.total"
                )
            },
        )

    def get_playlist_items(self, playlist_id):
        """
        Fetch every item in the playlist.

        Spotify returns playlist items in pages, so this keeps
        requesting pages until the entire playlist is loaded.
        """

        all_items = []
        offset = 0

        while True:
            data = self.request(
                "GET",
                f"/playlists/{playlist_id}/items",
                params={
                    "limit": 100,
                    "offset": offset,
                    "fields": (
                        "items("
                        "added_at,"
                        "added_by,"
                        "track"
                        "),"
                        "next,"
                        "total,"
                        "snapshot_id"
                    ),
                },
            )

            items = data.get("items", [])

            all_items.extend(items)

            total = data.get("total", "?")

            print(
                f"Fetched {len(all_items)}/{total} items..."
            )

            if not data.get("next"):
                break

            offset += len(items)

        return all_items

    def reorder_item(
        self,
        playlist_id,
        range_start,
        insert_before,
        snapshot_id=None,
    ):
        """
        Move one playlist item to another position.
        """

        payload = {
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": 1,
        }

        if snapshot_id:
            payload["snapshot_id"] = snapshot_id

        return self.request(
            "PUT",
            f"/playlists/{playlist_id}/items",
            json=payload,
        )


# ============================================================
# PLAYLIST HELPERS
# ============================================================

def parse_added_at(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def sort_newest_first(items):
    """
    Sort newest additions first.

    Spotify's added_at timestamp represents when the playlist
    item was added to the playlist.

    Items without a timestamp are placed at the bottom.
    """

    def sort_key(item):
        timestamp = parse_added_at(
            item.get("added_at")
        )

        if timestamp is None:
            return datetime.min.replace(
                tzinfo=timezone.utc
            )

        return timestamp

    return sorted(
        items,
        key=sort_key,
        reverse=True,
    )


def get_track_name(item):
    track = item.get("track")

    if not track:
        return "Unknown track"

    return track.get("name") or "Unknown track"


def get_artist_names(item):
    track = item.get("track")

    if not track:
        return ""

    artists = track.get("artists") or []

    return ", ".join(
        artist.get("name", "Unknown artist")
        for artist in artists
    )


# ============================================================
# REORDER
# ============================================================

def reorder_playlist(
    spotify,
    playlist_id,
    items,
    logger,
):
    """
    Reorder the playlist in place.

    Duplicate tracks are supported because playlist entries are
    tracked as individual objects rather than by track URI.
    """

    desired_order = sort_newest_first(items)

    # Local representation of the current Spotify order.
    working_order = list(items)

    changes = 0

    for desired_position, desired_item in enumerate(
        desired_order
    ):
        current_position = None

        # Find the exact playlist entry.
        for index, current_item in enumerate(
            working_order
        ):
            if current_item is desired_item:
                current_position = index
                break

        if current_position is None:
            raise RuntimeError(
                "Could not find playlist item while reordering."
            )

        # Already in the correct position.
        if current_position == desired_position:
            continue

        name = get_track_name(desired_item)
        artists = get_artist_names(desired_item)
        added_at = desired_item.get("added_at")

        logger.write(
            f"Move {current_position + 1:>4} -> "
            f"{desired_position + 1:>4} | "
            f"{name} - {artists} | "
            f"added_at={added_at}"
        )

        spotify.reorder_item(
            playlist_id=playlist_id,
            range_start=current_position,
            insert_before=desired_position,
        )

        # Keep our local order synchronized with Spotify.
        moved_item = working_order.pop(
            current_position
        )

        working_order.insert(
            desired_position,
            moved_item,
        )

        changes += 1

    return changes


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Reorder a Spotify playlist so the newest "
            "additions are at the top."
        )
    )

    parser.add_argument(
        "--destination-playlist-id",
        required=True,
        help="Spotify playlist ID to reorder.",
    )

    parser.add_argument(
        "--log",
        default=None,
        help="Optional log file path.",
    )

    args = parser.parse_args()

    logger = Logger(args.log)

    logger.write("=" * 70)
    logger.write("Spotify Playlist Reorder")
    logger.write("Newest Added -> Oldest Added")
    logger.write(
        f"Started: {datetime.now(timezone.utc).isoformat()}"
    )
    logger.write("=" * 70)

    try:
        # --------------------------------------------------------
        # Authenticate
        # --------------------------------------------------------

        logger.write("Authenticating with Spotify...")

        access_token = get_access_token()

        spotify = Spotify(access_token)

        logger.write("Authentication successful.")
        logger.write("")

        # --------------------------------------------------------
        # Get playlist metadata
        # --------------------------------------------------------

        playlist_id = args.destination_playlist_id

        playlist = spotify.get_playlist(
            playlist_id
        )

        playlist_name = playlist.get(
            "name",
            "Unknown playlist",
        )

        owner = (
            playlist.get("owner", {})
            .get("display_name")
            or "Unknown"
        )

        logger.write(
            f"Playlist: {playlist_name}"
        )

        logger.write(
            f"Owner: {owner}"
        )

        logger.write(
            f"Playlist ID: {playlist_id}"
        )

        logger.write("")

        # --------------------------------------------------------
        # Fetch playlist
        # --------------------------------------------------------

        logger.write(
            "Fetching playlist items..."
        )

        items = spotify.get_playlist_items(
            playlist_id
        )

        logger.write("")

        if not items:
            logger.write(
                "Playlist is empty. Nothing to reorder."
            )

            return 0

        logger.write(
            f"Found {len(items)} playlist items."
        )

        # --------------------------------------------------------
        # Determine whether reorder is necessary
        # --------------------------------------------------------

        desired_order = sort_newest_first(items)

        already_sorted = all(
            current is desired
            for current, desired
            in zip(items, desired_order)
        )

        if already_sorted:
            logger.write("")
            logger.write(
                "Playlist is already sorted "
                "newest -> oldest."
            )
            logger.write(
                "No Spotify changes were necessary."
            )

            return 0

        # --------------------------------------------------------
        # Show desired order
        # --------------------------------------------------------

        logger.write("")
        logger.write(
            "Desired order (first 10):"
        )
        logger.write("-" * 70)

        for position, item in enumerate(
            desired_order[:10],
            start=1,
        ):
            logger.write(
                f"{position:>3}. "
                f"{get_track_name(item)}"
                f" - "
                f"{get_artist_names(item)}"
            )

            logger.write(
                f"     added_at="
                f"{item.get('added_at')}"
            )

        if len(desired_order) > 10:
            logger.write(
                f"... and "
                f"{len(desired_order) - 10} more"
            )

        logger.write("-" * 70)
        logger.write("")

        # --------------------------------------------------------
        # Reorder
        # --------------------------------------------------------

        logger.write(
            "Reordering playlist..."
        )

        changes = reorder_playlist(
            spotify=spotify,
            playlist_id=playlist_id,
            items=items,
            logger=logger,
        )

        # --------------------------------------------------------
        # Finish
        # --------------------------------------------------------

        logger.write("")
        logger.write("=" * 70)
        logger.write(
            f"Finished successfully. "
            f"{changes} item(s) moved."
        )
        logger.write(
            "Playlist is now newest added -> oldest added."
        )
        logger.write(
            f"Finished: "
            f"{datetime.now(timezone.utc).isoformat()}"
        )
        logger.write("=" * 70)

        return 0

    except Exception as error:
        logger.write("")
        logger.write("=" * 70)
        logger.write("REORDER FAILED")
        logger.write("=" * 70)
        logger.write(str(error))

        return 1


if __name__ == "__main__":
    sys.exit(main())
