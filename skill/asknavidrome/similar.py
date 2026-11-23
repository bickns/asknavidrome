"""
Finding the next similar song, in the background.

Asking Navidrome for similar songs can take several seconds, far too
long to do while Alexa is waiting for a reply. So whenever nothing is
queued behind the current track, a background thread looks for ONE
similar song and parks its ID in state.py (the "waiting song"). It is
not in the queue, so it can never get in the way of anything the user
queues or inserts.

When the queue runs dry (NearlyFinished, Next, PlaybackFailed) the
waiting song is moved into the queue and the next lookup starts. A new
"play" flushes the queue and discards the waiting song (see
helpers.flush_queue()).

Public entry points:

  start_similar_lookup()   start looking, unless already waiting/looking
  ensure_next_track()      make sure the queue has a next track
"""

import threading
import time

import asknavidrome.controller as controller
from asknavidrome import state
from asknavidrome.config import connection, logger
from asknavidrome.search import pick_similar_song

SIMILAR_SEEDS_TOTAL = 3
"""How many songs seed the search for the waiting song: the last song
in the queue (or the current song if the queue is empty), plus up to
this many minus one songs that finished within
state.RECENT_PLAYS_WINDOW_SECONDS."""

SIMILAR_LOOKUP_ATTEMPTS = 5
"""How many times the background lookup tries before giving up."""

SIMILAR_LOOKUP_RETRY_SECONDS = 5
"""Pause between background lookup attempts."""


def choose_similar_song(play_queue, device_id: str):
    """Pick the similar song that should follow the end of the queue.

    Seeds: the last song in the queue (or the current song when the
    queue is empty), plus the most recently finished songs within
    state.RECENT_PLAYS_WINDOW_SECONDS, SIMILAR_SEEDS_TOTAL seeds in all.
    Never chosen: a seed, the current song, anything already queued, or
    anything in the last state.RECENT_PLAYS_MAX finished tracks.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :return: A song ID, or None if nothing suitable was found
    :rtype: str | None
    """
    queued = list(play_queue.get_current_queue())
    current = play_queue.get_current_track()

    if queued:
        anchor = queued[-1].id
    elif current and current.id:
        anchor = current.id
    else:
        logger.info('choose_similar_song: nothing is queued or playing to seed from')
        return None

    recent = state.get_recent_track_ids(
        device_id, limit=SIMILAR_SEEDS_TOTAL, max_age_seconds=state.RECENT_PLAYS_WINDOW_SECONDS)
    seeds = [anchor] + [r for r in recent if r != anchor][:SIMILAR_SEEDS_TOTAL - 1]

    exclude = set(state.get_recent_track_ids(device_id))
    exclude.update(seeds)
    exclude.update(track.id for track in queued)
    if current and current.id:
        exclude.add(current.id)

    logger.info(f'choose_similar_song: {len(seeds)} seeds {seeds}, {len(exclude)} songs excluded')
    return pick_similar_song(seeds, exclude)


def similar_lookup_worker(play_queue, device_id: str, generation: int) -> None:
    """Background thread body: find the waiting similar song.

    Tries up to SIMILAR_LOOKUP_ATTEMPTS times, SIMILAR_LOOKUP_RETRY_SECONDS
    apart. Stops early if the queue is flushed in the meantime (the
    generation changes). Any unexpected error is logged with its
    traceback, and the lookup is always marked finished so a later
    trigger can start another.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :param int generation: The generation state.start_lookup() returned
    :return: None
    """
    try:
        for attempt in range(1, SIMILAR_LOOKUP_ATTEMPTS + 1):
            if state.get_generation(device_id) != generation:
                logger.info('Similar-song lookup: the queue was flushed - abandoning the lookup')
                return

            song_id = choose_similar_song(play_queue, device_id)

            if song_id:
                if state.set_waiting_song(device_id, song_id, generation):
                    logger.info(f'Similar-song lookup: waiting song is {song_id} (attempt {attempt})')
                else:
                    logger.info('Similar-song lookup: the queue was flushed while looking - result discarded')
                return

            logger.info(f'Similar-song lookup: attempt {attempt} of {SIMILAR_LOOKUP_ATTEMPTS} found nothing')
            if attempt < SIMILAR_LOOKUP_ATTEMPTS:
                time.sleep(SIMILAR_LOOKUP_RETRY_SECONDS)

        logger.warning(f'Similar-song lookup: gave up after {SIMILAR_LOOKUP_ATTEMPTS} attempts')

    except Exception:
        logger.exception('Similar-song lookup crashed')

    finally:
        state.finish_lookup(device_id, generation)


def start_similar_lookup(play_queue, device_id: str) -> None:
    """Start looking for the waiting similar song in a background
    thread, unless a song is already waiting or a lookup is already
    running. Safe to call as often as needed.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :return: None
    """
    generation = state.start_lookup(device_id)
    if generation is None:
        logger.debug('Similar-song lookup not needed: a song is already waiting or a lookup is running')
        return

    logger.info('Starting a background lookup for the next similar song')
    try:
        threading.Thread(
            target=similar_lookup_worker,
            args=(play_queue, device_id, generation),
            name='similar-song-lookup',
            daemon=True,
        ).start()
    except Exception:
        state.finish_lookup(device_id, generation)
        raise


def ensure_next_track(play_queue, device_id: str) -> bool:
    """Make sure the queue has something to play next, using the
    waiting similar song if the queue is empty.

    Runs under the device's lock so two requests arriving together
    (for example NextIntent and PlaybackNearlyFinished) cannot both
    add a song. The song is added through play_queue.add_tracks(),
    which runs inside the manager process and sets the previous_id
    that Alexa's ENQUEUE directive requires.

    If the queue is empty and nothing is waiting, a lookup is started
    and False is returned.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :return: True if the queue now has a next track
    :rtype: bool
    """
    with state.get_device_lock(device_id):
        if play_queue.get_queue_count() > 0:
            return True

        song_id = state.take_waiting_song(device_id)

        if not song_id:
            logger.info('Queue is empty and no similar song is waiting yet')
            start_similar_lookup(play_queue, device_id)
            return False

        current = play_queue.get_current_track()
        if current and current.id == song_id:
            logger.info('The waiting similar song is the one already playing - discarding it')
            start_similar_lookup(play_queue, device_id)
            return False

        track = controller.build_track(connection, song_id)
        play_queue.add_tracks([track])
        logger.info(f'Queue was empty - added the waiting similar song: {track.title} by {track.artist} [id={track.id}]')
        return True