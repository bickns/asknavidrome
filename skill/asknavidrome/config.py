"""
Startup configuration for the AskNavidrome web service.

Handles logging setup, environment-variable validation, the Navidrome
connection, and the startup-time genre/artist caches. This module is
imported first by app.py, before any handler or search logic - other
modules (search.py, handlers.py, exception_handlers.py) import their
shared dependencies (connection, logger, known_genres, etc.) from here
rather than each re-reading the environment or re-establishing their
own connection.
"""

import logging
import os
import sys

import asknavidrome.subsonic_api as api

#
# Tunable constants
#

DECADE_RANGES = {
    '1920s': (1920, 1929), '1960s': (1960, 1969), '1970s': (1970, 1979),
    '1980s': (1980, 1989), '1990s': (1990, 1999), '2000s': (2000, 2009),
    '2010s': (2010, 2019), '2020s': (2020, 2029),
}
"""Maps a decade's canonical slot value to its (from_year, to_year) range."""

DEFAULT_GENRES = ['rock', 'pop', 'dance', 'club', 'classical', 'disco', 'electronic', 'trance', 'house',
                    'metal', 'punk', 'reggae', 'soul', 'techno', 'blues', 'folk', 'indie', 'rap']
"""Small set of common genres used as a fallback if the startup genre
fetch from Navidrome fails, rather than disabling genre matching
entirely."""

SEARCH_RESULT_COUNT = 100
"""Target queue size for free-text artist/genre matches in resolve_search()."""

MIN_ACCEPTABLE_SCORE = 2.0
"""Minimum match score ("contains" match or better) for a fuzzy search
result to be trusted - below this, the match is treated as too weak
and the search falls through to the next resolution step."""

ARTIST_SPELLING_CORRECTION_THRESHOLD = 0.75
"""Minimum SequenceMatcher similarity ratio required to accept a
fuzzy-corrected artist name (e.g. spoken "gipsy kings" corrected to
the tagged "Gypsy Kings")."""


#
# Logging setup
#

logger = logging.getLogger()
"""The root logger used throughout the application."""

logger.setLevel(logging.getLevelName('DEBUG'))

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

_handler = logging.StreamHandler(sys.stdout)
_handler.setLevel(logging.getLevelName('DEBUG'))
_handler.setFormatter(log_formatter)

logger.addHandler(_handler)


#
# Get service configuration
#

logger.info('AskNavidrome 0.10!')
logger.debug('Getting configuration from the environment...')

try:
    if 'NAVI_SKILL_ID' in os.environ:
        # Set skill ID, this is available on the Alexa Developer Console
        # if this is not set the web service will respond to any skill.
        skill_id = os.getenv('NAVI_SKILL_ID')

        logger.info(f'Skill ID set to: {skill_id}')

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

# Set log level based on config value
if 'NAVI_DEBUG' in os.environ:
    navidrome_log_level = int(os.getenv('NAVI_DEBUG'))

    if navidrome_log_level == 0:
        # Warnings and higher
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')

    elif navidrome_log_level == 1:
        # Info messages and higher
        logger.setLevel(logging.INFO)
        logger.info('Log level set to INFO')

    elif navidrome_log_level == 2:
        # Debug with request and response interceptors
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')

    elif navidrome_log_level == 3:
        # Debug with request / response interceptors and Web GUI
        logger.setLevel(logging.DEBUG)
        logger.debug('Log level set to DEBUG')

    else:
        # Invalid value provided - set to WARNING
        navidrome_log_level = 0
        logger.setLevel(logging.WARNING)
        logger.warning('Log level set to WARNING')
else:
    navidrome_log_level = 0

#
# Connect to Navidrome
#

connection = api.SubsonicConnection(navidrome_url,
                                    navidrome_user,
                                    navidrome_passwd,
                                    navidrome_port,
                                    navidrome_api_location,
                                    navidrome_api_version)
"""The shared SubsonicConnection instance used across every handler and
search function."""

try:
    connection.ping()

except:
    raise RuntimeError('Could not connect to SubSonic API!')

#
# Startup-time genre and artist caches
#

try:
    known_genres = {g.lower() for g in connection.get_genre_list()}
    logger.info(f'Loaded {len(known_genres)} genres from Navidrome')
except Exception as e:
    logger.warning(f'Could not load genre list, falling back to defaults: {e}')
    known_genres = set(DEFAULT_GENRES)
"""Lowercased set of every genre name known to Navidrome, cached at
startup so genre matching in search.py doesn't need a live API call
on every search."""

try:
    known_artists = connection.get_artist_list()
    logger.info(f'Loaded {len(known_artists)} artists from Navidrome')
except Exception as e:
    logger.warning(f'Could not load artist list, spelling-correction fallback will be disabled: {e}')
    known_artists = []
"""List of every indexed artist name known to Navidrome, used by
search.py's fuzzy spelling-correction fallback."""


logger.info('AskNavidrome Web Service is ready to start!')