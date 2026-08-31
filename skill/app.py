from datetime import datetime
from difflib import SequenceMatcher
from flask import Flask, render_template
import logging
from multiprocessing import Process
from multiprocessing.managers import BaseManager
import os
import random
import sys
import re
import traceback

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractRequestInterceptor, AbstractResponseInterceptor
from ask_sdk_core.utils import is_request_type, is_intent_name, get_slot_value_v2, get_intent_name, get_request_type
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from flask_ask_sdk.skill_adapter import SkillAdapter

import asknavidrome.subsonic_api as api
import asknavidrome.media_queue as queue
import asknavidrome.controller as controller

DECADE_RANGES = {
    '1920s': (1920, 1929), '1960s': (1960, 1969), '1970s': (1970, 1979),
    '1980s': (1980, 1989), '1990s': (1990, 1999), '2000s': (2000, 2009),
    '2010s': (2010, 2019), '2020s': (2020, 2029),
}

DEFAULT_GENRES = ['rock', 'pop', 'dance', 'club', 'classical', 'disco', 'electronic', 'trance', 'house',
                    'metal', 'punk', 'reggae', 'soul', 'techno', 'blues', 'folk', 'indie', 'rap']

SEARCH_RESULT_COUNT = 100  # target queue size for free-text artist/genre matches in resolve_search()
MIN_ACCEPTABLE_SCORE = 2.0  # "contains" match or better - below this, a song-title match is too weak to trust
ARTIST_SPELLING_CORRECTION_THRESHOLD = 0.75  # min similarity to accept a fuzzy-corrected artist name

# Create web service
app = Flask(__name__)

# Create skill object
sb = SkillBuilder()

# Setup Logging
logger = logging.getLogger()
level = logging.getLevelName('DEBUG')
logger.setLevel(level)

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(level)
handler.setFormatter(log_formatter)

logger.addHandler(handler)

#
# Get service configuration
#

logger.info('AskNavidrome 0.10!')
logger.debug('Getting configuration from the environment...')

try:
    if 'NAVI_SKILL_ID' in os.environ:
        sb.skill_id = os.getenv('NAVI_SKILL_ID')
        logger.info(f'Skill ID set to: {sb.skill_id}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Alexa skill ID was not found! {err}')
    raise

try:
    if 'NAVI_SONG_COUNT' in os.environ:
        min_song_count = os.getenv('NAVI_SONG_COUNT')
        logger.info(f'Minimum song count is set to: {min_song_count}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The minimum song count was not found! {err}')
    raise

try:
    if 'NAVI_URL' in os.environ:
        navidrome_url = os.getenv('NAVI_URL')
        logger.info(f'The URL for Navidrome is set to: {navidrome_url}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The URL of the Navidrome server was not found! {err}')
    raise

try:
    if 'NAVI_USER' in os.environ:
        navidrome_user = os.getenv('NAVI_USER')
        logger.info(f'The Navidrome user name is set to: {navidrome_user}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome user name was not found! {err}')
    raise

try:
    if 'NAVI_PASS' in os.environ:
        navidrome_passwd = os.getenv('NAVI_PASS')
        logger.info('The Navidrome password is set')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome password was not found! {err}')
    raise

try:
    if 'NAVI_PORT' in os.environ:
        navidrome_port = os.getenv('NAVI_PORT')
        logger.info(f'The Navidrome port is set to: {navidrome_port}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome port was not found! {err}')
    raise

try:
    if 'NAVI_API_PATH' in os.environ:
        navidrome_api_location = os.getenv('NAVI_API_PATH')
        logger.info(f'The Navidrome API path is set to: {navidrome_api_location}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome API path was not found! {err}')
    raise

try:
    if 'NAVI_API_VER' in os.environ:
        navidrome_api_version = os.getenv('NAVI_API_VER')
        logger.info(f'The Navidrome API version is set to: {navidrome_api_version}')
    else:
        raise NameError
except NameError as err:
    logger.error(f'The Navidrome API version was not found! {err}')
    raise

logger.debug('Configuration has been successfully loaded')

if 'NAVI_DEBUG' in os.environ:
    navidrome_log_level = int(os.getenv('NAVI_DEBUG'))

    if navidrome_log_level == 0:
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')
    elif navidrome_log_level == 1:
        logger.setLevel(logging.INFO)
        logger.info('Log level set to INFO')
    elif navidrome_log_level == 2:
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')
    elif navidrome_log_level == 3:
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')
    else:
        navidrome_log_level = 0
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')

BaseManager.register('MediaQueue', queue.MediaQueue)
manager = BaseManager()
manager.start()
play_queue = manager.MediaQueue()
logger.debug('MediaQueue object created...')

backgroundProcess = None

connection = api.SubsonicConnection(navidrome_url,
                                    navidrome_user,
                                    navidrome_passwd,
                                    navidrome_port,
                                    navidrome_api_location,
                                    navidrome_api_version)

try:
    connection.ping()
except:
    raise RuntimeError('Could not connect to SubSonic API!')

try:
    known_genres = {g.lower() for g in connection.get_genre_list()}
    logger.info(f'Loaded {len(known_genres)} genres from Navidrome')
except Exception as e:
    logger.warning(f'Could not load genre list, falling back to defaults: {e}')
    known_genres = set(DEFAULT_GENRES)

try:
    known_artists = connection.get_artist_list()
    logger.info(f'Loaded {len(known_artists)} artists from Navidrome')
except Exception as e:
    logger.warning(f'Could not load artist list, spelling-correction fallback will be disabled: {e}')
    known_artists = []


logger.info('AskNavidrome Web Service is ready to start!')


#
# Handler Classes
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
    """Check if device supports audio play."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        if handler_input.request_envelope.context.system.device:
            return handler_input.request_envelope.context.system.device.supported_interfaces.audio_player is None
        else:
            return False

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In CheckAudioInterfaceHandler')
        _ = handler_input.attributes_manager.request_attributes['_']
        handler_input.response_builder.speak('This device is not supported').set_should_end_session(True)
        return handler_input.response_builder.response


class SkillEventHandler(AbstractRequestHandler):
    """Close session for skill events or when session ends."""

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

    Play a selection of songs for the given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayMusicByArtist')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

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

            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()

            speech = sanitise_speech_output(f'Playing music by: {artist.value}')
            logger.info(speech)

            card = {'title': 'AskNavidrome', 'text': speech}

            play_queue.shuffle()
            track_details = play_queue.get_next_track()
            log_playback_start(speech)
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
        global backgroundProcess
        logger.debug('In NaviSonicShuffleArtist')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

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
        controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
        backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
        backgroundProcess.start()

        speech = sanitise_speech_output(f'Shuffling all songs by {artist.value}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlaySimilar(AbstractRequestHandler):
    """Handle NaviSonicPlaySimilar

    Play a given song, then continue with similar songs, seeded from the
    matched track's ID via Navidrome's own similarity engine.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySimilar')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlaySimilar')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

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

        similar_ids = connection.get_similar_songs(song_id, SEARCH_RESULT_COUNT)
        combined = [song_id] + [s for s in similar_ids if s != song_id]

        play_queue.clear()

        if len(combined) > 1:
            controller.enqueue_songs(connection, play_queue, [combined[0], combined[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, combined[2:]))
            backgroundProcess.start()
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist} and similar songs')
        else:
            controller.enqueue_songs(connection, play_queue, [combined[0]])
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist}')

        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(speech)

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
        global backgroundProcess
        logger.debug('In NaviSonicPlayMusicByDecade')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

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
        controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
        backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
        backgroundProcess.start()

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayAlbumByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayAlbumByArtist

    Play a given album by a given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayAlbumByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayAlbumByArtist')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

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

                controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
                backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
                backgroundProcess.start()

                speech = sanitise_speech_output(f'Playing {album.value} by: {artist.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = play_queue.get_next_track()
                log_playback_start(speech)

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
                play_queue.clear()

                controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
                backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
                backgroundProcess.start()

                speech = sanitise_speech_output(f'Playing {album.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = play_queue.get_next_track()
                log_playback_start(speech)

                return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlaySearch(AbstractRequestHandler):
    """Handle NaviSonicPlaySearch

    Free-text play: resolves the query against songs, artists, genres,
    then albums, in that priority order, and starts playback immediately.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlaySearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlaySearch')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

        query = get_slot_value_v2(handler_input, 'query')
        song_id_list, description = resolve_search(query)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find {query.value} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        play_queue.clear()

        if len(song_id_list) > 1:
            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()
        else:
            controller.enqueue_songs(connection, play_queue, [song_id_list[0]])

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = play_queue.get_next_track()
        log_playback_start(speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicQueueSearch(AbstractRequestHandler):
    """Handle NaviSonicQueueSearch

    Free-text queue: resolves the query the same way as NaviSonicPlaySearch,
    but adds to the existing queue rather than replacing it, and doesn't
    start playback.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicQueueSearch')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicQueueSearch')

        query = get_slot_value_v2(handler_input, 'query')
        song_id_list, description = resolve_search(query)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find {query.value} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        controller.enqueue_songs(connection, play_queue, song_id_list)
        play_queue.sync()

        speech = sanitise_speech_output(f'Added {description} to the queue')
        handler_input.response_builder.speak(speech)
        return handler_input.response_builder.response


class NaviSonicPlayPlaylist(AbstractRequestHandler):
    """Handle NaviSonicPlayPlaylist

    Play the given playlist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayPlaylist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayPlaylist')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

        playlist = get_slot_value_v2(handler_input, 'playlist')
        playlist_id = connection.search_playlist(playlist.value)

        if playlist_id is None:
            text = sanitise_speech_output("I couldn't find the playlist " + str(playlist.value) + ' in the collection.')
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        else:
            song_id_list = connection.build_song_list_from_playlist(playlist_id)
            play_queue.clear()

            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()

            speech = sanitise_speech_output('Playing playlist ' + str(playlist.value))
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicByGenre(AbstractRequestHandler):
    """Play songs from the given genre

    50 tracks from the given genre are shuffled and played
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByGenre')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayMusicByGenre')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

        genre = get_slot_value_v2(handler_input, 'genre')
        song_id_list = connection.build_song_list_from_genre(genre.value, min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find any {genre.value} songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()

            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()

            speech = sanitise_speech_output(f'Playing {genre.value} music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicRandom(AbstractRequestHandler):
    """Handle the NaviSonicPlayMusicRandom intent

    Play a random selection of music.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicRandom')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayMusicRandom')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

        song_id_list = connection.build_random_song_list(min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output("I couldn't find any songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()

            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()

            speech = sanitise_speech_output('Playing random music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayFavouriteSongs(AbstractRequestHandler):
    """Handle the NaviSonicPlayFavouriteSongs intent

    Play all starred / liked songs, songs are automatically shuffled.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayFavouriteSongs')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        global backgroundProcess
        logger.debug('In NaviSonicPlayFavouriteSongs')

        if backgroundProcess is not None:
            backgroundProcess.terminate()
            backgroundProcess.join()

        song_id_list = connection.build_song_list_from_favourites()

        if song_id_list is None:
            text = sanitise_speech_output("You don't have any favourite songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            play_queue.clear()

            controller.enqueue_songs(connection, play_queue, [song_id_list[0], song_id_list[1]])
            backgroundProcess = Process(target=queue_worker_thread, args=(connection, play_queue, song_id_list[2:]))
            backgroundProcess.start()

            speech = sanitise_speech_output('Playing your favourite tracks.')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicRandomiseQueue(AbstractRequestHandler):
    """Handle NaviSonicRandomiseQueue Intent

    Shuffle the current play queue
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicRandomiseQueue')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicRandomiseQueue Handler')
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
        current_track = play_queue.get_current_track()
        song_id = current_track.id
        connection.star_entry(song_id, 'song')
        connection.unstar_entry(song_id, 'song')
        return handler_input.response_builder.response

#
# AudioPlayer Handlers
#


class PlaybackStartedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted Directive received."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStarted')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStartedHandler')
        logger.info('Playback started')
        return handler_input.response_builder.response


class PlaybackStoppedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStopped Directive received."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStopped')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStoppedHandler')

        play_queue.set_current_track_offset(handler_input.request_envelope.request.offset_in_milliseconds)

        current_track = play_queue.get_current_track()
        logger.debug(f'Stored track offset of: {current_track.offset} ms for {current_track.title}')
        logger.info('Playback stopped')

        return handler_input.response_builder.response


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackNearlyFinished Directive received.

    If the buffer is empty when Alexa asks for the next track, refill
    the queue with songs similar to the current one rather than letting
    playback error out.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackNearlyFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackNearlyFinishedHandler')

        if len(play_queue.get_buffer()) == 0:
            refill_queue_with_similar(play_queue.get_current_track())

        if len(play_queue.get_buffer()) == 0:
            logger.info('Buffer still empty after refill attempt - nothing more to queue')
            return handler_input.response_builder.response

        logger.info('Queuing next track...')
        track_details = play_queue.enqueue_next_track()
        return controller.start_playback('continue', None, None, track_details, handler_input)


class PlaybackFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFinished Directive received.

    If the queue is empty when a track finishes, refill it with similar
    songs before advancing, rather than crashing on an empty deque.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFinishedHandler')

        timestamp_ms = datetime.now().timestamp()
        current_track = play_queue.get_current_track()
        connection.scrobble(current_track.id, timestamp_ms)

        if play_queue.get_queue_count() == 0:
            refill_queue_with_similar(current_track)

        if play_queue.get_queue_count() > 0:
            play_queue.get_next_track()
        else:
            logger.info('Queue and refill both empty - nothing left to play')

        return handler_input.response_builder.response


class PausePlaybackHandler(AbstractRequestHandler):
    """Handler for stopping audio."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.StopIntent')(handler_input) or
                is_intent_name('AMAZON.CancelIntent')(handler_input) or
                is_intent_name('AMAZON.PauseIntent')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PausePlaybackHandler')
        play_queue.sync()
        return controller.stop(handler_input)


class ResumePlaybackHandler(AbstractRequestHandler):
    """Handler for resuming audio on different events."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.ResumeIntent')(handler_input) or
                is_intent_name('PlayAudio')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In ResumePlaybackHandler')

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

    If the queue is empty when "next" is requested, refill it with
    songs similar to the current one. If that also fails, respond
    gracefully rather than crashing - speaking only for the voice-intent
    case, since PlaybackController.NextCommandIssued (a hardware/touch
    button event) cannot carry outputSpeech.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.NextIntent')(handler_input) or
                is_request_type('PlaybackController.NextCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NextPlaybackHandler')
        logger.debug(f'Queue count before next: {play_queue.get_queue_count()}')

        if play_queue.get_queue_count() == 0:
            refill_queue_with_similar(play_queue.get_current_track())

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after refill attempt')
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
    """AudioPlayer.PlaybackFailed Directive received."""

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFailed')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFailedHandler')

        current_track = play_queue.get_current_track()
        song_id = current_track.id

        logger.error(f'Playback Failed: {handler_input.request_envelope.request.error}')
        logger.error(f'Failed playing track with ID: {song_id}')

        if play_queue.get_queue_count() == 0:
            refill_queue_with_similar(current_track)

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after a playback failure and refill attempt')
            return handler_input.response_builder.response

        track_details = play_queue.get_next_track()
        track_details.offset = 0

        return controller.start_playback('play', None, None, track_details, handler_input)


#
# Exception Handers
#


def _request_forbids_speech(handler_input: HandlerInput) -> bool:
    """Certain request types (AudioPlayer directives, PlaybackController
    button events, System.ExceptionEncountered) must not receive a
    response containing outputSpeech, reprompt, or shouldEndSession -
    Alexa rejects such responses outright."""
    req_type = get_request_type(handler_input)
    return (req_type.startswith('AudioPlayer.') or
            req_type.startswith('PlaybackController.') or
            req_type == 'System.ExceptionEncountered')


class SystemExceptionHandler(AbstractExceptionHandler):
    """Handle System.ExceptionEncountered"""

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return is_request_type('System.ExceptionEncountered')(handler_input)

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In SystemExceptionHandler')

        logger.error(f'System Exception: {exception}')
        logger.error(traceback.format_exc())
        logger.error(f'Request Type Was: {get_request_type(handler_input)}')
        error = handler_input.request_envelope.request.to_dict()
        logger.error(f"Details: {error.get('error').get('message')}")

        if _request_forbids_speech(handler_input):
            logger.warning('Exception occurred on a directive-only request type - returning empty response without speech')
            return handler_input.response_builder.response

        slot_summary = get_slot_summary(handler_input)

        if slot_summary:
            logger.error(f'Slots received: {slot_summary}')
            text = sanitise_speech_output(f"I couldn't find {slot_summary} in your library.")
        else:
            text = sanitise_speech_output("Sorry, I didn't get that. Can you please say it again!!")

        handler_input.response_builder.speak(text).ask(text)

        return handler_input.response_builder.response


class GeneralExceptionHandler(AbstractExceptionHandler):
    """Handle general exceptions"""

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return True

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In GeneralExceptionHandler')

        logger.error(f'General Exception: {exception}')
        logger.error(traceback.format_exc())
        logger.error(f'Request Type Was: {get_request_type(handler_input)}')

        if _request_forbids_speech(handler_input):
            logger.warning('Exception occurred on a directive-only request type - returning empty response without speech')
            return handler_input.response_builder.response

        slot_summary = get_slot_summary(handler_input)

        if slot_summary:
            logger.error(f'Slots received: {slot_summary}')
            text = sanitise_speech_output(f"I couldn't find {slot_summary} in your library.")
        else:
            text = sanitise_speech_output("Sorry, I didn't get that. Can you please say it again!!")

        handler_input.response_builder.speak(text).ask(text)

        return handler_input.response_builder.response


#
# Request Interceptors
#


class LoggingRequestInterceptor(AbstractRequestInterceptor):
    """Intercept all requests"""

    def process(self, handler_input: HandlerInput):
        logger.debug(f'Request received: {handler_input.request_envelope.request}')


class LoggingResponseInterceptor(AbstractResponseInterceptor):
    """Intercept all responses"""

    def process(self, handler_input: HandlerInput, response: Response):
        logger.debug(f'Response sent: {response}')

#
# Functions
#

def get_slot_summary(handler_input: HandlerInput) -> str:
    """Extract any filled slot values from the current request, for
    diagnostic speech output when an exception occurs."""

    if get_request_type(handler_input) != 'IntentRequest':
        return ''

    intent_name = get_intent_name(handler_input)
    logger.error(f'Intent Name Was: {intent_name}')

    slots = handler_input.request_envelope.request.intent.slots
    if not slots:
        return ''

    filled = [slot.value for slot in slots.values() if slot and slot.value]
    return ', '.join(filled)


def get_resolved_slot_value(slot):
    """Get the canonical (synonym-resolved) value for a custom-type slot,
    falling back to the raw spoken value if no resolution exists."""
    try:
        resolutions = slot.resolutions.resolutions_per_authority
        if resolutions:
            for resolution in resolutions:
                if resolution.values:
                    return resolution.values[0].value.name
    except AttributeError:
        pass
    return slot.value


def genre_matches(query, genre_name):
    """Whole-word match against a genre name, avoiding false positives
    like 'dance' incorrectly matching 'dancehall'."""
    tokens = re.split(r'[\s\-&]+', genre_name.lower())
    return query.lower() in tokens


def parse_query_for_artist(text: str):
    """Split 'song by artist' style phrasing out of a free-text query.

    Strips optional filler words ("the song", "the band", "the artist")
    so both terse ("X by Y") and wordier ("the song X by the artist Y")
    phrasings resolve to clean search terms. Returns (song_text, artist_text)
    if a ' by ' pattern is found, otherwise (text, None).
    """

    match = re.search(
        r'^(?:the\s+song\s+)?(.*?)\s+by\s+(?:the\s+(?:band|artist)\s+)?(.*)$',
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text, None


def _search_variants(term: str):
    """Generate normalized variants of a search term to work around
    common tagging punctuation mismatches (e.g. 'and' vs '&')."""

    variants = [term]

    if re.search(r'\band\b', term, re.IGNORECASE):
        variants.append(re.sub(r'\band\b', '&', term, flags=re.IGNORECASE))
    if '&' in term:
        variants.append(term.replace('&', 'and'))

    no_punct = re.sub(r"[,\.]", '', term)
    if no_punct != term:
        variants.append(no_punct)

    return variants


def correct_artist_spelling(term: str, threshold: float = ARTIST_SPELLING_CORRECTION_THRESHOLD):
    """Find the closest known artist name to term using local fuzzy
    matching, for cases where speech-to-text produces a plausible but
    incorrectly-spelled name (e.g. spoken "gipsy kings" vs the tagged
    "Gypsy Kings"). Returns the corrected name, or None if nothing
    scores above threshold."""

    if not known_artists:
        return None

    best_match = None
    best_score = 0.0

    for name in known_artists:
        score = SequenceMatcher(None, term.lower(), name.lower()).ratio()
        if score > best_score:
            best_score = score
            best_match = name

    if best_score >= threshold:
        logger.debug(f'correct_artist_spelling: "{term}" -> "{best_match}" (score {best_score:.2f})')
        return best_match
    return None


def fuzzy_search_song(term: str):
    """Try search_song() against normalized variants of term until one
    returns a result. Returns None if nothing matches any variant."""
    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_song: trying variant "{variant}"')
        result = connection.search_song(variant, SEARCH_RESULT_COUNT)
        logger.debug(f'fuzzy_search_song: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_song("{term}"): {len(result)} results (via variant "{variant}")')
            return result
    logger.info(f'fuzzy_search_song("{term}"): no results')
    return None


def fuzzy_search_artist(term: str):
    """Try search_artist() against normalized variants, then local fuzzy
    spelling correction, then finally fall back to inspecting song search
    results directly - this catches artists who exist as plain-text
    metadata on tracks but were never indexed as a standalone artist
    entity."""

    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_artist: trying variant "{variant}"')
        result = connection.search_artist(variant)
        logger.debug(f'fuzzy_search_artist: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_artist("{term}"): {len(result)} results (via variant "{variant}") - top match: {result[0].get("name")!r} (id={result[0].get("id")})')
            return result

    corrected = correct_artist_spelling(term)
    if corrected and corrected.lower() != term.lower():
        logger.debug(f'fuzzy_search_artist: no direct match for "{term}", trying spelling-corrected "{corrected}"')
        result = connection.search_artist(corrected)
        if result:
            logger.info(f'fuzzy_search_artist("{term}"): {len(result)} results via spelling correction to "{corrected}"')
            return result

    logger.debug(f'fuzzy_search_artist: no indexed match for "{term}", trying song-metadata fallback')
    song_results = connection.search_song(term, SEARCH_RESULT_COUNT)
    if song_results:
        for song in song_results:
            song_artist = song.get('artist', '')
            if _match_score(term, song_artist) >= 3.0 and song.get('artistId'):
                logger.info(f'fuzzy_search_artist("{term}"): matched via song-metadata fallback - "{song_artist}" (id={song.get("artistId")}) via track "{song.get("title")}"')
                return [{'id': song.get('artistId'), 'name': song_artist}]

    logger.info(f'fuzzy_search_artist("{term}"): no results via any method')
    return None


def fuzzy_search_album(term: str):
    """Try search_album() against normalized variants of term until one
    returns a result. Returns None if nothing matches any variant."""
    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_album: trying variant "{variant}"')
        result = connection.search_album(variant)
        logger.debug(f'fuzzy_search_album: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_album("{term}"): {len(result)} results (via variant "{variant}")')
            return result
    logger.info(f'fuzzy_search_album("{term}"): no results')
    return None


def _match_score(query: str, candidate: str) -> float:
    """Score how well candidate matches query, for ranking search results
    client-side rather than trusting whatever order the server returned
    them in."""

    q = query.lower().strip()
    c = candidate.lower().strip()

    if q == c:
        return 4.0
    if c.startswith(q):
        return 3.0
    if q in c:
        return 2.0
    return SequenceMatcher(None, q, c).ratio()


def best_song_match(query: str, songs: list) -> list:
    """Re-order a list of song dicts so the best match to query is first."""
    return sorted(songs, key=lambda s: _match_score(query, s.get('title', '')), reverse=True)


def filter_to_best_matches(query: str, songs: list, threshold: float = 0.15) -> list:
    """Keep only songs whose match score is close to the best match's score,
    discarding loosely-related results."""

    scored = [(s, _match_score(query, s.get('title', ''))) for s in songs]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    top_score = scored[0][1]
    return [s for s, score in scored if top_score - score <= threshold]


def best_album_match(query: str, albums: list):
    """Pick the single best-matching album dict from a list of candidates,
    rather than combining every loosely-matched album's songs together."""
    return max(albums, key=lambda a: _match_score(query, a.get('name', '')))


def log_playback_start(speech: str) -> None:
    """Log what's about to play, plus the next 10 queued tracks, whenever
    a playback response is sent to Alexa."""

    current = play_queue.get_current_track()
    if current:
        logger.info(f'Playing track: {current.title} by: {current.artist} (album: {current.album})')

    upcoming = list(play_queue.get_current_queue())[:10]
    if upcoming:
        logger.info('Next in queue:')
        for i, track in enumerate(upcoming, 1):
            logger.info(f'  {i}. {track.title} by: {track.artist} (album: {track.album})')
    else:
        logger.info('Queue is empty after this track.')


def refill_queue_with_similar(current_track) -> bool:
    """When the queue runs dry, keep playback going by queueing songs
    similar to whatever was last playing, rather than erroring out.

    :return: True if similar songs were found and queued, False otherwise
    :rtype: bool
    """

    if not current_track or not current_track.id:
        return False

    similar_ids = connection.get_similar_songs(current_track.id, SEARCH_RESULT_COUNT)
    similar_ids = [s for s in similar_ids if s != current_track.id]

    if not similar_ids:
        return False

    controller.enqueue_songs(connection, play_queue, similar_ids)
    play_queue.sync()
    logger.info(f'Queue was empty - added {len(similar_ids)} similar songs to continue playback')
    return True


def resolve_search(query):
    """
    Resolve a free-text query against songs, then artists, then genre,
    then albums, in that priority order. Every step logs at INFO level
    what was searched and how many results came back, so search
    behaviour is diagnosable directly from the log without needing
    NAVI_DEBUG turned up.

    The artist-scoped song match (when "X by Y" phrasing is used) now
    requires the same MIN_ACCEPTABLE_SCORE threshold as every other
    step - previously it accepted ANY non-empty result regardless of
    match quality, which meant a query like "greatest hits" (not a real
    song title) could coincidentally match a handful of loosely-fuzzy-matched
    tracks that happened to be by the right artist, and win outright
    before the album step ever got a chance to run.

    :return: (song_id_list, description) or (None, None) if nothing found
    """

    song_text, artist_text = parse_query_for_artist(query.value)
    search_text = song_text if artist_text else query.value

    logger.info(f'resolve_search: query="{query.value}" -> search_text="{search_text}" artist_text={artist_text!r}')

    if artist_text:
        artist_results = fuzzy_search_artist(artist_text)
        logger.info(f'resolve_search: artist search for "{artist_text}" -> {len(artist_results) if artist_results else 0} matches')

        if artist_results:
            artist_id = artist_results[0].get('id')
            logger.info(f'resolve_search: using artist id={artist_id} name={artist_results[0].get("name")!r}')

            song_results = fuzzy_search_song(search_text)
            logger.info(f'resolve_search: song search for "{search_text}" -> {len(song_results) if song_results else 0} matches (pre artist-filter)')

            song_dets = [item for item in song_results if item.get('artistId') == artist_id] if song_results else []
            logger.info(f'resolve_search: {len(song_dets)} of those songs belong to artist id={artist_id}')

            if song_dets:
                song_dets = best_song_match(search_text, song_dets)
                top_score = _match_score(search_text, song_dets[0].get('title', ''))
                logger.info(f'resolve_search: artist-scoped SONG step best match "{song_dets[0].get("title")}" scored {top_score:.2f} against "{search_text}"')

                if top_score >= MIN_ACCEPTABLE_SCORE:
                    logger.info(f'resolve_search: matched via artist-scoped SONG step - "{song_dets[0].get("title")}"')
                    return [item.get('id') for item in song_dets], f'{search_text} by {artist_text}'
                else:
                    logger.info(f'resolve_search: artist-scoped SONG step score too weak ({top_score:.2f} < {MIN_ACCEPTABLE_SCORE}) - trying album instead')

            artist_album_lookup = connection.albums_by_artist(artist_id)
            logger.info(f'resolve_search: artist has {len(artist_album_lookup) if artist_album_lookup else 0} albums in library')

            if artist_album_lookup:
                candidate = best_album_match(search_text, artist_album_lookup)
                score = _match_score(search_text, candidate.get('name', ''))
                logger.info(
                    f'resolve_search: best album candidate "{candidate.get("name")}" '
                    f'(id={candidate.get("id")}, songCount={candidate.get("songCount")}) '
                    f'scored {score:.2f} against "{search_text}"'
                )

                if score >= MIN_ACCEPTABLE_SCORE:
                    song_id_list = connection.build_song_list_from_albums([candidate], -1)
                    logger.info(f'resolve_search: build_song_list_from_albums returned {len(song_id_list)} song IDs for "{candidate.get("name")}"')
                    return song_id_list, f'the album {candidate.get("name")} by {artist_text}'
        else:
            logger.info(f'resolve_search: no artist match for "{artist_text}" - falling through to generic search')
        # falls through to the generic search below, using search_text

    # 1. Song title
    song_results = fuzzy_search_song(search_text)
    logger.info(f'resolve_search: SONG step for "{search_text}" -> {len(song_results) if song_results else 0} matches')
    if song_results:
        song_results = best_song_match(search_text, song_results)
        top_score = _match_score(search_text, song_results[0].get('title', ''))
        logger.info(f'resolve_search: SONG step best match "{song_results[0].get("title")}" scored {top_score:.2f}')

        if top_score >= MIN_ACCEPTABLE_SCORE:
            song_results = filter_to_best_matches(search_text, song_results)
            logger.info(f'resolve_search: SONG step accepted - {len(song_results)} tracks after filtering to best matches')
            return [item.get('id') for item in song_results], search_text

    # 2. Artist name
    artist_results = fuzzy_search_artist(search_text)
    logger.info(f'resolve_search: ARTIST step for "{search_text}" -> {len(artist_results) if artist_results else 0} matches')
    if artist_results:
        artist_album_lookup = connection.albums_by_artist(artist_results[0].get('id'))
        song_id_list = connection.build_song_list_from_albums(artist_album_lookup, SEARCH_RESULT_COUNT)
        logger.info(f'resolve_search: ARTIST step matched "{artist_results[0].get("name")}" - {len(song_id_list)} songs pulled')
        random.shuffle(song_id_list)
        return song_id_list[:SEARCH_RESULT_COUNT], f'music by {search_text}'

    # 3. Genre
    matched_genres = [g for g in known_genres if genre_matches(search_text, g)]
    logger.info(f'resolve_search: GENRE step for "{search_text}" -> matched genres: {matched_genres}')
    if matched_genres:
        all_songs = []
        for g in matched_genres:
            all_songs.extend(connection.build_song_list_from_genre(g, min_song_count))
        logger.info(f'resolve_search: GENRE step - {len(all_songs)} songs pulled across {len(matched_genres)} matched genres')
        if all_songs:
            random.shuffle(all_songs)
            return all_songs[:SEARCH_RESULT_COUNT], f'{search_text} music'

    # 4. Album title - last resort
    album_results = fuzzy_search_album(search_text)
    logger.info(f'resolve_search: ALBUM step for "{search_text}" -> {len(album_results) if album_results else 0} matches')
    if album_results:
        best_album = best_album_match(search_text, album_results)
        logger.info(
            f'resolve_search: ALBUM step best match "{best_album.get("name")}" '
            f'(id={best_album.get("id")}, songCount={best_album.get("songCount")})'
        )
        song_id_list = connection.build_song_list_from_albums([best_album], -1)
        logger.info(f'resolve_search: ALBUM step build_song_list_from_albums returned {len(song_id_list)} song IDs')

        if len(song_id_list) == 1:
            song_id = song_id_list[0]
            song_details = connection.get_song_details(song_id).get('song', {})
            song_title = song_details.get('title', search_text)
            song_artist = song_details.get('artist', 'an unknown artist')
            similar_ids = connection.get_similar_songs(song_id, min_song_count)
            combined = [song_id] + [s for s in similar_ids if s != song_id]

            if len(combined) > 1:
                return combined, f'{song_title} by {song_artist} and similar songs'
            return combined, f'{song_title} by {song_artist}'

        found_artist = best_album.get('artist', 'an unknown artist')
        return song_id_list, f'the album {search_text} by {found_artist}'

    logger.info(f'resolve_search: no match found for "{query.value}" at any step')
    return None, None


def sanitise_speech_output(speech_string: str) -> str:
    """Sanitise speech output inline with the SSML standard"""

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


def queue_worker_thread(connection: object, play_queue: object, song_id_list: list) -> None:
    """Media queue worker"""

    logger.debug('In playlist processing thread!')
    controller.enqueue_songs(connection, play_queue, song_id_list)
    play_queue.sync()
    logger.debug('Finished playlist processing!')


# Register Intent Handlers
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(CheckAudioInterfaceHandler())
sb.add_request_handler(SkillEventHandler())
sb.add_request_handler(HelpHandler())
sb.add_request_handler(FallbackHandler())
sb.add_request_handler(NaviSonicPlayMusicByArtist())
sb.add_request_handler(NaviSonicShuffleArtist())
sb.add_request_handler(NaviSonicPlaySimilar())
sb.add_request_handler(NaviSonicPlayAlbumByArtist())
sb.add_request_handler(NaviSonicPlayPlaylist())
sb.add_request_handler(NaviSonicPlayFavouriteSongs())
sb.add_request_handler(NaviSonicPlayMusicByDecade())
sb.add_request_handler(NaviSonicPlayMusicByGenre())
sb.add_request_handler(NaviSonicPlayMusicRandom())
sb.add_request_handler(NaviSonicRandomiseQueue())
sb.add_request_handler(NaviSonicSongDetails())
sb.add_request_handler(NaviSonicStarSong())
sb.add_request_handler(NaviSonicUnstarSong())
sb.add_request_handler(NaviSonicPlaySearch())
sb.add_request_handler(NaviSonicQueueSearch())

# Register AutoPlayer Handlers
sb.add_request_handler(PlaybackStartedHandler())
sb.add_request_handler(PlaybackStoppedHandler())
sb.add_request_handler(PlaybackNearlyFinishedHandler())
sb.add_request_handler(PlaybackFinishedHandler())
sb.add_request_handler(PausePlaybackHandler())
sb.add_request_handler(NextPlaybackHandler())
sb.add_request_handler(PreviousPlaybackHandler())
sb.add_request_handler(ResumePlaybackHandler())
sb.add_request_handler(PlaybackFailedEventHandler())


# Register Exception Handlers
sb.add_exception_handler(SystemExceptionHandler())
sb.add_exception_handler(GeneralExceptionHandler())

if navidrome_log_level >= 2:
    sb.add_global_request_interceptor(LoggingRequestInterceptor())
    sb.add_global_response_interceptor(LoggingResponseInterceptor())

sa = SkillAdapter(skill=sb.create(), skill_id='test', app=app)
sa.register(app=app, route='/')

if navidrome_log_level == 3:
    logger.warning('AskNavidrome debugging has been enabled, this should only be used when testing!')
    logger.warning('The /buffer, /queue and /history http endpoints are available publicly!')

    @app.route('/queue')
    def view_queue():
        current_track = play_queue.get_current_track()
        return render_template('table.html', title='AskNavidrome - Queued Tracks',
                               tracks=play_queue.get_current_queue(), current=current_track)

    @app.route('/history')
    def view_history():
        current_track = play_queue.get_current_track()
        return render_template('table.html', title='AskNavidrome - Track History',
                               tracks=play_queue.get_history(), current=current_track)

    @app.route('/buffer')
    def view_buffer():
        current_track = play_queue.get_current_track()
        return render_template('table.html', title='AskNavidrome - Buffered Tracks',
                               tracks=play_queue.get_buffer(), current=current_track)


if __name__ == '__main__':
    app.run(host='0.0.0.0')