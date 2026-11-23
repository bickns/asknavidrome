"""
Request handlers for the AskNavidrome Alexa skill.

The handlers used to live in this one large file. They are now split
by how often they change:

  helpers.py               shared functions used by every handler
  similar.py               finding the next similar song in the background
  handlers_search.py       play / queue / insert / play-and-similar
  handlers_collections.py  playlist, genre, decade, random, favourites, shuffle artist
  handlers_standard.py     launch, help, controls, AudioPlayer events

This file only re-exports them, so app.py (which does
`from asknavidrome import handlers as h`) and exception_handlers.py
(which imports sanitise_speech_output from here) keep working unchanged.
Nothing is defined here.
"""

from asknavidrome.helpers import (
    sanitise_speech_output,
    log_incoming_request,
)
from asknavidrome.handlers_search import (
    NaviSonicPlaySearch,
    NaviSonicQueueSearch,
    NaviSonicInsertSearch,
    NaviSonicPlaySimilar,
)
from asknavidrome.handlers_collections import (
    NaviSonicShuffleArtist,
    NaviSonicPlayMusicByDecade,
    NaviSonicPlayPlaylist,
    NaviSonicPlayMusicByGenre,
    NaviSonicPlayMusicRandom,
    NaviSonicPlayFavouriteSongs,
)
from asknavidrome.handlers_standard import (
    LaunchRequestHandler,
    CheckAudioInterfaceHandler,
    SkillEventHandler,
    HelpHandler,
    FallbackHandler,
    NaviSonicRandomiseQueue,
    NaviSonicSongDetails,
    NaviSonicStarSong,
    NaviSonicUnstarSong,
    PausePlaybackHandler,
    ResumePlaybackHandler,
    NextPlaybackHandler,
    PreviousPlaybackHandler,
    PlaybackStartedHandler,
    PlaybackStoppedHandler,
    PlaybackNearlyFinishedHandler,
    PlaybackFinishedHandler,
    PlaybackFailedEventHandler,
)

__all__ = [
    'sanitise_speech_output',
    'log_incoming_request',
    'NaviSonicPlaySearch',
    'NaviSonicQueueSearch',
    'NaviSonicInsertSearch',
    'NaviSonicPlaySimilar',
    'NaviSonicShuffleArtist',
    'NaviSonicPlayMusicByDecade',
    'NaviSonicPlayPlaylist',
    'NaviSonicPlayMusicByGenre',
    'NaviSonicPlayMusicRandom',
    'NaviSonicPlayFavouriteSongs',
    'LaunchRequestHandler',
    'CheckAudioInterfaceHandler',
    'SkillEventHandler',
    'HelpHandler',
    'FallbackHandler',
    'NaviSonicRandomiseQueue',
    'NaviSonicSongDetails',
    'NaviSonicStarSong',
    'NaviSonicUnstarSong',
    'PausePlaybackHandler',
    'ResumePlaybackHandler',
    'NextPlaybackHandler',
    'PreviousPlaybackHandler',
    'PlaybackStartedHandler',
    'PlaybackStoppedHandler',
    'PlaybackNearlyFinishedHandler',
    'PlaybackFinishedHandler',
    'PlaybackFailedEventHandler',
]