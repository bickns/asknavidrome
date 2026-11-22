"""
Shared runtime state for the AskNavidrome web service.

This module owns one MediaQueue instance PER DEVICE, keyed by Alexa's
device_id (present on every request via
handler_input.request_envelope.context.system.device.device_id).
Without this, two Echo devices talking to the same skill would share
one queue and trample each other's playback state - see the
multi-device concurrency discussion earlier in this project's history.

A device's queue is created lazily, the first time it's requested -
there's no separate "register a device" step. Queues for devices that
stop being used are never evicted; for a personal/household-scale
deployment this is an acceptable, deliberate simplification rather
than an oversight - each idle queue costs a handful of empty deques
in the manager subprocess, not meaningfully more.

This module also keeps, per device and in the web service process
(not the manager process):

- a short, bounded list of the tracks the device recently FINISHED
  playing. Recent tracks seed the choice of the next similar song, and
  the same list stops a suggestion repeating anything heard lately.

- ONE "waiting song": the similar song that has been chosen in the
  background and will be added to the queue when the queue runs dry.
  It is only a song ID, never a queue entry, so it can't get in the
  way of anything the user queues or inserts. A "generation" counter
  lets a flush of the queue (a new "play X") discard the waiting song
  and make any lookup still running for the old song throw its result
  away.

Everything here is lost on restart, which is fine: the list covers a
window of minutes and the waiting song is found again within seconds.
"""

from multiprocessing.managers import BaseManager
import threading
import time

import asknavidrome.media_queue as queue

BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()

RECENT_PLAYS_WINDOW_SECONDS = 30 * 60
"""A finished track only counts as a similarity SEED for this long."""

RECENT_PLAYS_MAX = 10
"""Number of finished tracks remembered per device. This is also the
'don't repeat anything from the last 10' rule, and the hard ceiling on
memory used."""

# Confirmed via direct testing that manager.MediaQueue() creates a
# genuinely independent instance on each call, not a shared singleton -
# each entry in this dict is a fully isolated MediaQueue.
_queues_by_device = {}
"""device_id -> MediaQueue instance"""

_background_by_device = {}
"""device_id -> Process instance, or None"""

_recent_by_device = {}
"""device_id -> list of (unix_timestamp, song_id), oldest first"""

_recent_lock = threading.Lock()
"""gunicorn runs several threads in this one process; the recent-plays
lists are mutated and read from different threads, so access is locked."""

_waiting_by_device = {}
"""device_id -> song ID of the similar song waiting to be queued"""

_generation_by_device = {}
"""device_id -> integer that goes up each time the waiting song is
discarded (i.e. whenever the queue is flushed)"""

_lookup_running = {}
"""device_id -> the generation a background lookup is currently
running for. Only a lookup for the CURRENT generation blocks a new one,
so a flush is never held up by a lookup for the old song."""

_waiting_lock = threading.Lock()
"""Guards _waiting_by_device, _generation_by_device and _lookup_running."""

_device_locks = {}
"""device_id -> Lock held while moving the waiting song into the queue"""

_device_locks_guard = threading.Lock()
"""Guards _device_locks."""


def get_play_queue(device_id: str):
    """Get this device's MediaQueue, creating a new one if this is the
    first request seen from it.

    :param str device_id: The Alexa device ID for the current request
    :return: This device's MediaQueue instance
    :rtype: MediaQueue
    """
    if device_id not in _queues_by_device:
        _queues_by_device[device_id] = manager.MediaQueue()
    return _queues_by_device[device_id]


def get_background_process(device_id: str):
    """Get the currently-tracked background Process for this device,
    or None if there isn't one running.

    :param str device_id: The Alexa device ID for the current request
    :return: The Process object, or None
    :rtype: multiprocessing.Process | None
    """
    return _background_by_device.get(device_id)


def set_background_process(device_id: str, process) -> None:
    """Replace the tracked background Process for this device.

    :param str device_id: The Alexa device ID for the current request
    :param process: The new Process object (or None to clear it)
    :return: None
    """
    _background_by_device[device_id] = process


def stop_background_process(device_id: str) -> None:
    """Terminate and join this device's current background Process if
    one is running, then clear it. Safe to call even if none is
    running.

    Every playback-starting handler needs to do this before starting
    a new playlist, to avoid two background threads racing to populate
    the same device's queue at once.

    :param str device_id: The Alexa device ID for the current request
    :return: None
    """
    proc = _background_by_device.get(device_id)
    if proc is not None:
        proc.terminate()
        proc.join()
        _background_by_device[device_id] = None


def record_finished_track(device_id: str, song_id: str) -> None:
    """Record that this device has just finished playing a track.

    The list is trimmed to RECENT_PLAYS_MAX entries, oldest dropped
    first, so it cannot grow without bound and needs no timer.

    :param str device_id: The Alexa device ID for the current request
    :param str song_id: The Navidrome ID of the track that finished
    :return: None
    """
    if not song_id:
        return

    with _recent_lock:
        plays = _recent_by_device.setdefault(device_id, [])
        plays.append((time.time(), song_id))
        del plays[:-RECENT_PLAYS_MAX]


def get_recent_track_ids(device_id: str, limit: int = None, max_age_seconds: int = None) -> list:
    """Get the IDs of the most recently finished DISTINCT tracks for
    this device, newest first.

    With no arguments this returns everything remembered (up to
    RECENT_PLAYS_MAX tracks), which is the 'don't repeat' list. Pass
    limit and max_age_seconds to get the narrower set used as seeds.
    If the same song finished more than once it is only returned once.

    :param str device_id: The Alexa device ID for the current request
    :param int limit: Maximum number of distinct track IDs to return
    :param int max_age_seconds: Ignore tracks that finished longer ago than this
    :return: A list of song IDs, newest first (possibly empty)
    :rtype: list
    """
    cutoff = (time.time() - max_age_seconds) if max_age_seconds is not None else None

    with _recent_lock:
        plays = list(_recent_by_device.get(device_id, []))

    ids = []
    for timestamp, song_id in reversed(plays):
        if cutoff is not None and timestamp < cutoff:
            continue
        if song_id not in ids:
            ids.append(song_id)
        if limit is not None and len(ids) >= limit:
            break
    return ids


def get_device_lock(device_id: str):
    """Get this device's lock, creating it on first use.

    Held while the waiting song is checked for and moved into the
    queue, so two requests arriving together (for example NextIntent
    and PlaybackNearlyFinished) cannot both add a song.

    :param str device_id: The Alexa device ID for the current request
    :return: A threading.Lock
    :rtype: threading.Lock
    """
    with _device_locks_guard:
        lock = _device_locks.get(device_id)
        if lock is None:
            lock = threading.Lock()
            _device_locks[device_id] = lock
        return lock


def get_generation(device_id: str) -> int:
    """Get the current waiting-song generation for this device.

    :param str device_id: The Alexa device ID for the current request
    :return: The generation number (0 until the first flush)
    :rtype: int
    """
    with _waiting_lock:
        return _generation_by_device.get(device_id, 0)


def start_lookup(device_id: str):
    """Claim the right to start a background lookup for the waiting song.

    Refused (None) if a song is already waiting, or if a lookup for the
    current generation is already running. Otherwise the lookup is
    recorded as running and the current generation is returned, which
    the lookup must pass back to set_waiting_song() and finish_lookup().

    :param str device_id: The Alexa device ID for the current request
    :return: The generation to run the lookup for, or None if not needed
    :rtype: int | None
    """
    with _waiting_lock:
        if _waiting_by_device.get(device_id):
            return None

        generation = _generation_by_device.get(device_id, 0)
        if _lookup_running.get(device_id) == generation:
            return None

        _lookup_running[device_id] = generation
        return generation


def finish_lookup(device_id: str, generation: int) -> None:
    """Record that a background lookup has finished (found a song or gave up).

    Only clears the record if it belongs to the same generation, so a
    stale lookup finishing late cannot clear a newer lookup's record.

    :param str device_id: The Alexa device ID for the current request
    :param int generation: The generation start_lookup() returned
    :return: None
    """
    with _waiting_lock:
        if _lookup_running.get(device_id) == generation:
            del _lookup_running[device_id]


def set_waiting_song(device_id: str, song_id: str, generation: int) -> bool:
    """Store the song found by a background lookup.

    Ignored (False) if the generation has moved on, which means the
    queue was flushed while the lookup was running and the song was
    chosen for something that is no longer playing.

    :param str device_id: The Alexa device ID for the current request
    :param str song_id: The Navidrome ID of the chosen song
    :param int generation: The generation the lookup was started for
    :return: True if the song was stored, False if it was discarded as stale
    :rtype: bool
    """
    with _waiting_lock:
        if generation != _generation_by_device.get(device_id, 0):
            return False
        _waiting_by_device[device_id] = song_id
        return True


def has_waiting_song(device_id: str) -> bool:
    """Is a similar song waiting for this device?

    :param str device_id: The Alexa device ID for the current request
    :return: True if a song is waiting
    :rtype: bool
    """
    with _waiting_lock:
        return bool(_waiting_by_device.get(device_id))


def take_waiting_song(device_id: str):
    """Remove and return the waiting song, if there is one.

    :param str device_id: The Alexa device ID for the current request
    :return: The song ID, or None if nothing is waiting
    :rtype: str | None
    """
    with _waiting_lock:
        return _waiting_by_device.pop(device_id, None)


def discard_waiting_song(device_id: str) -> None:
    """Throw away the waiting song and invalidate any lookup in progress.

    Called whenever the queue is flushed for a new "play": the old
    suggestion was chosen to follow something that no longer applies.

    :param str device_id: The Alexa device ID for the current request
    :return: None
    """
    with _waiting_lock:
        _waiting_by_device.pop(device_id, None)
        _generation_by_device[device_id] = _generation_by_device.get(device_id, 0) + 1