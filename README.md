# Spotify Playlist Full Archive

Maintain a **permanent, always-updating archive of a Spotify playlist** using
[Spotify Playlist Archive](https://spotifyplaylistarchive.com/) and the Spotify
Web API.

The archive playlist is built in two layers:

```text
┌──────────────────────────────────────────┐
│ 1. The source playlist's current order   │  exactly how it is on Spotify today
├──────────────────────────────────────────┤
│ 2. Every track from the recorded history │  newest addition first
└──────────────────────────────────────────┘
```

So the archive always opens the way the real playlist looks right now, and the
rest of the history sits underneath it, ready to shuffle.

---

# Managed playlists

## Full archives

| Playlist | Destination | Source |
|---|---|---|
| [Creamy](https://open.spotify.com/playlist/0d9fkV0DPxsq5Ag7IP8obL) | `0d9fkV0DPxsq5Ag7IP8obL` | `37i9dQZF1DXdgz8ZB7c2CP` |
| [Dubstep Don](https://open.spotify.com/playlist/6jFdULRcOzcsNJoHLU136C) | `6jFdULRcOzcsNJoHLU136C` | `37i9dQZF1DX5Q27plkaOQ3` |
| [Brain Food](https://open.spotify.com/playlist/5Hcbss2XBcioA7GEGM0yOf) | `5Hcbss2XBcioA7GEGM0yOf` | `37i9dQZF1DWXLeA8Omikj7` |

## Reordered playlists

| Playlist | Playlist ID |
|---|---|
| What Is This | `5sahCBpr8p5Bzte0QgB4gh` |
| What Is This+ | `1PNK5SF5TClIEUJ5ZrTa6Q` |

These are sorted by the date each track was added, newest first.

---

# Schedule

Every managed playlist is updated **daily at 2 PM Eastern**.

GitHub Actions cron runs in UTC and cannot follow daylight saving, so each
workflow schedules two times:

```text
18:00 UTC  →  2 PM EDT (daylight saving)
19:00 UTC  →  2 PM EST (standard time)
```

A single gate step in the shared workflow checks the actual hour in
`America/New_York` and skips the run that is not 2 PM, so only one sync happens
per day, all year.

Things to know:

* Scheduled GitHub workflows can start a few minutes late when the platform is
  busy. The scripts are idempotent, so a late or repeated run is harmless.
* Use **Run workflow** in the Actions tab to sync immediately; manual runs skip
  the time check.
* Each run overwrites its log in `logs/` and commits it back to the repository.

---

# How it works

```text
Spotify Playlist Archive        Live Spotify playlist
      cumulative                       current order
          │                                 │
          ▼                                 ▼
   Parse markdown                    Read items
   (id, title, artist,               (id, name, artist,
    duration, added)                  duration, order)
          │                                 │
          └──────────────┬──────────────────┘
                         ▼
              Remove duplicates
              (spotify-dedup rule)
                         ▼
                 Exact target order
                         ▼
       Remove extras → add missing → reorder
                         ▼
       Verify the playlist matches the target
```

1. **History** comes from the cumulative markdown file, where each track keeps
   its earliest recorded addition date. When the same Spotify ID was added,
   removed and added again, the newest occurrence wins.
2. **Current order** comes from the live source playlist. If Spotify cannot
   serve it, the archive's `pretty` snapshot is used instead, so the job still
   runs.
3. **Duplicates** are dropped while the target is built, and then the
   destination playlist is reconciled to that target exactly. Any track in the
   archive playlist that is not part of the target is removed.
4. **Changes are minimal.** Tracks that are already correct stay where they
   are; the rest are moved in blocks of up to 100 items, which is Spotify's
   per-request limit.
5. **Verification** re-reads the playlist and compares it to the target. A
   mismatch fails the job instead of leaving a half-tidied playlist.

---

# Duplicate detection

The rule is the one used by
[JMPerez/spotify-dedup](https://github.com/JMPerez/spotify-dedup):

| Reason | Condition |
|---|---|
| `same-id` | The exact Spotify track ID was already kept. |
| `same-name-artist` | The name and **primary** artist match (case-insensitive) and the durations are within **2 seconds**. |

The first occurrence is always the one that is kept. Extra artists are ignored
for the comparison, which is what catches the common case of the same song
appearing under two releases:

```text
"Song"                    "Song"
Artist A, Artist B        Artist A
3:42                      3:42

same-name-artist  →  one copy kept
```

It is deliberately conservative about the parts that usually mean a different
recording:

* A different primary artist is never a duplicate.
* A duration difference of 2 seconds or more is never a duplicate, so distinct
  edits and extended mixes survive.
* Titles are compared as-is (apart from case and surrounding whitespace), so
  `Song` and `Song - Remastered` are only merged when the duration also matches
  within 2 seconds.

A song that is unavailable in your region can be in the history but not
addable. Those tracks are skipped by Spotify and do not fail the sync.

---

# Scripts

Both scripts need `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and
`SPOTIFY_REFRESH_TOKEN`, and both accept `--dry-run` to report what would
change without touching Spotify.

## `archive-playlists.py`

```bash
python archive-playlists.py \
  --archive-playlist-id 37i9dQZF1DXdgz8ZB7c2CP \
  --destination-playlist-id 0d9fkV0DPxsq5Ag7IP8obL \
  --log logs/creamy-output.txt
```

`--archive-playlist-id` is the Spotify ID of the playlist being archived; it is
also the ID used to find the markdown history. `--destination-playlist-id` is
the archive playlist that gets maintained.

## `reorder-playlists.py`

```bash
python reorder-playlists.py \
  --destination-playlist-id 5sahCBpr8p5Bzte0QgB4gh \
  --log logs/what-is-this-output.txt
```

Removes duplicates, then sorts by `added_at`, newest first.

## Shared code

`spotify_utils.py` holds the pieces both scripts use: credentials, a Web API
client with rate-limit and 5xx retries, the duplicate tracker, the log writer
and the reorder planner. Behaviour only needs to be changed in one place.

---

# Limitations

* **Upstream data is required.** If Spotify Playlist Archive has no record of a
  playlist, there is no history to rebuild.
* **Track availability.** Removed, region-locked or deleted tracks cannot be
  added even though the history still lists them.
* **Spotify ID changes.** Tracks that are re-released under a new ID can only be
  merged when name, primary artist and duration still line up.
* **Playlist size.** Spotify itself caps playlists at 10,000 items; the archive
  is limited by the same ceiling.

---

# Credits

* Historical playlist data: [Spotify Playlist Archive](https://spotifyplaylistarchive.com/)
  ([mackorone/spotify-playlist-archive-2](https://github.com/mackorone/spotify-playlist-archive-2))
* Duplicate-matching rule: [JMPerez/spotify-dedup](https://github.com/JMPerez/spotify-dedup)
* Playlist API: [Spotify Web API](https://developer.spotify.com/documentation/web-api)

# License

See `LICENSE` for details.
