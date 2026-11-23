from hashlib import md5
from typing import Union
import logging
import random
import secrets

import libsonic


class SubsonicConnection:
    """Class with methods to interact with Subsonic API compatible media servers
    """

    def __init__(self, server_url: str, user: str, passwd: str, port: int, api_location: str, api_version: str) -> None:
        """
        :param str server_url: The URL of the Subsonic API compatible media server
        :param str user: Username to authenticate against the API
        :param str passwd: Password to authenticate against the API
        :param int port: Port the Subsonic compatible server is listening on
        :param str api_location: Path to the API, this is appended to server_url
        :param str api_version: The version of the Subsonic API that is in use
        :return: None
        """

        self.logger = logging.getLogger(__name__)

        self.server_url = server_url
        self.user = user
        self.passwd = passwd
        self.port = port
        self.api_location = api_location
        self.api_version = api_version

        self.conn = libsonic.Connection(self.server_url,
                                        self.user,
                                        self.passwd,
                                        self.port,
                                        self.api_location,
                                        'AskNavidrome',
                                        self.api_version,
                                        False)

        self.logger.debug('Connecting to Navidrome.....')

    def ping(self) -> bool:
        """Ping a Subsonic API server

        Verify the connection to a Subsonic compatible API server
        is working.  Enhanced logging has been added to examine the
        HTTP response returned from the server to assist with troubleshooting
        connection issues.

        :return: True if the connection works, False if it does not
        :rtype: bool
        """

        self.logger.debug('In function ping()')
        http_request = self.conn._getRequest('ping.view')
        try:
            http_request_result = self.conn._doInfoReq(http_request)
        except Exception as e:
            self.logger.error('Failed to connect to Navidrome: %s', e, exc_info=True)
            return False

        self.logger.debug('ping() response from server: %s', http_request_result)
        status = http_request_result.get('status')
        if status == 'ok':
            self.logger.info('Successfully connected to Navidrome')
            return True
        elif status == 'failed':
            err = http_request_result.get('error', {})
            self.logger.error('Failed to connect to Navidrome: code=%s msg=%s',
                              err.get('code'), err.get('message'))
            return False
        else:
            self.logger.error('Unexpected error when connecting to Navidrome: %r', status)
            return False

    def scrobble(self, track_id: str, time: int) -> None:
        """Scrobble the given track

        :param str track_id: The ID of the track to scrobble
        :param int time: UNIX timestamp of track play time
        :return: None
        """
        self.logger.debug('In function scrobble()')

        self.conn.scrobble(track_id, True, time)

        return None

    def search_playlist(self, term: str) -> Union[str, None]:
        """Search the media server for the given playlist

        :param str term: The name of the playlist
        :return: The ID of the playlist or None if the playlist is not found
        :rtype: str | None
        """

        self.logger.debug('In function search_playlist()')

        playlist_dict = self.conn.getPlaylists()

        # Navidrome omits the 'playlist' key entirely when there are no playlists
        playlists = playlist_dict.get('playlists', {}).get('playlist', [])

        # Search the list of dictionaries for a playlist with a name that matches the search term
        playlist_id_list = [item.get('id') for item in playlists if (item.get('name') or '').lower() == term.lower()]

        if len(playlist_id_list) == 1:
            # We have matched the playlist return it
            self.logger.debug(f'Found playlist {playlist_id_list[0]}')

            return playlist_id_list[0]

        elif len(playlist_id_list) > 1:
            # More than one result was returned, this should not be possible
            self.logger.error(f'More than one playlist called {term} was found, multiple playlists with the same name are not supported')

            return None

        elif len(playlist_id_list) == 0:
            self.logger.error(f'No playlist matching the name {term} was found!')

            return None

    def search_artist(self, term: str, count: int = 10) -> Union[dict, None]:
        """Search the media server for the given artist

        Uses .get() with defaults throughout when reading the response,
        rather than bracket indexing - Navidrome can omit the 'artist'
        key entirely from searchResult3 when there are zero matches,
        rather than returning an empty list, so bracket indexing would
        raise a KeyError in that case.

        :param str term: The name of the artist
        :param int count: Maximum number of artists to return, defaults to 10
        :return: A dictionary of artists or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_artist()')

        result_dict = self.conn.search3(term, songCount=0, albumCount=0, artistCount=count)

        artists = result_dict.get('searchResult3', {}).get('artist', [])

        self.logger.debug(f'Searching artists for term: {term} found {len(artists)} entries.')

        if len(artists) > 0:
            # Results were found
            return artists

        # No results were found
        return None

    def search_album(self, term: str, count: int = 10) -> Union[dict, None]:
        """Search the media server for the given album

        Uses .get() with defaults throughout when reading the response,
        for the same reason as search_artist() above.

        :param str term: The name of the album
        :param int count: Maximum number of albums to return, defaults to 10
        :return: A dictionary of albums or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_album()')

        result_dict = self.conn.search3(term, songCount=0, albumCount=count, artistCount=0)

        albums = result_dict.get('searchResult3', {}).get('album', [])

        self.logger.debug(f'Searching albums for term: {term} found {len(albums)} entries.')

        if len(albums) > 0:
            # Results were found
            return albums

        # No results were found
        return None

    def search_song(self, term: str, count: int = 50) -> Union[dict, None]:
        """Search the media server for the given song

        Uses .get() with defaults throughout when reading the response,
        for the same reason as search_artist() above.

        :param str term: The name of the song
        :param int count: Maximum number of songs to return, defaults to 50
        :return: A dictionary of songs or None if no results are found
        :rtype: dict | None
        """

        self.logger.debug('In function search_song()')

        result_dict = self.conn.search3(term, songCount=count, albumCount=0, artistCount=0)

        songs = result_dict.get('searchResult3', {}).get('song', [])

        self.logger.debug(f'Searching songs for term: {term}, found {len(songs)} entries.')

        if len(songs) > 0:
            # Results were found
            return songs

        # No results were found
        return None

    def albums_by_artist(self, id: str) -> 'list[dict]':
        """Get the albums for a given artist

        Navidrome omits the 'album' key for an artist with no albums of
        their own, so an empty list is returned in that case (callers must
        handle it) rather than raising.

        :param str id: The artist ID
        :return: A list of albums (may be empty)
        :rtype: list of dict
        """

        self.logger.debug('In function albums_by_artist()')

        result_dict = self.conn.getArtist(id)
        album_list = result_dict.get('artist', {}).get('album', [])

        if not album_list:
            self.logger.warning(f'albums_by_artist: artist {id} has no albums (response keys: {list(result_dict.get("artist", {}).keys())})')
            return []

        # Shuffle the album list to keep generic requests fresh
        random.shuffle(album_list)

        return album_list

    def build_song_list_from_albums(self, albums: 'list[dict]', length: int) -> list:
        """Get a list of songs from given albums

        Build a list of songs from the given albums, keep adding tracks
        until song_count is greater than of equal to length

        Songs within each album are sorted by disc number then track number
        before being added, so playback always starts from track 1 regardless
        of what order the underlying API happens to list them in. Track and
        disc numbers are explicitly cast to int via the local _to_int helper,
        since a string/int type mismatch would either raise a TypeError or
        (if all values happen to be strings) produce a lexicographic rather
        than numeric sort order. Debug-level logging of the raw and sorted
        track data is included per album, to make ordering issues
        diagnosable directly from the log rather than by guessing.

        :param list[dict] albums: A list of dictionaries containing album information
        :param int length: The minimum number of songs that should be returned, if -1 there is no limit
        :return: A list of song IDs
        :rtype: list
        """

        self.logger.debug('In function build_song_list_from_albums()')

        song_id_list = []

        if length != -1:
            song_count = 0
            album_id_list = []

            # The list of songs should be limited by length
            for album in albums:
                if song_count < int(length):
                    # We need more songs
                    album_id_list.append(album.get('id'))
                    song_count = song_count + (album.get('songCount') or 0)
                else:
                    # We have enough songs, stop iterating
                    break
        else:
            # The list of songs should not be limited
            album_id_list = [album.get('id') for album in albums]

        def _to_int(value, default):
            """Cast value to int, falling back to default if it can't be
            interpreted as one (e.g. None, or an empty string)."""
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        # Get a song listing for each album
        for album_id in album_id_list:
            album_details = self.conn.getAlbum(album_id)
            songs = album_details.get('album', {}).get('song', [])

            self.logger.debug(f'build_song_list_from_albums: album {album_id} returned {len(songs)} songs')
            for s in songs:
                self.logger.debug(
                    f'  raw track data: title={s.get("title")!r} '
                    f'track={s.get("track")!r} (type {type(s.get("track")).__name__}) '
                    f'discNumber={s.get("discNumber")!r}'
                )

            # Sort by disc number then track number, so playback always
            # starts from track 1 regardless of the order the API returned
            # the songs in.
            songs = sorted(songs, key=lambda s: (_to_int(s.get('discNumber'), 1), _to_int(s.get('track'), 0)))

            self.logger.debug(f'build_song_list_from_albums: sorted order: {[s.get("title") for s in songs]}')

            for song_detail in songs:
                # Capture the song ID
                song_id_list.append(song_detail.get('id'))

        return song_id_list

    def build_song_list_from_playlist(self, id: str) -> list:
        """Build a list of songs from a given playlist

        :param str id: The playlist ID
        :return: A list of song IDs
        :rtype: list
        """

        self.logger.debug('In function build_song_list_from_playlist()')

        song_id_list = []
        playlist_details = self.conn.getPlaylist(id)

        song_id_list = [song_detail.get('id') for song_detail in playlist_details.get('playlist', {}).get('entry', [])]

        return song_id_list

    def build_song_list_from_favourites(self) -> Union[list, None]:
        """Build a shuffled list favourite songs

        :return: A list of song IDs or None if no favourite tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_song_list_from_favourites()')

        favourite_songs = self.conn.getStarred2().get('starred2', {}).get('song', [])

        if len(favourite_songs) > 0:
            song_id_list = [song.get('id') for song in favourite_songs]

            return song_id_list

        else:
            return None

    def build_song_list_from_genre(self, genre: str, count: int) -> Union[list, None]:
        """Build a shuffled list songs of songs from the given genre.

        :param str genre: The genre, acceptable values are with the getGenres Subsonic API call.
        :param int count: The number of songs to return
        :return: A list of song IDs or None if no tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_song_list_from_genre()')

        # Note the use of title() to capitalise the first letter of each word in the genre
        # without this the genres do not match the strings returned by the API.
        self.logger.debug(f'Searching for {genre.title()} music')
        songs_from_genre = self.conn.getSongsByGenre(genre.title(), count).get('songsByGenre', {}).get('song', [])

        if len(songs_from_genre) > 0:
            song_id_list = [song.get('id') for song in songs_from_genre]

            return song_id_list

        else:
            return None

    def build_random_song_list(self, count: int) -> Union[list, None]:
        """Build a shuffled list of random songs

        :param int count: The number of songs to return
        :return: A list of song IDs or None if no tracks are found.
        :rtype: list | None
        """

        self.logger.debug('In function build_random_song_list()')
        random_songs = self.conn.getRandomSongs(count).get('randomSongs', {}).get('song', [])

        if len(random_songs) > 0:
            song_id_list = [song.get('id') for song in random_songs]

            return song_id_list

        else:
            return None

    def get_genre_list(self) -> list:
        """Get a list of all genre names known to the media server

        :return: A list of genre name strings (may be empty)
        :rtype: list
        """
        self.logger.debug('In function get_genre_list()')

        result = self.conn.getGenres()
        genres = result.get('genres', {}).get('genre', [])

        return [g.get('value') for g in genres]

    def get_artist_list(self) -> list:
        """Get a list of all artist names known to the media server, via
        the indexed artist list.

        Used to build a local cache for fuzzy spelling correction (e.g.
        matching spoken "gipsy kings" against the correctly-tagged
        "Gypsy Kings"). Note that Navidrome only indexes artists who are
        the primary album artist on at least one album of their own -
        featured/guest/compilation-only artists will not appear here even
        though their name exists as plain-text metadata on individual
        tracks.

        :return: A list of artist name strings (may be empty)
        :rtype: list
        """
        self.logger.debug('In function get_artist_list()')

        result = self.conn.getArtists()
        indexes = result.get('artists', {}).get('index', [])

        names = []
        for index in indexes:
            for artist in index.get('artist', []):
                name = artist.get('name')
                if name:
                    names.append(name)

        return names

    def get_similar_songs(self, id: str, count: int) -> list:
        """Get songs similar to the given song, album, or artist ID

        :param str id: The seed ID for the similarity recommendation
        :param int count: Maximum number of similar songs to return
        :return: A list of song IDs (may be empty if none found)
        :rtype: list
        """
        self.logger.debug('In function get_similar_songs()')

        result = self.conn.getSimilarSongs2(id, count)
        similar_songs = result.get('similarSongs2', {}).get('song', [])

        return [song.get('id') for song in similar_songs]

    def get_song_details(self, id: str) -> dict:
        """Get details about a given song ID

        :param str id: A song ID
        :return: A dictionary of details about the given song.
        :rtype: dict
        """

        self.logger.debug('In function get_song_details()')

        song_details = self.conn.getSong(id)

        return song_details

    def get_song_uri(self, id: str) -> str:
        """Create a URI for a given song

        Creates a URI for the song represented by the given ID.  Authentication details are
        embedded in the URI

        :param str id: A song ID
        :return: A properly formatted URI
        :rtype: str
        """

        self.logger.debug('In function get_song_uri()')

        salt = secrets.token_hex(16)
        auth_token = md5(self.passwd.encode() + salt.encode())

        # This creates a multiline f string, uri contains a single line with both
        # f strings.
        uri = (
            f'{self.server_url}:{self.port}{self.api_location}/stream.view?f=json&v={self.api_version}&c=AskNavidrome&u='
            f'{self.user}&s={salt}&t={auth_token.hexdigest()}&id={id}'
        )

        return uri

    def get_songs_by_year_range(self, from_year: int, to_year: int, genre: str = None, count: int = 500) -> list:
        """Get songs released within a year range, optionally filtered by genre

        Operates at the song level, so compilations, "best of" collections,
        and albums where individual tracks' years differ from the release's
        own year tag are all covered correctly.

        :param int from_year: Start year (inclusive)
        :param int to_year: End year (inclusive)
        :param str genre: Optional genre name to filter results by
        :param int count: Maximum number of songs to return (server cap is 500)
        :return: A list of song dictionaries
        :rtype: list
        """
        self.logger.debug('In function get_songs_by_year_range()')

        result = self.conn.getRandomSongs(size=count, genre=genre, fromYear=from_year, toYear=to_year)

        return result.get('randomSongs', {}).get('song', [])

    def star_entry(self, id: str, mode: str) -> None:
        """Add a star to the given entity

        :param str id: The Navidrome ID of the entity.
        :param str mode: The type of entity, must be song, artist or album
        :return: None.
        """

        # Convert id to list
        id_list = [id]

        if mode == 'song':
            self.conn.star(id_list, None, None)

            return None
        elif mode == 'album':
            self.conn.star(None, id_list, None)

            return None
        elif mode == 'artist':
            self.conn.star(None, None, id_list)

            return None

    def unstar_entry(self, id: str, mode: str) -> None:
        """Remove a star from the given entity

        :param str id: The Navidrome ID of the entity.
        :param str mode: The type of entity, must be song, artist or album
        :return: None.
        """

        # Convert id to list
        id_list = [id]

        if mode == 'song':
            self.conn.unstar(id_list, None, None)

            return None
        elif mode == 'album':
            self.conn.unstar(None, id_list, None)

            return None
        elif mode == 'artist':
            self.conn.unstar(None, None, id_list)

            return None