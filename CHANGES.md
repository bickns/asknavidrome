# Changes from rosskouk/asknavidrome

This fork has diverged substantially from the upstream project. This document
summarises what's different and why, for anyone (including future me)
trying to understand the codebase without re-reading the entire history.

Full credit to Ross Stewart ([rosskouk](https://github.com/rosskouk)) for the
original AskNavidrome project this is built on.

## Architecture

The original single `app.py` (~1900 lines by the time this fork started
modifying it) has been split into focused modules under `skill/asknavidrome/`:

| Module | Responsibility |
|---|---|
| `config.py` | Environment variable loading/validation, logging setup, Navidrome connection, startup-time genre/artist caches, tunable constants |
| `state.py` | Per-device play queues and background-fill process tracking (see Multi-device support, below) |
| `search.py` | Free-text search resolution — fuzzy matching, scoring, the song/artist/genre/album priority chain |
| `handlers.py` | Every Alexa request handler (intents + AudioPlayer directives) and their shared support functions |
| `exception_handlers.py` | The two exception handlers and their support functions |
| `app.py` | Slimmed down to Flask/SkillBuilder setup and handler registration only |

`subsonic_api.py`, `media_queue.py`, `controller.py`, and `track.py` are
functionally close to the original, with fixes noted below.

## New features

- **Free-text search** (`NaviSonicPlaySearch` / `NaviSonicQueueSearch`,
  invoked as "play {query}" / "queue {query}") — resolves a spoken phrase
  against songs, then artists, then genres, then albums, in priority order,
  each step requiring a minimum match-confidence score before being
  accepted. Handles "X by Y" phrasing (with or without filler words like
  "the song"/"the band"), punctuation-normalized retry variants (e.g. "and"
  vs "&"), and local fuzzy spelling correction against the artist list for
  speech-to-text near-misses (e.g. "gipsy kings" → "Gypsy Kings").
- **Play by decade** (`NaviSonicPlayMusicByDecade`, "play music from the
  {decade}", optionally with a genre) — operates at the song level so
  compilations/"best of" collections are handled correctly, not just
  albums whose own release year happens to match.
- **Shuffle an artist's entire catalogue** (`NaviSonicShuffleArtist`,
  "shuffle songs by {artist}") — distinct from the original
  `NaviSonicPlayMusicByArtist`, which caps at `NAVI_SONG_COUNT`.
- **Play similar songs** (`NaviSonicPlaySimilar`, "play {song} and similar
  songs") — seeds Navidrome's own similarity engine from a matched track.
- **Multi-device support** — the original held a single shared play queue;
  two Echo devices talking to the skill would trample each other's
  playback state. Every device now gets its own isolated `MediaQueue`,
  keyed by Alexa's `device_id` and created lazily on first use.
- **Empty-queue auto-recovery** — if the queue runs dry mid-playback
  (`PlaybackNearlyFinished`, `PlaybackFinished`, "next" with nothing
  queued), the skill now automatically refills with similar songs rather
  than erroring out or repeating the same track.
- **Device ID in logging** — every log line now includes the last 8
  characters of the triggering device's ID, so concurrent multi-device
  sessions and errors can be traced back to the right device.

## Bug fixes

- **`search_artist`/`search_album`/`search_song` `KeyError` crash** —
  Navidrome can omit the relevant key from `searchResult3` entirely on
  zero matches rather than returning an empty list; these now use `.get()`
  with defaults throughout instead of bracket indexing.
- **Track ordering within an album** — `build_song_list_from_albums` now
  explicitly sorts by disc/track number (cast to `int`, defensively, in
  case the API ever returns them as strings) rather than trusting
  whatever order the API happened to return.
- **Single-track "album" match crash** — a compilation/album search that
  collapsed to exactly one song previously crashed with an `IndexError`;
  now pads out with similar songs via `get_similar_songs` instead.
- **`ENQUEUE` Play directive rejected by Alexa** — the empty-queue refill
  path was adding tracks without linking the first one's `previous_id` to
  the currently-playing track (which `MediaQueue.add_track()` can't do on
  its own when the queue is empty), causing Alexa to reject the resulting
  directive with "must have an expected token." Fixed by explicitly
  setting `previous_id` on the first refilled track.
- **Exception handlers sending invalid responses to AudioPlayer/System
  requests** — both exception handlers previously always tried to speak a
  response, but AudioPlayer-directive and `System.ExceptionEncountered`
  requests must not receive `outputSpeech`/`reprompt`/`shouldEndSession`
  at all; Alexa was rejecting these and sending back a further exception
  as a result. Both handlers now check the request type first.
- **Decade synonym resolution** — a custom slot's `.value` returns the
  literal words spoken ("90s"), not the canonical resolved value
  ("1990s") the slot type maps it to; `get_resolved_slot_value()` now
  handles this properly for the decade slot.
- **Whole-word genre matching** — raw substring matching on genre names
  produced false positives (e.g. "dance" matching "dancehall"); genre
  matching now splits on whitespace/hyphen/ampersand and compares whole
  tokens.

## Configuration / operational changes

- `asknavidrome.env` is gitignored; `asknavidrome.env.example` provides a
  template with placeholder values. (An earlier commit history briefly
  contained real credentials before this fork's history was rewritten to
  remove them — if you're looking at commit history predating this file,
  treat any credentials found there as already rotated and invalid.)
- `tester.py` is gitignored (local testing scaffolding, not part of the
  skill itself).

## Not carried over from upstream

- `NaviSonicPlaySongByArtist` was removed — its functionality is fully
  subsumed by the free-text search's "X by Y" parsing, and keeping both
  created Alexa NLU intent-matching ambiguity (too many "play"-prefixed
  intents competing for the same phrasing).