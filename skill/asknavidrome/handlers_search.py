"""
Search handlers: the intents where the user names something to play.

  play X                    NaviSonicPlaySearch   flush the queue, play X
  queue X                   NaviSonicQueueSearch  add X to the END of the queue
  insert X                  NaviSonicInsertSearch add X to the FRONT of the queue
  play X and similar songs  NaviSonicPlaySimilar  flush, play X, pre-load similar songs

If nothing is playing, queue and insert behave like play.

This is the file that changes most often, so it is kept small and apart
from the stable handlers. Anything shared lives in helpers.py.
"""

from datetime import datetime

from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.utils import is_intent_name, get_slot_value_v2
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response

import asknavidrome.controller as controller
from asknavidrome import state
from asknavidrome.config import connection, logger
from asknavidrome.helpers import (
    device_and_queue,
    reply_not_found,
    sanitise_speech_output,
    something_is_playing,
    log_playback_start,
    start_playing,
)
from asknavidrome.search import (
    resolve_search,
    fuzzy_search_song,
    best_song_match,
)

QUEUE_ADD_LIMIT = 30
"""Most tracks added by one queue/insert request while something is
playing. Each track costs a Navidrome lookup and the reply must reach
Alexa inside its 8 second limit. This is a cautious guess - watch the
timings in the log."""


def add_search_to_queue(handler_input: HandlerInput, at_front: bool) -> Response:
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
    device_id, play_queue = device_and_queue(handler_input)

    query = get_slot_value_v2(handler_input, 'query')
    song_id_list, description = resolve_search(query)

    if song_id_list is None:
        return reply_not_found(handler_input, f"I couldn't find {query.value} in your library.")

    if not something_is_playing(handler_input, play_queue):
        logger.info('Nothing is playing - treating this as a play request')
        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Playing {description}')

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


class NaviSonicPlaySearch(AbstractRequestHandler):
    """Handle NaviSonicPlaySearch ("play X")

    Free-text play: resolves the query against songs, albums, artists,
    then genres (in that priority order), flushes the queue (and the
    waiting similar song) and starts playback. A song match plays that
    one song only; the next similar song is found in the background
    (see similar.py).
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlaySearch')
        device_id, play_queue = device_and_queue(handler_input)

        query = get_slot_value_v2(handler_input, 'query')
        song_id_list, description = resolve_search(query)

        if song_id_list is None:
            return reply_not_found(handler_input, f"I couldn't find {query.value} in your library.")

        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Playing {description}')


class NaviSonicQueueSearch(AbstractRequestHandler):
    """Handle NaviSonicQueueSearch ("queue X")

    Add the result to the END of the queue. If nothing is playing it
    starts playing instead - see add_search_to_queue().
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicQueueSearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicQueueSearch')
        return add_search_to_queue(handler_input, at_front=False)


class NaviSonicInsertSearch(AbstractRequestHandler):
    """Handle NaviSonicInsertSearch ("insert X")

    Add the result to the FRONT of the queue so it plays after the
    current track. If nothing is playing it starts playing instead -
    see add_search_to_queue().
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicInsertSearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicInsertSearch')
        return add_search_to_queue(handler_input, at_front=True)


class NaviSonicPlaySimilar(AbstractRequestHandler):
    """Handle NaviSonicPlaySimilar ("play X and similar songs")

    Play the song, then pre-load a batch of songs similar to it via
    Navidrome's similarity engine. This is the only place a batch of
    similar songs is queued up front - a plain "play X" plays only X.

    Note: the similar-songs lookup here is made live, inside the
    request, and has been seen to take about 6 seconds, which is close
    to Alexa's 8 second limit.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySimilar')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlaySimilar')
        device_id, play_queue = device_and_queue(handler_input)

        song = get_slot_value_v2(handler_input, 'song')
        song_results = fuzzy_search_song(song.value)

        if not song_results:
            return reply_not_found(handler_input, f"I couldn't find {song.value} in your library.")

        best_match = best_song_match(song.value, song_results)[0]
        song_id = best_match.get('id')
        song_title = best_match.get('title', song.value)
        song_artist = best_match.get('artist', 'an unknown artist')

        similar_ids = connection.get_similar_songs(song_id, 100)
        combined = [song_id] + [s for s in similar_ids if s != song_id]

        if len(combined) > 1:
            speech = f'Playing {song_title} by {song_artist} and similar songs'
        else:
            speech = f'Playing {song_title} by {song_artist}'

        return start_playing(handler_input, device_id, play_queue, combined, speech)