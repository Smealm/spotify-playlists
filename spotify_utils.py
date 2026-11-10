#!/usr/bin/env python3
"""Shared Spotify helpers for the playlist scripts.

This module holds everything both scripts need so behaviour stays
identical: credentials, a small Web API client with retries, the
duplicate rule used by JMPerez/spotify-dedup, and the chunked reorder
planner (Spotify only allows 100 items per mutation request).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import requests

ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"

# Spotify accepts at most 100 items per playlist mutation request.
BATCH_SIZE = 100

# spotify-dedup treats two entries as the same song when the name and the
# primary artist match (case-insensitively) and their durations differ by
# less than this many milliseconds.
DUPLICATE_DURATION_TOLERANCE_MS = 2_000

PROJECT_DIR = Path(__file__).resolve().parent


class SpotifyError(RuntimeError):
    """Raised when credentials are missing or Spotify rejects a request."""


@dataclass(frozen=True)
class Track:
    """The only track fields these scripts care about."""

    id: str
    name: str
    artists: tuple[str, ...]
    duration_ms: int

    @property
    def duplicate_key(self) -> str:
        """Key used by spotify-dedup: name plus the first artist, lowercased."""
        artist = self.artists[0].strip().lower() if self.artists else ""
        return f"{self.name.strip().lower()}:{artist}"

    def describe(self) -> str:
        artists = ", ".join(self.artists) or "Unknown artist"
        return f"{self.name} - {artists}"


@dataclass(frozen=True)
class Duplicate:
    """A playlist entry that repeats an earlier one."""

    position: int
    reason: str
    track: Track


def track_from_spotify(raw: dict | None) -> Track | None:
    """Convert a Spotify track object, or return None if it is unusable."""
    if not raw or not raw.get("id"):
        return None

    track_type = raw.get("type")

    if track_type and track_type != "track":
        return None

    artists = tuple(
        str(artist.get("name") or "")
        for artist in (raw.get("artists") or [])
        if artist.get("name")
    )

    return Track(
        id=raw["id"],
        name=str(raw.get("name") or ""),
        artists=artists,
        duration_ms=int(raw.get("duration_ms") or 0),
    )


def playlist_entry_track(entry: dict) -> dict | None:
    """Return the track object of a playlist entry.

    Spotify renamed the payload key from ``track`` to ``item``; accept both
    so the scripts keep working across API versions.
    """
    raw = entry.get("item")

    if raw is None:
        raw = entry.get("track")

    return raw


class DuplicateTracker:
    """Incremental version of spotify-dedup's ``findDuplicatedTracks``.

    A track is a duplicate when its Spotify ID was already seen, or when a
    kept track has the same name and primary artist and a duration within
    ``DUPLICATE_DURATION_TOLERANCE_MS``.
    """

    def __init__(self) -> None:
        self._seen_ids: set[str] = set()
        self._seen_durations: dict[str, list[int]] = {}

    def check(self, track: Track) -> str | None:
        if track.id in self._seen_ids:
            return "same-id"

        for duration in self._seen_durations.get(track.duplicate_key, ()):
            if abs(duration - track.duration_ms) < DUPLICATE_DURATION_TOLERANCE_MS:
                return "same-name-artist"

        return None

    def add(self, track: Track) -> None:
        self._seen_ids.add(track.id)
        self._seen_durations.setdefault(track.duplicate_key, []).append(track.duration_ms)

    def keep(self, track: Track) -> bool:
        """Return True when the track is new, remembering it if it is."""
        if self.check(track):
            return False

        self.add(track)
        return True


def find_duplicates(tracks: Sequence[Track | None]) -> list[Duplicate]:
    """Return every entry that duplicates an earlier entry, in order."""
    tracker = DuplicateTracker()
    duplicates: list[Duplicate] = []

    for position, track in enumerate(tracks):
        if track is None:
            continue

        reason = tracker.check(track)

        if reason:
            duplicates.append(Duplicate(position, reason, track))
        else:
            tracker.add(track)

    return duplicates


class Logger:
    """Mirror output to stdout and to a log file inside the repository."""

    def __init__(self, path: str | None) -> None:
        self.path = resolve_log_path(path)
        self._lines: list[str] = []

        # Track titles are not always encodable in a Windows console code
        # page; never let logging crash a sync.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")

    def write(self, message: str = "") -> None:
        print(message)
        self._lines.append(message)

    def save(self) -> None:
        if self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self._lines) + "\n", encoding="utf-8")
        print(f"\nLog written to {self.path}")


def resolve_log_path(value: str | None) -> Path | None:
    """Resolve --log against the repository root and keep it inside it."""
    if value is None or not str(value).strip():
        return None

    candidate = str(value).strip().lstrip("/\\")
    path = (PROJECT_DIR / candidate).resolve()

    try:
        path.relative_to(PROJECT_DIR)
    except ValueError as error:
        raise ValueError("--log must stay inside the project directory") from error

    return path


def get_access_token() -> str:
    """Exchange the long-lived refresh token for a fresh access token."""
    client_id, client_secret, refresh_token = (
        _required_env("SPOTIFY_CLIENT_ID"),
        _required_env("SPOTIFY_CLIENT_SECRET"),
        _required_env("SPOTIFY_REFRESH_TOKEN"),
    )

    response = requests.post(
        ACCOUNTS_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        timeout=30,
    )

    if not response.ok:
        raise SpotifyError(
            f"Spotify authentication failed ({response.status_code}): "
            f"{response.text.strip()}"
        )

    access_token = response.json().get("access_token")

    if not access_token:
        raise SpotifyError("Spotify authentication returned no access token.")

    return access_token


def _required_env(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise SpotifyError(f"Missing required environment variable: {name}")

    return value


class Spotify:
    """Minimal Spotify Web API client with rate-limit handling."""

    def __init__(self, access_token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        attempts: int = 6,
    ):
        url = path if path.startswith("http") else f"{API_URL}{path}"

        for attempt in range(1, attempts + 1):
            response = self._session.request(
                method,
                url,
                params=params,
                json=json,
                timeout=60,
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == attempts:
                    break

                delay = _retry_delay(response, attempt)
                print(
                    f"Spotify returned {response.status_code}; "
                    f"retrying in {delay:.0f}s..."
                )
                time.sleep(delay)
                continue

            if not response.ok:
                raise SpotifyError(
                    f"{method} {url} failed ({response.status_code}): "
                    f"{response.text.strip()[:500]}"
                )

            if response.status_code == 204 or not response.content:
                return None

            return response.json()

        raise SpotifyError(f"{method} {url} failed after {attempts} attempts.")

    def playlist(self, playlist_id: str) -> dict:
        data = self.request(
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": "name,owner(display_name),snapshot_id"},
        )

        return data or {}

    def snapshot_id(self, playlist_id: str) -> str | None:
        return self.playlist(playlist_id).get("snapshot_id")

    def playlist_items(self, playlist_id: str, *, label: str) -> list[dict]:
        """Return every playlist item, following pagination to the end.

        No ``fields`` filter is used on purpose: Spotify renames response
        keys from time to time, and a stale filter silently returns empty
        objects.
        """
        items: list[dict] = []
        data = self.request(
            "GET",
            f"/playlists/{playlist_id}/items",
            params={"limit": 100, "offset": 0},
        )

        while data is not None:
            page = data.get("items") or []
            items.extend(page)
            total = data.get("total", "?")
            print(f"  {label}: read {len(items)}/{total} item(s)")

            next_url = data.get("next")

            if not next_url:
                break

            data = self.request("GET", next_url)

        return items

    def add_items(
        self,
        playlist_id: str,
        uris: Sequence[str],
        *,
        position: int | None = None,
        snapshot_id: str | None = None,
    ) -> str | None:
        payload: dict = {"uris": list(uris)}

        if position is not None:
            payload["position"] = position

        if snapshot_id:
            payload["snapshot_id"] = snapshot_id

        data = self.request("POST", f"/playlists/{playlist_id}/items", json=payload)

        return (data or {}).get("snapshot_id", snapshot_id)

    def remove_positions(
        self,
        playlist_id: str,
        removals: Sequence[tuple[str, Sequence[int]]],
        *,
        snapshot_id: str | None = None,
    ) -> str | None:
        # An empty positions list means "remove every occurrence of this URI",
        # which is all the published schema guarantees.
        items = []

        for uri, positions in removals:
            item: dict = {"uri": uri}

            if positions:
                item["positions"] = list(positions)

            items.append(item)

        payload: dict = {"items": items}

        if snapshot_id:
            payload["snapshot_id"] = snapshot_id

        data = self.request("DELETE", f"/playlists/{playlist_id}/items", json=payload)

        return (data or {}).get("snapshot_id", snapshot_id)

    def reorder(
        self,
        playlist_id: str,
        *,
        range_start: int,
        insert_before: int,
        range_length: int,
        snapshot_id: str | None = None,
    ) -> str | None:
        payload: dict = {
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": range_length,
        }

        if snapshot_id:
            payload["snapshot_id"] = snapshot_id

        data = self.request("PUT", f"/playlists/{playlist_id}/items", json=payload)

        return (data or {}).get("snapshot_id", snapshot_id)


def _retry_delay(response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass

    return float(min(2**attempt, 30))


def remove_playlist_positions(
    spotify: Spotify,
    playlist_id: str,
    uri_by_position: dict[int, str],
    positions: Sequence[int],
    *,
    snapshot_id: str | None,
    logger: Logger,
    dry_run: bool = False,
) -> str | None:
    """Remove specific playlist positions, newest position first."""
    positions = sorted({position for position in positions}, reverse=True)

    if not positions:
        return snapshot_id

    logger.write(f"Removing {len(positions)} playlist item(s)...")

    if dry_run:
        logger.write("Dry run: nothing was removed.")
        return snapshot_id

    # Spotify needs one entry per URI with all of its positions.
    grouped: dict[str, list[int]] = {}

    for position in positions:
        uri = uri_by_position.get(position)

        if uri is None:
            logger.write(f"  Skipping position {position}: no track URI.")
            continue

        grouped.setdefault(uri, []).append(position)

    # One request may carry at most BATCH_SIZE positions in total.
    batches: list[dict[str, list[int]]] = []
    current: dict[str, list[int]] = {}
    size = 0

    for uri, uri_positions in grouped.items():
        for position in uri_positions:
            if size == BATCH_SIZE:
                batches.append(current)
                current, size = {}, 0

            current.setdefault(uri, []).append(position)
            size += 1

    if current:
        batches.append(current)

    for number, batch in enumerate(batches, start=1):
        removals = list(batch.items())

        try:
            snapshot_id = spotify.remove_positions(
                playlist_id,
                removals,
                snapshot_id=snapshot_id,
            )
        except SpotifyError as error:
            # Position-specific removal is not part of the documented schema.
            # Fall back to removing whole URIs; callers reconcile afterwards.
            logger.write(
                f"  position-specific removal rejected ({error}); "
                "removing whole URIs instead."
            )
            snapshot_id = spotify.remove_positions(
                playlist_id,
                [(uri, ()) for uri, _ in removals],
                snapshot_id=snapshot_id,
            )

        removed = sum(len(batch_positions) for batch_positions in batch.values())
        logger.write(f"  removal {number}/{len(batches)}: {removed} item(s)")

    return snapshot_id


def plan_moves(
    current: Sequence[str],
    desired: Sequence[str],
    *,
    batch_size: int = BATCH_SIZE,
) -> list[dict]:
    """Plan the fewest greedy block moves that turn current into desired.

    Tokens only need to be comparable; positions are what Spotify sees.
    The plan is verified by simulation before it is returned.
    """
    current = list(current)
    desired = list(desired)

    if len(current) != len(desired):
        raise ValueError(
            "Reorder planning requires matching lengths: "
            f"{len(current)} current vs {len(desired)} desired."
        )

    if sorted(current) != sorted(desired):
        raise ValueError("Reorder planning requires the same items on both sides.")

    moves: list[dict] = []
    position = 0

    while position < len(desired):
        if current[position] == desired[position]:
            position += 1
            continue

        try:
            source = current.index(desired[position], position + 1)
        except ValueError as error:
            raise RuntimeError(
                f"Planner could not find {desired[position]!r} after position {position}."
            ) from error

        # Extend the move while the following items already match.
        length = 1

        while (
            length < batch_size
            and source + length < len(current)
            and position + length < len(desired)
            and current[source + length] == desired[position + length]
        ):
            length += 1

        block = current[source : source + length]
        del current[source : source + length]
        current[position:position] = block

        moves.append(
            {
                "range_start": source,
                "insert_before": position,
                "range_length": length,
            }
        )

        position += length

    if current != desired:
        raise RuntimeError("Reorder planning failed to reproduce the desired order.")

    return moves


def apply_moves(
    spotify: Spotify,
    playlist_id: str,
    moves: Sequence[dict],
    *,
    snapshot_id: str | None,
    logger: Logger,
    dry_run: bool = False,
) -> tuple[str | None, float]:
    """Execute a plan produced by :func:`plan_moves`."""
    if not moves:
        logger.write("Playlist is already in the desired order.")
        return snapshot_id, 0.0

    logger.write(f"Applying {len(moves)} reorder operation(s)...")

    if dry_run:
        logger.write("Dry run: reorder operations skipped.")
        return snapshot_id, 0.0

    started = time.perf_counter()

    for number, move in enumerate(moves, start=1):
        snapshot_id = spotify.reorder(
            playlist_id,
            range_start=move["range_start"],
            insert_before=move["insert_before"],
            range_length=move["range_length"],
            snapshot_id=snapshot_id,
        )

        logger.write(
            f"  move {number}/{len(moves)}: "
            f"{move['range_length']} item(s) from "
            f"{move['range_start']} to {move['insert_before']}"
        )

    return snapshot_id, time.perf_counter() - started
