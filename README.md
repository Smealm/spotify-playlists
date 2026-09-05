# Spotify Playlist Full Archive

Automatically reconstruct and maintain a **complete historical archive of a Spotify playlist** using data from [Spotify Playlist Archive](https://spotifyplaylistarchive.com/) and the Spotify Web API.

The goal is simple:

> **Take a playlist's entire recorded history and turn it into a permanent, always-updating Spotify playlist.**

Spotify Playlist Archive already keeps track of songs that have been added to and removed from playlists. This project takes that historical data, parses it, and mirrors the complete history back into Spotify.

---

# ⚠️ Heads Up: Archive Song Count Disparity

A cumulative archive may contain **more songs** than are currently present in the actual Spotify playlist. This is expected.

The cumulative archive counts **every occurrence** of a song, including when it is removed and later re-added. The actual Spotify playlist only contains **one copy of each song**.

The final archive may also contain fewer tracks if some songs are no longer available on Spotify or in your region.

---

# Full Archives

## Electronic

### [Creamy](https://open.spotify.com/playlist/0d9fkV0DPxsq5Ag7IP8obL)
<sub>
Spotify’s “Creamy” playlist, preserved with every song it has featured since 2021, plus every song it adds in the future.<br>
Powered by spotifyplaylistarchive.com and my own custom automation script.
</sub>

### [Dubstep Don]()
### [Creamy](https://open.spotify.com/playlist/6jFdULRcOzcsNJoHLU136C)
<sub>
Spotify’s “Dubstep Don” playlist, preserved with every song it has featured since 2021, plus every song it adds in the future.<br>
Powered by spotifyplaylistarchive.com and my own custom automation script.
</sub>

---

# Inspiration / Motivation

This project is about creating **more complete Spotify playlists**.

Each playlist in this repo contains the **full official Spotify playlist history** of whatever playlists are being archived, rather than being limited to the songs currently in rotation.

It started with me wanting a **full Creamy playlist discography** to shuffle through instead of being limited to the current 100-song curation. I wanted the historical curation.


---

# How It Works

Using **Creamy** as an example, the process works in a few stages.

```text
Spotify Playlist Archive
          │
          ▼
   Cumulative Archive
          │
          ▼
    Parse Track URLs
    + Added Dates
          │
          ▼
   Fetch Spotify Metadata
          │
          ▼
     Duplicate Check
          │
          ▼
     Add Missing Songs
          │
          ▼
     Reorder Playlist
          │
          ▼
 Permanent Full Archive
```

## 1. Get the complete playlist history

The first step is retrieving the **Cumulative** archive for the playlist from [Spotify Playlist Archive](https://github.com/mackorone/spotify-playlist-archive-2).

For Creamy, the source is:

https://github.com/mackorone/spotify-playlist-archive-2/blob/main/playlists/cumulative/37i9dQZF1DXdgz8ZB7c2CP.md

The cumulative archive contains every track that has been recorded as appearing in the playlist, including tracks that have since been removed.

This is important because the current Spotify playlist only tells us what is there **right now**. The cumulative archive lets us reconstruct what has been there **historically**.

---

## 2. Parse the archive

The archive is a Markdown file containing information about each track.

The script parses the raw Markdown and extracts:

* Spotify track URLs
* Spotify track IDs
* The date each track was added

Everything else is ignored.

The result is essentially a chronological list:

```text
Track A → 2024-01-03
Track B → 2024-01-08
Track C → 2024-01-15
Track D → 2024-02-01
...
```

The tracks are then sorted by their original **Added** date.

---

## 3. Fetch Spotify metadata

The Spotify track URLs are used to retrieve metadata through the **Spotify Web API**.

Metadata is used for more than simply adding songs.

It also allows the script to identify duplicate songs that may have different Spotify track IDs.

This matters because the cumulative archive can contain situations like:

```text
Song added
    ↓
Song removed
    ↓
Same song added again
```

The archive records both appearances, meaning the same song can occur multiple times in the historical data.

Without duplicate detection, those entries would become duplicate songs in the permanent archive.

---

# Duplicate Detection

Before anything is added to the destination playlist, the script checks whether the song is already represented there.

Duplicate detection uses a combination of:

### Exact Spotify ID

If the exact Spotify track already exists in the destination playlist, it is skipped immediately.

```text
spotify:track:ABC123
        ↓
Already exists
        ↓
SKIP
```

### Metadata-based matching

Spotify can have multiple track IDs representing the same song across different releases, remasters, albums, or regional versions.

Because of this, the script also performs **Dedup-style fuzzy matching** using track metadata.

The matching process considers:

* Track title
* Artist(s)
* Track duration

For example:

```text
Track A
"Example Song"
Artist: Example Artist
Duration: 3:42

Track B
"Example Song - Remastered"
Artist: Example Artist
Duration: 3:43
```

Even though these may have different Spotify IDs, they can be recognized as the same underlying song.

The matching thresholds can be configured in the script.

---

# Adding Songs

Once duplicate detection is complete, the script has a list containing only songs that genuinely need to be added.

Those songs are added to Spotify using the Spotify Web API.

Spotify allows multiple tracks to be added in a single request, so the script batches additions rather than making one API request per song.

New tracks are initially appended to the playlist.

They are not placed in their final position yet.

That happens in the next step.

---

# Why This Exists

Spotify playlists are inherently temporary.

A playlist can change every day:

```text
Today
├── Song A
├── Song B
└── Song C

Tomorrow
├── Song B
├── Song C
└── Song D
```

Once Song A is removed, Spotify's current playlist no longer provides an easy way to see that Song A was ever there.

Spotify Playlist Archive solves the historical-data problem by recording playlist changes over time.

This project takes the next step:

> **Turn that historical record into a permanent Spotify playlist.**

Instead of manually saving songs as they appear, the archive can be reconstructed automatically.

---

# Relationship to Spotify Playlist Archive

This project **does not maintain its own playlist change history**.

That responsibility belongs to the project it relies on:

[Spotify Playlist Archive](https://spotifyplaylistarchive.com/)

If you want to see the historical record for a specific archived playlist — including when tracks were added and removed — use:

https://spotifyplaylistarchive.com/

This repository simply consumes that existing historical data, parses it, and uses it to maintain a permanent Spotify playlist.

In other words:

```text
Spotify Playlist Archive
        │
        │  Historical data
        ▼
This project
        │
        │  Parse + deduplicate
        │  + synchronize
        ▼
Permanent Spotify Playlist
```

The upstream project remains the **source of truth for playlist history**.

This project is essentially a **mirror and reconstruction layer**.

---

# Limitations

### Upstream data is required

If Spotify Playlist Archive does not have a historical record for a playlist, this project cannot reconstruct its history.

### Spotify track availability

A historical Spotify track may no longer be available.

Tracks can become unavailable because of:

* Licensing changes
* Regional availability
* Removed releases
* Deleted Spotify tracks
* Label/catalog changes

Those tracks may not be addable even though they exist in the historical archive.

### Fuzzy matching is not perfect

Metadata-based duplicate detection is intentionally conservative.

There is no universal way to determine whether two Spotify tracks are "the same song" in every situation.

Remixes, live versions, edits, remasters, covers, and alternate recordings can make this ambiguous.

The matching thresholds can therefore be adjusted depending on how aggressive the archive should be.

---

# Project Philosophy

The project is intentionally simple:

**Don't lose the music.**

If a playlist is continuously updated, its history should not have to disappear every time a song is removed.

The upstream archive records the history.

This project turns that history into something you can actually keep listening to.

---

## Credits

Historical playlist data is provided by:

**[Spotify Playlist Archive](https://spotifyplaylistarchive.com/)**

Archive repository:

**[mackorone/spotify-playlist-archive-2](https://github.com/mackorone/spotify-playlist-archive-2)**

Duplicate-matching inspiration:

**[JMPerez/spotify-dedup](https://github.com/JMPerez/spotify-dedup)**

Spotify integration:

**Spotify Web API**

---

# License

See `LICENSE` for details.
