"""
Shared helper functions for the AskNavidrome request handlers.

Everything the handler files have in common lives here, so the repeated
blocks exist once instead of a dozen times:

  device_and_queue()   the device ID and its play queue
  reply_not_found()    the spoken "couldn't find ..." reply
  start_playing()      flush the queue, fill it, and start playback

plus speech sanitising, request/playback logging, the player-state
check, and the background queue fill.

Per-device state (queues, background processes, recently finished
tracks, the waiting similar song) lives in state.py; the Navidrome
connection, logger and config constants live in config.py.
"""

from multiprocessing import Process

from ask_sdk_core.utils import get_intent_name, get_request_type
from ask_sdk_core.handler_input import HandlerInput

import asknavidrome.controller as controller
from asknavidrome import state
from asknavidrome.config import connection, logger

ACTIVE_PLAYER_STATES = ('PLAYING', 'PAUSED', 'BUFFER_UNDERRUN')
"""Alexa audio player states that count as 'something is playing'.
IDLE, STOPPED, FINISHED, or a missing state all count as nothing playing."""


def get_resolved_slot_value(slot):
    """Get the canonical (synonym-resolved) value for a custom-type
    slot, falling back to the raw spoken value if no resolution exists.

    Needed because .value on a custom slot returns the literal words
    spoken - e.g. saying "90s" leaves .value as "90s", not the
    canonical "1990s" the slot type actually maps it to. Built-in
    Amazon slot types (like AMAZON.Genre) don't need this, only
    custom types like the "decade" slot.

    :param slot: An Alexa slot object
    :return: The resolved canonical value, or the raw .value if none exists
    :rtype: str
    """
    try:
        resolutions = slot.resolutions.resolutions_per_authority
        if resolutions:
            for resolution in resolutions:
                if resolution.values:
                    return resolution.values[0].value.name
    except AttributeError:
        pass
    return slot.value


def sanitise_speech_output(speech_string: str) -> str:
    """Sanitise speech output inline with the SSML standard

    Speech Synthesis Markup Language (SSML) has certain ASCII characters that are
    reserved.  This function replaces them with alternatives.

    :param speech_string: The string to process
    :type speech_string: str
    :return: The processed SSML compliant string
    :rtype: str
    """

    logger.debug('In sanitise_speech_output()')

    if '&' in speech_string:
        speech_string = speech_string.replace('&', 'and')
    if '/' in speech_string:
        speech_string = speech_string.replace('/', 'and')
    if '\\' in speech_string:
        speech_string = speech_string.replace('\\', 'and')
    if '"' in speech_string:
        speech_string = speech_string.replace('"', '')
    if "'" in speech_string:
        speech_string = speech_string.replace("'", "")
    if "<" in speech_string:
        speech_string = speech_string.replace('<', '')
    if ">" in speech_string:
        speech_string = speech_string.replace('>', '')

    return speech_string


def _short_device_id(device_id: str) -> str:
    """Truncate a device_id down to its last 8 characters for log
    readability - full device IDs run 150+ characters, too long to be
    useful inline in every log line, but the tail is enough to tell
    devices apart when correlating concurrent test sessions.

    :param str device_id: The full Alexa device ID
    :return: The last 8 characters, or 'unknown' if there is no ID
    :rtype: str
    """
    return device_id[-8:] if device_id else 'unknown'


def log_incoming_request(handler_input: HandlerInput) -> None:
    """Log the device ID, request type, intent name (if any), and every
    filled slot value, for every incoming request - regardless of
    whether it ends up matching a handler. The device ID (truncated)
    is included so concurrent multi-device sessions can be traced
    apart in the log.

    :param handler_input: The current request's HandlerInput
    :return: None
    """
    device_id = handler_input.request_envelope.context.system.device.device_id
    short_id = _short_device_id(device_id)
    req_type = get_request_type(handler_input)

    if req_type == 'IntentRequest':
        intent_name = get_intent_name(handler_input)
        slots = handler_input.request_envelope.request.intent.slots

        if slots:
            slot_summary = ', '.join(
                f'{name}={slot.value!r}' for name, slot in slots.items() if slot
            )
        else:
            slot_summary = '(no slots)'

        logger.info(f'[device ...{short_id}] IntentRequest -> {intent_name} | {slot_summary}')
    else:
        logger.info(f'[device ...{short_id}] {req_type}')


def log_playback_start(play_queue, speech: str) -> None:
    """Log what's about to play, plus the next 10 queued tracks, whenever
    a playback response is sent to Alexa - for visibility into queue
    behaviour that the Test-tab simulator doesn't show. Track IDs are
    included specifically to make duplicate-track issues (the same
    song appearing under two different IDs, e.g. from two different
    albums) diagnosable directly from the log.

    :param play_queue: This device's MediaQueue instance
    :param str speech: The speech text being sent (unused directly, kept
        for call-site symmetry / possible future use)
    :return: None
    """

    current = play_queue.get_current_track()
    if current:
        logger.info(f'Playing track: {current.title} by: {current.artist} (album: {current.album}) [id={current.id}]')

    upcoming = list(play_queue.get_current_queue())[:10]
    if upcoming:
        logger.info('Next in queue:')
        for i, track in enumerate(upcoming, 1):
            logger.info(f'  {i}. {track.title} by: {track.artist} (album: {track.album}) [id={track.id}]')
    else:
        logger.info('Queue is empty after this track.')


def device_and_queue(handler_input: HandlerInput):
    """Get this request's device ID and that device's play queue.

    :param handler_input: The current request's HandlerInput
    :return: (device_id, play_queue)
    :rtype: tuple[str, MediaQueue]
    """
    device_id = handler_input.request_envelope.context.system.device.device_id
    return device_id, state.get_play_queue(device_id)


def reply_not_found(handler_input: HandlerInput, message: str):
    """Speak a "couldn't find ..." message and keep the session open
    for another request.

    :param handler_input: The current request's HandlerInput
    :param str message: What to say (sanitised here)
    :return: The Alexa response
    :rtype: Response
    """
    text = sanitise_speech_output(message)
    handler_input.response_builder.speak(text).ask(text)
    return handler_input.response_builder.response


def player_activity(handler_input: HandlerInput) -> str:
    """Read the Alexa audio player state from the request context.

    :param handler_input: The current request's HandlerInput
    :return: The state name (PLAYING, PAUSED, STOPPED, IDLE ...), or
        'NONE' if the request carries no audio player state
    :rtype: str
    """
    context = handler_input.request_envelope.context
    audio_player = getattr(context, 'audio_player', None)
    activity = getattr(audio_player, 'player_activity', None)

    if activity is None:
        return 'NONE'
    return str(getattr(activity, 'value', activity))


def something_is_playing(handler_input: HandlerInput, play_queue) -> bool:
    """Decide whether this device is currently playing (or paused on) a track.

    Alexa must report an active player state AND this skill's queue
    must know which track is current. The second check covers a
    service restart, where Alexa may still be playing but the queue
    has been emptied and has nothing to chain new tracks to.

    :param handler_input: The current request's HandlerInput
    :param play_queue: This device's MediaQueue instance
    :return: True if something is playing
    :rtype: bool
    """
    activity = player_activity(handler_input)
    current = play_queue.get_current_track()
    has_current = bool(current and current.id)
    playing = activity in ACTIVE_PLAYER_STATES and has_current

    logger.info(f'Player state: activity={activity}, queue has a current track={has_current} -> something playing={playing}')
    return playing


def flush_queue(play_queue, device_id: str) -> None:
    """Empty this device's queue ready for a new "play", and throw away
    the waiting similar song.

    The waiting song was chosen to follow whatever was playing before,
    so it no longer applies. Discarding it also makes any lookup still
    running for the old song throw its result away when it finishes.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :return: None
    """
    play_queue.clear()
    state.discard_waiting_song(device_id)


def queue_worker_thread(connection: object, play_queue: object, song_id_list: list) -> None:
    """Media queue worker

    This function allows media queues to be populated in the background enabling multithreading
    and increasing skill response times.

    :param connection: A SubSonic API connection object
    :type connection: object
    :param play_queue: A MediaQueue object
    :type play_queue: object
    :param song_id_list: A list containing Navidrome song IDs
    :type song_id_list: list
    """

    logger.debug('In playlist processing thread!')
    controller.enqueue_songs(connection, play_queue, song_id_list)
    play_queue.sync()
    logger.debug('Finished playlist processing!')


def start_background_fill(play_queue, device_id: str, song_id_list: list) -> None:
    """Enqueue the first track(s) synchronously (fast enough to stay
    within Alexa's 8-second response window), then spawn a background
    process to enqueue the rest, if there's more than one track.
    Tracks the spawned process against THIS device's ID via
    state.set_background_process(), so a later request from the same
    device can find and stop it - and so it never gets confused with
    another device's own background fill.

    Safe for a song_id_list of any length >= 1, including exactly 1.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :param list song_id_list: Full ordered list of song IDs to queue
    :return: None
    """
    if len(song_id_list) > 1:
        controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
        bg = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
        bg.start()
        state.set_background_process(device_id, bg)
    else:
        controller.enqueue_songs(connection, play_queue, [song_id_list[0]])


def start_playing(handler_input: HandlerInput, device_id: str, play_queue, song_id_list: list, speech_text: str):
    """Replace whatever is queued with song_id_list and start playing it.

    The full "new play" sequence, in the order it must happen: stop the
    previous background fill, flush the queue (discarding the waiting
    similar song), fill the queue from song_id_list, take the first
    track, log, and send the Play directive.

    The previous background fill is only stopped here, once something
    has actually been found. A search that finds nothing leaves the
    current queue alone.

    :param handler_input: The current request's HandlerInput
    :param str device_id: The Alexa device ID for the current request
    :param play_queue: This device's MediaQueue instance
    :param list song_id_list: Song IDs to play, in order. If empty, nothing
        is changed and a "couldn't find any songs" reply is returned.
    :param str speech_text: What to say, also shown on the card
    :return: The Alexa response
    :rtype: Response
    """
    if not song_id_list:
        # Nothing to play (e.g. an artist with no albums): say so and leave
        # the current queue and background fill alone.
        logger.warning(f'start_playing: empty song list for "{speech_text}" - nothing to play')
        return reply_not_found(handler_input, "I couldn't find any songs to play for that.")

    state.stop_background_process(device_id)
    flush_queue(play_queue, device_id)
    start_background_fill(play_queue, device_id, song_id_list)

    speech = sanitise_speech_output(speech_text)
    logger.info(speech)
    card = {'title': 'AskNavidrome', 'text': speech}
    track_details = play_queue.get_next_track()
    log_playback_start(play_queue, speech)

    return controller.start_playback('play', speech, card, track_details, handler_input)