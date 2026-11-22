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

This module also keeps a short, bounded, in-memory list of the tracks
each device has recently FINISHED playing. It is used when the queue
runs dry, to choose the next similar song: recent tracks seed the
choice, and the same list is used to avoid repeating anything heard
lately. It lives in the web service process (not the manager process),
is capped at RECENT_PLAYS_MAX entries per device, and is lost on
restart - which is fine for a window measured in minutes.
"""

from multiprocessing.managers import BaseManager
import threading
import time

import asknavidrome.media_queue as queue

BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()

RECENT_PLAYS_WINDOW_SECONDS = 15 * 60
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