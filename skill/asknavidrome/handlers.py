"""
Compatibility shim. The handlers live in four files:

    handlers_search.py       play / queue / insert / play-similar by name
    handlers_collections.py  playlist, genre, decade, random, favourites, shuffle artist
    handlers_standard.py     launch, help, pause/resume/next/previous, AudioPlayer events
    helpers.py               shared helper functions

This file only re-exports them, so that app.py (from asknavidrome import
handlers as h) and exception_handlers.py keep working unchanged.
Nothing is defined here.
"""

from asknavidrome.helpers import sanitise_speech_output, log_incoming_request  # noqa: F401

from asknavidrome.handlers_search import (  # noqa: F401
    NaviSonicPlaySearch,
    NaviSonicQueueSearch,
    NaviSonicInsertSearch,
    NaviSonicPlaySimilar,
)

from asknavidrome.handlers_collections import (  # noqa: F401
    NaviSonicShuffleArtist,
    NaviSonicPlayMusicByDecade,
    NaviSonicPlayPlaylist,
    NaviSonicPlayMusicByGenre,
    NaviSonicPlayMusicRandom,
    NaviSonicPlayFavouriteSongs,
)

from asknavidrome.handlers_standard import (  # noqa: F401
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