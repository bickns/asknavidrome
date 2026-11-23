"""
Collection handlers: the intents that play a whole set of tracks
rather than one named song - a playlist, a genre, a decade, random
music, favourites, or everything by one artist.

All of them end the same way (helpers.start_playing()): stop the old
background fill, flush the queue, fill it, and start playback. These
rarely change.
"""

import random

from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.utils import is_intent_name, get_slot_value_v2
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response

from asknavidrome.config import connection, logger, min_song_count, DECADE_RANGES
from asknavidrome.helpers import (
    device_and_queue,
    reply_not_found,
    get_resolved_slot_value,
    start_playing,
)
from asknavidrome.search import fuzzy_search_artist


class NaviSonicShuffleArtist(AbstractRequestHandler):
    """Handle NaviSonicShuffleArtist ("shuffle songs by X")

    Shuffle and play the artist's ENTIRE catalogue.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicShuffleArtist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicShuffleArtist')
        device_id, play_queue = device_and_queue(handler_input)

        artist = get_slot_value_v2(handler_input, 'artist')
        artist_lookup = fuzzy_search_artist(artist.value)

        if artist_lookup is None:
            return reply_not_found(handler_input, f"I couldn't find the artist {artist.value} in the collection.")

        artist_album_lookup = connection.albums_by_artist(artist_lookup[0].get('id'))
        song_id_list = connection.build_song_list_from_albums(artist_album_lookup, -1)  # unlimited - full catalogue
        random.shuffle(song_id_list)

        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Shuffling all songs by {artist.value}')


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
        device_id, play_queue = device_and_queue(handler_input)

        decade = get_slot_value_v2(handler_input, 'decade')
        genre = get_slot_value_v2(handler_input, 'genre')

        decade_value = get_resolved_slot_value(decade)
        year_range = DECADE_RANGES.get(decade_value)

        if year_range is None:
            return reply_not_found(handler_input, f"I don't recognise the decade {decade.value}.")

        from_year, to_year = year_range
        genre_name = genre.value if genre else None
        songs = connection.get_songs_by_year_range(from_year, to_year, genre_name)

        description = f'{genre.value} music from the {decade_value}' if genre else f'music from the {decade_value}'

        if not songs:
            return reply_not_found(handler_input, f"You don't have any {description} in your library.")

        song_id_list = [s.get('id') for s in songs]

        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Playing {description}')


class NaviSonicPlayPlaylist(AbstractRequestHandler):
    """Handle NaviSonicPlayPlaylist

    Play the given playlist
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayPlaylist')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayPlaylist')
        device_id, play_queue = device_and_queue(handler_input)

        playlist = get_slot_value_v2(handler_input, 'playlist')
        playlist_id = connection.search_playlist(playlist.value)

        if playlist_id is None:
            return reply_not_found(handler_input, f"I couldn't find the playlist {playlist.value} in the collection.")

        song_id_list = connection.build_song_list_from_playlist(playlist_id)

        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Playing playlist {playlist.value}')


class NaviSonicPlayMusicByGenre(AbstractRequestHandler):
    """Play songs from the given genre

    50 tracks from the given genre are shuffled and played
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicByGenre')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicByGenre')
        device_id, play_queue = device_and_queue(handler_input)

        genre = get_slot_value_v2(handler_input, 'genre')
        song_id_list = connection.build_song_list_from_genre(genre.value, min_song_count)

        if song_id_list is None:
            return reply_not_found(handler_input, f"I couldn't find any {genre.value} songs in the collection.")

        random.shuffle(song_id_list)

        return start_playing(handler_input, device_id, play_queue, song_id_list, f'Playing {genre.value} music')


class NaviSonicPlayMusicRandom(AbstractRequestHandler):
    """Handle the NaviSonicPlayMusicRandom intent

    Play a random selection of music.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayMusicRandom')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayMusicRandom')
        device_id, play_queue = device_and_queue(handler_input)

        song_id_list = connection.build_random_song_list(min_song_count)

        if song_id_list is None:
            return reply_not_found(handler_input, "I couldn't find any songs in the collection.")

        random.shuffle(song_id_list)

        return start_playing(handler_input, device_id, play_queue, song_id_list, 'Playing random music')


class NaviSonicPlayFavouriteSongs(AbstractRequestHandler):
    """Handle the NaviSonicPlayFavouriteSongs intent

    Play all starred / liked songs, songs are automatically shuffled.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicPlayFavouriteSongs')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicPlayFavouriteSongs')
        device_id, play_queue = device_and_queue(handler_input)

        song_id_list = connection.build_song_list_from_favourites()

        if song_id_list is None:
            return reply_not_found(handler_input, "You don't have any favourite songs in the collection.")

        random.shuffle(song_id_list)

        return start_playing(handler_input, device_id, play_queue, song_id_list, 'Playing your favourite tracks.')