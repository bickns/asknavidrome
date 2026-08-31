"""
Shared runtime state for the AskNavidrome web service.

This module owns the multiprocessing-safe MediaQueue instance and the
background-process handle used to populate large playlists without
hitting Alexa's 8-second response timeout. It exists as its own module
so that handlers.py, search.py, and app.py can all share the same
queue and background-process reference without circular imports or
each holding their own stale copy of a plain module-level global.
"""

from multiprocessing.managers import BaseManager
import asknavidrome.media_queue as queue

BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()

play_queue = manager.MediaQueue()
"""The shared MediaQueue instance used across every handler."""

# Tracks the background Process used to populate large playlists.
# Private and accessed only via the getter/setter below - a bare
# module-level name reassigned from another module (e.g. handlers.py
# doing `state.background_process = ...`) would rebind the name in
# that module's own namespace, not this one, so a proper setter is
# needed for the reassignment to actually take effect here.
_background_process = None


def get_background_process():
    """Get the currently-tracked background Process, or None if there
    isn't one running.

    :return: The Process object, or None
    :rtype: multiprocessing.Process | None
    """
    return _background_process


def set_background_process(process) -> None:
    """Replace the tracked background Process.

    :param process: The new Process object (or None to clear it)
    :return: None
    """
    global _background_process
    _background_process = process


def stop_background_process() -> None:
    """Terminate and join the current background Process if one is
    running, then clear it. Safe to call even if none is running.

    Every playback-starting handler needs to do this before starting
    a new playlist, to avoid two background threads racing to populate
    the queue at once.

    :return: None
    """
    global _background_process
    if _background_process is not None:
        _background_process.terminate()
        _background_process.join()
        _background_process = None