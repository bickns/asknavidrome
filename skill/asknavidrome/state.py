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
"""

from multiprocessing.managers import BaseManager
import asknavidrome.media_queue as queue

BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()

# Confirmed via direct testing that manager.MediaQueue() creates a
# genuinely independent instance on each call, not a shared singleton -
# each entry in this dict is a fully isolated MediaQueue.
_queues_by_device = {}
"""device_id -> MediaQueue instance"""

_background_by_device = {}
"""device_id -> Process instance, or None"""


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
