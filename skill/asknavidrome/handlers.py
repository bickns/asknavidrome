"""
Request handlers for the AskNavidrome Alexa skill.

Every AbstractRequestHandler subclass the skill registers lives here -
both the intent handlers (playing music, searching, favouriting) and
the AudioPlayer directive handlers (playback lifecycle events, next/
previous, pause/resume). Handler-support utility functions used only
by this file (speech sanitisation, playback logging, the background
queue-fill worker) live here too, rather than in a separate utils
module, since nothing outside handlers.py needs them.

Shared state (the play queue, the background-fill process) lives in
state.py; connection, logger, and config constants live in config.py;
free-text search resolution lives in search.py. This file imports from
all three rather than owning any of that itself.
"""

from multiprocessing import Process
import random

from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.utils import is_request_type, is_intent_name, get_slot_value_v2
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
)


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


def log_playback_start(speech: str) -> None:
    """Log what's about to play, plus the next 10 queued tracks, whenever
    a playback response is sent to Alexa - for visibility into queue
    behaviour that the Test-tab simulator doesn't show.

    :param str speech: The speech text being sent (unused directly, kept
        for call-site symmetry / possible future use)
    :return: None
    """

    current = state.play_queue.get_current_track()
    if current:
        logger.info(f'Playing track: {current.title} by: {current.artist} (album: {current.album})')

    upcoming = list(state.play_queue.get_current_queue())[:10]
    if upcoming:
        logger.info('Next in queue:')
        for i, track in enumerate(upcoming, 1):
            logger.info(f'  {i}. {track.title} by: {track.artist} (album: {track.album})')
    else:
        logger.info('Queue is empty after this track.')


def refill_queue_with_similar(current_track) -> bool:
    """When the queue runs dry, keep playback going by queueing songs
    similar to whatever was last playing, rather than erroring out.

    :param current_track: The Track that just finished/is ending
    :return: True if similar songs were found and queued, False otherwise
    :rtype: bool
    """

    if not current_track or not current_track.id:
        return False

    similar_ids = connection.get_similar_songs(current_track.id, 100)
    similar_ids = [s for s in similar_ids if s != current_track.id]

    if not similar_ids:
        return False

    controller.enqueue_songs(connection, state.play_queue, similar_ids)
    state.play_queue.sync()
    logger.info(f'Queue was empty - added {len(similar_ids)} similar songs to continue playback')
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


def _start_background_fill(song_id_list: list) -> None:
    """Enqueue the first two tracks synchronously (fast enough to stay
    within Alexa's 8-second response window), then spawn a background
    process to enqueue the rest. Replaces any previously-running
    background fill via state.stop_background_process().

    :param list song_id_list: Full ordered list of song IDs to queue
    :return: None
    """
    controller.enqueue_songs(connection, state.play_queue, [song_id_list[0], song_id_list[1]])
    bg = Process(target=queue_worker_thread, args=(connection, state.play_queue, song_id_list[2:]))
    bg.start()
    state.set_background_process(bg)


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

    Play a selection of songs for the given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByArtist')
        state.stop_background_process()

        artist = get_slot_value_v2(handler_input, 'artist')
        artist_lookup = fuzzy_search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
            song_id_list = connection.build_song_list_from_albums(artist_album_lookup, min_song_count)
            state.play_queue.clear()

            _start_background_fill(song_id_list)

            speech = sanitise_speech_output(f'Playing music by: {artist.value}')
            logger.info(speech)

            card = {'title': 'AskNavidrome', 'text': speech}

            state.play_queue.shuffle()
            track_details = state.play_queue.get_next_track()
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
        logger.debug('In NaviSonicShuffleArtist')
        state.stop_background_process()

        artist = get_slot_value_v2(handler_input, 'artist')
        artist_lookup = fuzzy_search_artist(artist.value)

        if artist_lookup is None:
            text = sanitise_speech_output(f"I couldn't find the artist {artist.value} in the collection.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
        song_id_list = connection.build_song_list_from_albums(artist_album_lookup, -1)  # unlimited - full catalogue
        random.shuffle(song_id_list)

        state.play_queue.clear()
        _start_background_fill(song_id_list)

        speech = sanitise_speech_output(f'Shuffling all songs by {artist.value}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = state.play_queue.get_next_track()
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
        logger.debug('In NaviSonicPlaySimilar')
        state.stop_background_process()

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

        state.play_queue.clear()

        if len(combined) > 1:
            _start_background_fill(combined)
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist} and similar songs')
        else:
            controller.enqueue_songs(connection, state.play_queue, [combined[0]])
            speech = sanitise_speech_output(f'Playing {song_title} by {song_artist}')

        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = state.play_queue.get_next_track()
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
        logger.debug('In NaviSonicPlayMusicByDecade')
        state.stop_background_process()

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

        state.play_queue.clear()
        _start_background_fill(song_id_list)

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = state.play_queue.get_next_track()
        log_playback_start(speech)

        return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayAlbumByArtist(AbstractRequestHandler):
    """Handle NaviSonicPlayAlbumByArtist

    Play a given album by a given artist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayAlbumByArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayAlbumByArtist')
        state.stop_background_process()

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
                state.play_queue.clear()
                _start_background_fill(song_id_list)

                speech = sanitise_speech_output(f'Playing {album.value} by: {artist.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = state.play_queue.get_next_track()
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
                state.play_queue.clear()
                _start_background_fill(song_id_list)

                speech = sanitise_speech_output(f'Playing {album.value}')
                logger.info(speech)
                card = {'title': 'AskNavidrome', 'text': speech}
                track_details = state.play_queue.get_next_track()
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
        logger.debug('In NaviSonicPlaySearch')
        state.stop_background_process()

        query = get_slot_value_v2(handler_input, 'query')
        song_id_list, description = resolve_search(query)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find {query.value} in your library.")
            handler_input.response_builder.speak(text).ask(text)
            return handler_input.response_builder.response

        state.play_queue.clear()

        if len(song_id_list) > 1:
            _start_background_fill(song_id_list)
        else:
            controller.enqueue_songs(connection, state.play_queue, [song_id_list[0]])

        speech = sanitise_speech_output(f'Playing {description}')
        card = {'title': 'AskNavidrome', 'text': speech}
        track_details = state.play_queue.get_next_track()
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

        controller.enqueue_songs(connection, state.play_queue, song_id_list)
        state.play_queue.sync()

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
        logger.debug('In NaviSonicPlayPlaylist')
        state.stop_background_process()

        playlist = get_slot_value_v2(handler_input, 'playlist')
        playlist_id = connection.search_playlist(playlist.value)

        if playlist_id is None:
            text = sanitise_speech_output("I couldn't find the playlist " + str(playlist.value) + ' in the collection.')
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            song_id_list = connection.build_song_list_from_playlist(playlist_id)
            state.play_queue.clear()
            _start_background_fill(song_id_list)

            speech = sanitise_speech_output('Playing playlist ' + str(playlist.value))
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = state.play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicByGenre(AbstractRequestHandler):
    """Play songs from the given genre

    50 tracks from the given genre are shuffled and played
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByGenre')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByGenre')
        state.stop_background_process()

        genre = get_slot_value_v2(handler_input, 'genre')
        song_id_list = connection.build_song_list_from_genre(genre.value, min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output(f"I couldn't find any {genre.value} songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            state.play_queue.clear()
            _start_background_fill(song_id_list)

            speech = sanitise_speech_output(f'Playing {genre.value} music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = state.play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayMusicRandom(AbstractRequestHandler):
    """Handle the NaviSonicPlayMusicRandom intent

    Play a random selection of music.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicRandom')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicRandom')
        state.stop_background_process()

        song_id_list = connection.build_random_song_list(min_song_count)

        if song_id_list is None:
            text = sanitise_speech_output("I couldn't find any songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            state.play_queue.clear()
            _start_background_fill(song_id_list)

            speech = sanitise_speech_output('Playing random music')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = state.play_queue.get_next_track()
            log_playback_start(speech)

            return controller.start_playback('play', speech, card, track_details, handler_input)


class NaviSonicPlayFavouriteSongs(AbstractRequestHandler):
    """Handle the NaviSonicPlayFavouriteSongs intent

    Play all starred / liked songs, songs are automatically shuffled.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayFavouriteSongs')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayFavouriteSongs')
        state.stop_background_process()

        song_id_list = connection.build_song_list_from_favourites()

        if song_id_list is None:
            text = sanitise_speech_output("You don't have any favourite songs in the collection.")
            handler_input.response_builder.speak(text).ask(text)

            return handler_input.response_builder.response

        else:
            random.shuffle(song_id_list)
            state.play_queue.clear()
            _start_background_fill(song_id_list)

            speech = sanitise_speech_output('Playing your favourite tracks.')
            logger.info(speech)
            card = {'title': 'AskNavidrome', 'text': speech}
            track_details = state.play_queue.get_next_track()
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

        state.play_queue.shuffle()
        state.play_queue.sync()

        return handler_input.response_builder.response


class NaviSonicSongDetails(AbstractRequestHandler):
    """Handle NaviSonicSongDetails Intent

    Returns information on the track that is currently playing
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicSongDetails')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicSongDetails Handler')

        current_track = state.play_queue.get_current_track()

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

        current_track = state.play_queue.get_current_track()

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

        current_track = state.play_queue.get_current_track()

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

        state.play_queue.set_current_track_offset(handler_input.request_envelope.request.offset_in_milliseconds)

        current_track = state.play_queue.get_current_track()
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

        if len(state.play_queue.get_buffer()) == 0:
            refill_queue_with_similar(state.play_queue.get_current_track())

        if len(state.play_queue.get_buffer()) == 0:
            logger.info('Buffer still empty after refill attempt - nothing more to queue')
            return handler_input.response_builder.response

        logger.info('Queuing next track...')
        track_details = state.play_queue.enqueue_next_track()
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

        from datetime import datetime
        timestamp_ms = datetime.now().timestamp()
        current_track = state.play_queue.get_current_track()
        connection.scrobble(current_track.id, timestamp_ms)

        if state.play_queue.get_queue_count() == 0:
            refill_queue_with_similar(current_track)

        if state.play_queue.get_queue_count() > 0:
            state.play_queue.get_next_track()
        else:
            logger.info('Queue and refill both empty - nothing left to play')

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
        state.play_queue.sync()

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

        current_track = state.play_queue.get_current_track()

        if current_track.offset > 0:
            logger.info('Resuming ' + str(current_track.title))
            logger.info('Offset ' + str(current_track.offset))

            return controller.start_playback('play', None, None, current_track, handler_input)

        elif state.play_queue.get_queue_count() > 0 and current_track.offset == 0:
            logger.info('Resuming - There was no paused track, getting next track from queue')
            track_details = state.play_queue.get_next_track()

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
        logger.debug(f'Queue count before next: {state.play_queue.get_queue_count()}')

        if state.play_queue.get_queue_count() == 0:
            refill_queue_with_similar(state.play_queue.get_current_track())

        if state.play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after refill attempt')
            if is_intent_name('AMAZON.NextIntent')(handler_input):
                text = sanitise_speech_output("There's nothing else queued up.")
                handler_input.response_builder.speak(text)
            return handler_input.response_builder.response

        track_details = state.play_queue.get_next_track()
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

        if state.play_queue.get_history_count() == 0:
            logger.info('No previous track in history')
            if is_intent_name('AMAZON.PreviousIntent')(handler_input):
                text = sanitise_speech_output("There's nothing before this.")
                handler_input.response_builder.speak(text)
            return handler_input.response_builder.response

        track_details = state.play_queue.get_previous_track()
        track_details.offset = 0
        return controller.start_playback('play', None, None, track_details, handler_input)


class PlaybackFailedEventHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFailed Directive received.

    Logging the error and restarting playing with no output speech.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFailed')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFailedHandler')

        current_track = state.play_queue.get_current_track()
        song_id = current_track.id

        logger.error(f'Playback Failed: {handler_input.request_envelope.request.error}')
        logger.error(f'Failed playing track with ID: {song_id}')

        if state.play_queue.get_queue_count() == 0:
            refill_queue_with_similar(current_track)

        if state.play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after a playback failure and refill attempt')
            return handler_input.response_builder.response

        track_details = state.play_queue.get_next_track()
        track_details.offset = 0

        return controller.start_playback('play', None, None, track_details, handler_input)