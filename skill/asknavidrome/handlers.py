"""
Request handlers for the AskNavidrome Alexa skill.

Every AbstractRequestHandler subclass the skill registers lives here -
both the intent handlers (playing music, searching, favouriting) and
the AudioPlayer directive handlers (playback lifecycle events, next/
previous, pause/resume). Handler-support utility functions used only
by this file (speech sanitisation, playback logging, the background
queue-fill worker) live here too, rather than in a separate utils
module, since nothing outside handlers.py needs them.

Every handler that touches the play queue extracts device_id from the
request and calls state.get_play_queue(device_id) to get THAT DEVICE's
queue, not a shared one - see state.py for why. Handler-support
functions that need the queue (log_playback_start,
add_next_similar_song, _start_background_fill) take it as a parameter
rather than re-deriving device_id themselves, since the calling
handler has already fetched it once.

How the queue is fed:

  play X                    flush the queue, play X (nothing else added)
  queue X                   add X to the END of the queue
  insert X                  add X to the FRONT of the queue (plays next)
  play X and similar songs  flush, play X, pre-load a batch of similar songs
  queue runs dry            add ONE similar song, just in time

If nothing is playing, queue and insert behave like play.

Shared state (per-device play queues, background-fill processes,
recently finished tracks) lives in state.py; connection, logger, and
config constants live in config.py; free-text search resolution lives
in search.py. This file imports from all three rather than owning any
of that itself.
"""

from multiprocessing import Process
from datetime import datetime
import random

from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.utils import is_request_type, is_intent_name, get_slot_value_v2, get_intent_name, get_request_type
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response

import asknavidrome.controller as controller
from asknavidrome import state
from asknavidrome.config import connection, logger, min_song_count, DECADE_RANGES
from asknavidrome.search import (
    resolve_search,
    fuzzy_search_artist,
    fuzzy_search_song,
    fuzzy_search_album,
    best_song_match,
    best_album_match,
    expand_single_track_album,
    pick_similar_song,
)

RECENT_SEEDS_MAX = 3
"""Number of recently finished songs (within the window kept by
state.py) used, along with the current song, to seed the next similar song."""

QUEUE_ADD_LIMIT = 30
"""Most tracks added by one queue/insert request while something is
playing. Each track costs a Navidrome lookup and the reply must reach
Alexa inside its 8 second limit. This is a cautious guess - watch the
timings in the log."""

ACTIVE_PLAYER_STATES = ('PLAYING', 'PAUSED', 'BUFFER_UNDERRUN')
"""Alexa audio player states that count as 'something is playing'.
IDLE, STOPPED, FINISHED, or a missing state all count as nothing playing."""


#
# Handler-support helper functions
#

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
    :return: The last 8 characters, or the whole string if shorter
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


def _player_activity(handler_input: HandlerInput) -> str:
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


def _something_is_playing(handler_input: HandlerInput, play_queue) -> bool:
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
    activity = _player_activity(handler_input)
    current = play_queue.get_current_track()
    has_current = bool(current and current.id)
    playing = activity in ACTIVE_PLAYER_STATES and has_current

    logger.info(f'Player state: activity={activity}, queue has a current track={has_current} -> something playing={playing}')
    return playing


def add_next_similar_song(play_queue, device_id: str, current_track) -> bool:
    """When the queue has run dry, add ONE song that suits what has
    been listened to, so playback carries on.

    Seeds: the current track plus up to RECENT_SEEDS_MAX songs that
    finished within state.RECENT_PLAYS_WINDOW_SECONDS. Never chosen:
    the current track, the seeds, or anything in the last
    state.RECENT_PLAYS_MAX finished tracks.

    The song is added via play_queue.add_tracks(), which runs inside
    the manager process and sets the previous_id that Alexa's ENQUEUE
    directive requires.

    :param play_queue: This device's MediaQueue instance
    :param str device_id: The Alexa device ID for the current request
    :param current_track: The Track that is playing / has just finished
    :return: True if a song was added, False otherwise
    :rtype: bool
    """

    if not current_track or not current_track.id:
        logger.info('add_next_similar_song: no current track to seed from')
        return False

    recent = state.get_recent_track_ids(device_id, RECENT_SEEDS_MAX, state.RECENT_PLAYS_WINDOW_SECONDS)
    seeds = [current_track.id] + [r for r in recent if r != current_track.id]

    exclude = set(state.get_recent_track_ids(device_id))
    exclude.update(seeds)

    logger.info(f'add_next_similar_song: {len(seeds)} seeds, {len(exclude)} songs excluded')

    song_id = pick_similar_song(seeds, exclude)
    if song_id is None:
        logger.info('add_next_similar_song: nothing suitable found')
        return False

    track = controller.build_track(connection, song_id)
    play_queue.add_tracks([track])
    logger.info(f'Queue was empty - added one similar song: {track.title} by {track.artist} [id={track.id}]')
    return True


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


def _start_background_fill(play_queue, device_id: str, song_id_list: list) -> None:
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


def _add_search_to_queue(handler_input: HandlerInput, at_front: bool) -> Response:
    """Shared body of the queue (add to END) and insert (add to FRONT) intents.

    If nothing is playing, this behaves exactly like play: the old
    queue is flushed and the result starts playing. If something is
    playing, the result is added to the queue and playback carries on.
    While something is playing the result is capped at QUEUE_ADD_LIMIT
    tracks.

    :param handler_input: The current request's HandlerInput
    :param bool at_front: True for insert (front of queue), False for queue (end)
    :return: The Alexa response
    :rtype: Response
    """
    device_id = handler_input.request_envelope.context.system.device.device_id
    play_queue = state.get_play_queue(device_id)

    query = get_slot_value_v2(handler_input, 'query')
    song_id_list, description = resolve_search(query)

    if song_id_list is None:
        text = sanitise_speech_output(f"I couldn't find {query.value} in your library.")
        handler_input.response_builder.speak(text).ask(text)
        return handler_input.response_builder.response

    if not _something_is_playing(handler_input, play_queue):
        logger.info('Nothing is playing - treating this as a play request')
        state.stop_background_process(device_id)
        play_queue.clear()
        _start_background_fill(play_queue, device_id, song_id_list)

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(play_queue, speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)

    if len(song_id_list) > QUEUE_ADD_LIMIT:
        logger.info(f'Result has {len(song_id_list)} tracks - adding only the first {QUEUE_ADD_LIMIT}')
        song_id_list = song_id_list[:QUEUE_ADD_LIMIT]

    started = datetime.now()
    tracks = [controller.build_track(connection, song_id) for song_id in song_id_list]
    play_queue.add_tracks(tracks, at_front=at_front)
    elapsed = (datetime.now() - started).total_seconds()

    where = 'FRONT' if at_front else 'END'
    logger.info(f'Added {len(tracks)} tracks to the {where} of the queue in {elapsed:.2f}s: {description}')
    log_playback_start(play_queue, '')

    if at_front:
        speech = sanitise_speech_output(f'Inserted {description} to play next')
    else:
        speech = sanitise_speech_output(f'Added {description} to the queue')

    handler_input.response_builder.speak(speech)
    return handler_input.response_builder.response


#
# Core Handlers
#

class LaunchRequestHandler(AbstractRequestHandler):
    """Handle LaunchRequest and NavigateHomeIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (
            is_request_type('LaunchRequest')(handler_input) or
            is_intent_name('AMAZON.NavigateHomeIntent')(handler_input)
        )

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In LaunchRequestHandler')

        connection.ping()
        speech = sanitise_speech_output('Ready!')

        handler_input.response_builder.speak(speech).ask(speech)
        return handler_input.response_builder.response


class CheckAudioInterfaceHandler(AbstractRequestHandler):
    """Check if device supports audio play.

    This can be used as the first handler to be checked, before invoking
    other handlers, thus making the skill respond to unsupported devices
    without doing much processing.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        if handler_input.request_envelope.context.system.device:
            # Since skill events won't have device information
            return handler_input.request_envelope.context.system.device.supported_interfaces.audio_player is None
        else:
            return False

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In CheckAudioInterfaceHandler')

        _ = handler_input.attributes_manager.request_attributes['_']
        handler_input.response_builder.speak('This device is not supported').set_should_end_session(True)

        return handler_input.response_builder.response


class SkillEventHandler(AbstractRequestHandler):
    """Close session for skill events or when session ends.

    Handler to handle session end or skill events (SkillEnabled,
    SkillDisabled etc.)
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (handler_input.request_envelope.request.object_type.startswith(
                'AlexaSkillEvent') or
                is_request_type('SessionEndedRequest')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In SkillEventHandler')

        return handler_input.response_builder.response


class HelpHandler(AbstractRequestHandler):
    """Handle HelpIntent"""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('AMAZON.HelpIntent')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In HelpHandler')

        text = sanitise_speech_output('AskNavidrome lets you interact with media servers that offer a Subsonic compatible A.P.I.')
        handler_input.response_builder.speak(text)

        return handler_input.response_builder.response


class FallbackHandler(AbstractRequestHandler):
    """Handle AMAZON.FallbackIntent

    Alexa did not match the utterance confidently to any intent. Amazon
    deliberately withholds the raw spoken text and any slot value for this
    intent, so the actual query cannot be recovered here - only logged as
    an occurrence for pattern-spotting.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('AMAZON.FallbackIntent')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.warning('FallbackIntent triggered - Alexa could not confidently match the utterance')
        text = sanitise_speech_output("I didn't catch that as something to play. Try saying play, followed by a song, artist, or genre.")
        handler_input.response_builder.speak(text).ask(text)
        return handler_input.response_builder.response


class NaviSonicPlayMusicByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayMusicByArtist

    DEAD CODE - this intent has been removed from the interaction
    model. Awaiting cleanup.

    Play a selection of songs for the given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByArtist')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        artist = get_slot_value_v2(handler_input, 'artist')
        artist_lookup = fuzzy_search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
            song_id_list = connection.build_song_list_from_albums(artist_album_lookup, min_song_count)
            play_queue.clear()

            _start_background_fill(play_queue, device_id, song_id_list)

            speech = sanitise_speech_output(f'Playing music by: {artist.value}')
            logger.info(speech)

            card = {'title': 'AskNavidrome', 'text': speech}

            play_queue.shuffle()
            track_details = play_queue.get_next_track()
            log_playback_start(play_queue, speech)
            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicShuffleArtist(AbstractRequestHandler):
    """Handle NaviSonicShuffleArtist

    Shuffle and play the artist's ENTIRE catalogue, not just a capped
    subset - distinct from NaviSonicPlayMusicByArtist, which caps at
    min_song_count.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicShuffleArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicShuffleArtist')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        artist = get_slot_value_v2(handler_input, 'artist')
        artist_lookup = fuzzy_search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
        song_id_list = connection.build_song_list_from_albums(artist_album_lookup, -1)  # unlimited - full catalogue
        random.shuffle(song_id_list)

        play_queue.clear()
        _start_background_fill(play_queue, device_id, song_id_list)

        speech = sanitise_speech_output(f'Shuffling all songs by {artist.value}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(play_queue, speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlaySimilar(AbstractRequestHandler):
    """Handle NaviSonicPlaySimilar

    "play X and similar songs": play the song, then pre-load a batch of
    songs similar to it via Navidrome's similarity engine. This is the
    only place a batch of similar songs is queued up front - a plain
    "play X" plays only X.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySimilar')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlaySimilar')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        song = get_slot_value_v2(handler_input, 'song')
        song_results = fuzzy_search_song(song.value)

        if not song_results:
            text = sanitise_speech_output(f"I couldn't find {song.value} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        song_results = best_song_match(song.value, song_results)
        best_match = song_results[0]
        song_id = best_match.get('id')
        song_title = best_match.get('title', song.value)
        song_artist = best_match.get('artist', 'an unknown artist')

        similar_ids = connection.get_similar_songs(song_id, 100)
        combined = [song_id] + [s for s in similar_ids if s != song_id]

        play_queue.clear()

        if len(combined) > 1:
            _start_background_fill(play_queue, device_id, combined)
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist} and similar songs')
        else:
            controller.enqueue_songs(connection, play_queue, [combined[0]])
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist}')

        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(play_queue, speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicByDecade(AbstractRequestHandler):
    """Handle NaviSonicPlayMusicByDecade

    Play music from a given decade, optionally filtered by genre.
    Operates at the song level (not album level), so compilations and
    "best of" collections are covered correctly.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByDecade')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByDecade')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        decade = get_slot_value_v2(handler_input, 'decade')
        genre = get_slot_value_v2(handler_input, 'genre')

        decade_value = get_resolved_slot_value(decade)
        year_range = DECADE_RANGES.get(decade_value)

        if year_range is None:
            text = sanitise_speech_output(f"I don't recognise the decade {decade.value}.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        from_year, to_year = year_range
        genre_name = genre.value if genre else None
        songs = connection.get_songs_by_year_range(from_year, to_year, genre_name)

        description = f'{genre.value} music from the {decade_value}' if genre else f'music from the {decade_value}'

        if not songs:
            text = sanitise_speech_output(f"You don't have any {description} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        song_id_list = [s.get('id') for s in songs]

        play_queue.clear()
        _start_background_fill(play_queue, device_id, song_id_list)

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(play_queue, speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayAlbumByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayAlbumByArtist

    DEAD CODE - this intent has been removed from the interaction
    model. Awaiting cleanup.

    Play a given album by a given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayAlbumByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayAlbumByArtist')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        artist = get_slot_value_v2(handler_input, 'artist')
        album = get_slot_value_v2(handler_input, 'album')

        if artist is not None and album is not None:
            logger.debug(f'Searching for the album {album.value} by {artist.value}')

            artist_lookup = fuzzy_search_artist(artist.value)

            if artist_lookup is None:
                text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
                handler_input.response_builder.speak(text).ask(text)
                return handler_input.response_builder.response

            else:
                artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
                result = [album_result for album_result in artist_album_lookup if album_result.get('name').lower() == album.value.lower()]

                if not result:
                    text = sanitise_speech_output(f"I couldn't find an album called {album.value} by {artist.value} in the collection.")
                    handler_input.response_builder.speak(text).ask(text)
                    return handler_input.response_builder.response

                song_id_list = connection.build_song_list_from_albums(result, -1)
                play_queue.clear()
                _start_background_fill(play_queue, device_id, song_id_list)

                speech = sanitise_speech_output(f'Playing {album.value} by: {artist.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = play_queue.get_next_track()
                log_playback_start(play_queue, speech)

                return controller.start_playback('play', speech, card, track_details, handler_input)

        elif artist is None and album:
            logger.debug(f'Searching for the album {album.value}')

            result = fuzzy_search_album(album.value)

            if result is None:
                text = sanitise_speech_output(f"I couldn't find the album {album.value} in the collection.")
                handler_input.response_builder.speak(text).ask(text)
                return handler_input.response_builder.response

            else:
                best_match = best_album_match(album.value, result)
                song_id_list = connection.build_song_list_from_albums([best_match], -1)
                logger.info(f'NaviSonicPlayAlbumByArtist: album match "{best_match.get("name")}" (songCount={best_match.get("songCount")}) returned {len(song_id_list)} song IDs')

                song_id_list, description = expand_single_track_album(song_id_list, album.value)

                play_queue.clear()
                _start_background_fill(play_queue, device_id, song_id_list)

                speech = sanitise_speech_output(f'Playing {description}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = play_queue.get_next_track()
                log_playback_start(play_queue, speech)

                return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlaySearch(AbstractRequestHandler):
    """Handle NaviSonicPlaySearch

    Free-text play: stops what is playing, flushes the queue, resolves
    the query against songs, albums, artists, then genres (in that
    priority order), and starts playback. A song match plays that one
    song only; when the queue runs dry, similar songs are added one at
    a time (see add_next_similar_song()).
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlaySearch')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        query = get_slot_value_v2(handler_input, 'query')
        song_id_list, description = resolve_search(query)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find {query.value} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        play_queue.clear()
        _start_background_fill(play_queue, device_id, song_id_list)

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(play_queue, speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicQueueSearch(AbstractRequestHandler):
    """Handle NaviSonicQueueSearch ("queue X")

    Add the result to the END of the queue. If nothing is playing it
    starts playing instead - see _add_search_to_queue().
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicQueueSearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicQueueSearch')
        return _add_search_to_queue(handler_input, at_front=False)


class NaviSonicInsertSearch(AbstractRequestHandler):
    """Handle NaviSonicInsertSearch ("insert X")

    Add the result to the FRONT of the queue so it plays after the
    current track. If nothing is playing it starts playing instead -
    see _add_search_to_queue().
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicInsertSearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicInsertSearch')
        return _add_search_to_queue(handler_input, at_front=True)


class NaviSonicPlayPlaylist(AbstractRequestHandler):
    """Handle NaviSonicPlayPlaylist

    Play the given playlist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayPlaylist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayPlaylist')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        playlist = get_slot_value_v2(handler_input, 'playlist')
        playlist_id = connection.search_playlist(playlist.value)

        if playlist_id is None:
            text = sanitise_speech_output("I couldn't find the playlist " + str(playlist.value) + ' in the collection.')
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            song_id_list = connection.build_song_list_from_playlist(playlist_id)
            play_queue.clear()
            _start_background_fill(play_queue, device_id, song_id_list)

            speech = sanitise_speech_output('Playing playlist ' + str(playlist.value))
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(play_queue, speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicByGenre(AbstractRequestHandler):
    """Play songs from the given genre

    50 tracks from the given genre are shuffled and played
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByGenre')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByGenre')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        genre = get_slot_value_v2(handler_input, 'genre')
        song_id_list = connection.build_song_list_from_genre(genre.value, min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find any {genre.value} songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()
            _start_background_fill(play_queue, device_id, song_id_list)

            speech = sanitise_speech_output(f'Playing {genre.value} music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(play_queue, speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicRandom(AbstractRequestHandler):
    """Handle the NaviSonicPlayMusicRandom intent

    Play a random selection of music.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicRandom')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicRandom')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        song_id_list = connection.build_random_song_list(min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output("I couldn't find any songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()
            _start_background_fill(play_queue, device_id, song_id_list)

            speech = sanitise_speech_output('Playing random music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(play_queue, speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayFavouriteSongs(AbstractRequestHandler):
    """Handle the NaviSonicPlayFavouriteSongs intent

    Play all starred / liked songs, songs are automatically shuffled.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayFavouriteSongs')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayFavouriteSongs')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        state.stop_background_process(device_id)

        song_id_list = connection.build_song_list_from_favourites()

        if song_id_list is None:
            text = sanitise_speech_output("You don't have any favourite songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()
            _start_background_fill(play_queue, device_id, song_id_list)

            speech = sanitise_speech_output('Playing your favourite tracks.')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(play_queue, speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicRandomiseQueue(AbstractRequestHandler):
    """Handle NaviSonicRandomiseQueue Intent

    Shuffle the current play queue
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicRandomiseQueue')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicRandomiseQueue Handler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        play_queue.shuffle()
        play_queue.sync()

        return handler_input.response_builder.response


class NaviSonicSongDetails(AbstractRequestHandler):
    """Handle NaviSonicSongDetails Intent

    Returns information on the track that is currently playing
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicSongDetails')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicSongDetails Handler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        current_track = play_queue.get_current_track()

        title = sanitise_speech_output(current_track.title)
        artist = sanitise_speech_output(current_track.artist)
        album = sanitise_speech_output(current_track.album)

        text = f'This is {title} by {artist}, from the album {album}'
        handler_input.response_builder.speak(text)

        return handler_input.response_builder.response


class NaviSonicStarSong(AbstractRequestHandler):
    """Handle NaviSonicStarSong Intent

    Star / favourite the current song
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicStarSong')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicStarSong Handler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')

        return handler_input.response_builder.response


class NaviSonicUnstarSong(AbstractRequestHandler):
    """Handle NaviSonicUnstarSong Intent

    Star / favourite the current song
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicUnstarSong')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicUnstarSong Handler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')
        connection.unstar_entry(song_id, 'song')

        return handler_input.response_builder.response


#
# AudioPlayer Handlers
#

class PlaybackStartedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted Directive received.

    Confirming that the requested audio file began playing.
    Do not send any specific response.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStarted')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStartedHandler')
        logger.info('Playback started')

        return handler_input.response_builder.response


class PlaybackStoppedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStopped Directive received.

    Confirming that the requested audio file stopped playing.
    Do not send any specific response.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStopped')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStoppedHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        play_queue.set_current_track_offset(handler_input.request_envelope.request.offset_in_milliseconds)

        current_track = play_queue.get_current_track()
        logger.debug(f'Stored track offset of: {current_track.offset} ms for {current_track.title}')
        logger.info('Playback stopped')

        return handler_input.response_builder.response


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackNearlyFinished Directive received.

    Alexa asks for the next track a few seconds before the current one
    ends. If nothing is queued, one similar song is chosen just in time
    (see add_next_similar_song()), then the next track is handed to
    Alexa. Handing it over also marks it as committed in the queue, so
    that an insert arriving now goes behind it rather than ahead of it.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackNearlyFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackNearlyFinishedHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        if len(play_queue.get_buffer()) == 0:
            add_next_similar_song(play_queue, device_id, play_queue.get_current_track())

        if len(play_queue.get_buffer()) == 0:
            logger.info('Buffer still empty after adding a similar song - nothing more to queue')
            return handler_input.response_builder.response

        logger.info('Queuing next track...')
        track_details = play_queue.enqueue_next_track()
        return controller.start_playback('continue', None, None, track_details, handler_input)


class PlaybackFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFinished Directive received.

    Fires when a track runs to its natural end (a skipped or stopped
    track produces PlaybackStopped instead). The finished track is
    scrobbled and recorded in state, so that similar-song choices can
    be seeded from it and avoid repeating it.

    If the queue is empty when a track finishes, one similar song is
    added before advancing, rather than crashing on an empty deque.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFinishedHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        timestamp_ms = datetime.now().timestamp()
        current_track = play_queue.get_current_track()
        connection.scrobble(current_track.id, timestamp_ms)
        state.record_finished_track(device_id, current_track.id)
        logger.info(f'Recorded finished track: {current_track.title} by {current_track.artist} [id={current_track.id}]')

        if play_queue.get_queue_count() == 0:
            add_next_similar_song(play_queue, device_id, current_track)

        if play_queue.get_queue_count() > 0:
            play_queue.get_next_track()
        else:
            logger.info('Queue is empty and no similar song was found - nothing left to play')

        return handler_input.response_builder.response


class PausePlaybackHandler(AbstractRequestHandler):
    """Handler for stopping audio.

    Handles Stop, Cancel and Pause Intents and PauseCommandIssued event.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.StopIntent')(handler_input) or
                is_intent_name('AMAZON.CancelIntent')(handler_input) or
                is_intent_name('AMAZON.PauseIntent')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PausePlaybackHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        play_queue.sync()

        return controller.stop(handler_input)


class ResumePlaybackHandler(AbstractRequestHandler):
    """Handler for resuming audio on different events.

    Handles PlayAudio Intent, Resume Intent.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.ResumeIntent')(handler_input) or
                is_intent_name('PlayAudio')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In ResumePlaybackHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        current_track = play_queue.get_current_track()

        if current_track.offset > 0:
            logger.info('Resuming ' + str(current_track.title))
            logger.info('Offset ' + str(current_track.offset))

            return controller.start_playback('play', None, None, current_track, handler_input)

        elif play_queue.get_queue_count() > 0 and current_track.offset == 0:
            logger.info('Resuming - There was no paused track, getting next track from queue')
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', None, None, track_details, handler_input)


class NextPlaybackHandler(AbstractRequestHandler):
    """Handle NextIntent.

    If the queue is empty when "next" is requested, one similar song is
    added first. If that also fails, respond gracefully rather than
    crashing - speaking only for the voice-intent case, since
    PlaybackController.NextCommandIssued (a hardware/touch button
    event) cannot carry outputSpeech.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.NextIntent')(handler_input) or
                is_request_type('PlaybackController.NextCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NextPlaybackHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)
        logger.debug(f'Queue count before next: {play_queue.get_queue_count()}')

        if play_queue.get_queue_count() == 0:
            add_next_similar_song(play_queue, device_id, play_queue.get_current_track())

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after trying to add a similar song')
            if is_intent_name('AMAZON.NextIntent')(handler_input):
                text = sanitise_speech_output("There's nothing else queued up.")
                handler_input.response_builder.speak(text)
            return handler_input.response_builder.response

        track_details = play_queue.get_next_track()
        track_details.offset = 0
        return controller.start_playback('play', None, None, track_details, handler_input)


class PreviousPlaybackHandler(AbstractRequestHandler):
    """Handle PreviousIntent.

    Guards against an empty history deque the same way NextPlaybackHandler
    guards against an empty queue.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.PreviousIntent')(handler_input) or
                is_request_type('PlaybackController.PreviousCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PreviousPlaybackHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        if play_queue.get_history_count() == 0:
            logger.info('No previous track in history')
            if is_intent_name('AMAZON.PreviousIntent')(handler_input):
                text = sanitise_speech_output("There's nothing before this.")
                handler_input.response_builder.speak(text)
            return handler_input.response_builder.response

        track_details = play_queue.get_previous_track()
        track_details.offset = 0
        return controller.start_playback('play', None, None, track_details, handler_input)


class PlaybackFailedEventHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFailed Directive received.

    Logging the error and moving on to the next track with no output
    speech. If the queue is empty, one similar song is added first.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFailed')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFailedHandler')
        device_id = handler_input.request_envelope.context.system.device.device_id
        play_queue = state.get_play_queue(device_id)

        current_track = play_queue.get_current_track()
        song_id = current_track.id

        logger.error(f'Playback Failed: {handler_input.request_envelope.request.error}')
        logger.error(f'Failed playing track with ID: {song_id}')

        if play_queue.get_queue_count() == 0:
            add_next_similar_song(play_queue, device_id, current_track)

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after a playback failure and trying to add a similar song')
            return handler_input.response_builder.response

        track_details = play_queue.get_next_track()
        track_details.offset = 0
        log_playback_start(play_queue, None)

        return controller.start_playback('play', None, None, track_details, handler_input)