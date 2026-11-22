"""
AskNavidrome web service entry point.

Creates the Flask app and ASK SkillBuilder, registers every request
and exception handler, wires up request/response logging interceptors
when debug is enabled, and (at NAVI_DEBUG=3) exposes the /queue,
/history, and /buffer diagnostic routes.

Importing asknavidrome.config triggers its module-level startup
sequence (env var validation, Navidrome connection, genre/artist
caching) as a side effect - it must be imported before anything else
here runs, since handlers.py, exception_handlers.py, and search.py all
depend on config having already completed successfully.
"""

from flask import Flask, render_template

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestInterceptor, AbstractResponseInterceptor
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response
from flask_ask_sdk.skill_adapter import SkillAdapter

from asknavidrome.config import skill_id, navidrome_log_level, logger
from asknavidrome import state
from asknavidrome import handlers as h
from asknavidrome import exception_handlers as eh


#
# Request / Response logging interceptors
#

class IntentLoggingInterceptor(AbstractRequestInterceptor):
    """Log the intent name and slot values for every incoming request,
    regardless of debug level - see handlers.log_incoming_request()."""

    def process(self, handler_input: HandlerInput):
        h.log_incoming_request(handler_input)


class LoggingRequestInterceptor(AbstractRequestInterceptor):
    """Intercept all requests

    Intercepts all requests sent to the skill and prints them in the log
    """

    def process(self, handler_input: HandlerInput):
        logger.debug(f'Request received: {handler_input.request_envelope.request}')


class LoggingResponseInterceptor(AbstractResponseInterceptor):
    """Intercept all responses

    Intercepts all responses sent from the skill and prints them in the log
    """

    def process(self, handler_input: HandlerInput, response: Response):
        logger.debug(f'Response sent: {response}')


#
# Create web service and skill object
#

app = Flask(__name__)
sb = SkillBuilder()
sb.skill_id = skill_id


# Register Intent Handlers
sb.add_request_handler(h.LaunchRequestHandler())
sb.add_request_handler(h.CheckAudioInterfaceHandler())
sb.add_request_handler(h.SkillEventHandler())
sb.add_request_handler(h.HelpHandler())
sb.add_request_handler(h.FallbackHandler())
sb.add_request_handler(h.NaviSonicPlayMusicByArtist())
sb.add_request_handler(h.NaviSonicShuffleArtist())
sb.add_request_handler(h.NaviSonicPlaySimilar())
sb.add_request_handler(h.NaviSonicPlayAlbumByArtist())
sb.add_request_handler(h.NaviSonicPlayPlaylist())
sb.add_request_handler(h.NaviSonicPlayFavouriteSongs())
sb.add_request_handler(h.NaviSonicPlayMusicByDecade())
sb.add_request_handler(h.NaviSonicPlayMusicByGenre())
sb.add_request_handler(h.NaviSonicPlayMusicRandom())
sb.add_request_handler(h.NaviSonicRandomiseQueue())
sb.add_request_handler(h.NaviSonicSongDetails())
sb.add_request_handler(h.NaviSonicStarSong())
sb.add_request_handler(h.NaviSonicUnstarSong())
sb.add_request_handler(h.NaviSonicPlaySearch())
sb.add_request_handler(h.NaviSonicQueueSearch())
sb.add_request_handler(h.NaviSonicInsertSearch())

# Register AudioPlayer Handlers
sb.add_request_handler(h.PlaybackStartedHandler())
sb.add_request_handler(h.PlaybackStoppedHandler())
sb.add_request_handler(h.PlaybackNearlyFinishedHandler())
sb.add_request_handler(h.PlaybackFinishedHandler())
sb.add_request_handler(h.PausePlaybackHandler())
sb.add_request_handler(h.NextPlaybackHandler())
sb.add_request_handler(h.PreviousPlaybackHandler())
sb.add_request_handler(h.ResumePlaybackHandler())
sb.add_request_handler(h.PlaybackFailedEventHandler())

# Register Exception Handlers
sb.add_exception_handler(eh.SystemExceptionHandler())
sb.add_exception_handler(eh.GeneralExceptionHandler())

sb.add_global_request_interceptor(IntentLoggingInterceptor())

if navidrome_log_level >= 2:
    # Register Interceptors (log all requests)
    sb.add_global_request_interceptor(LoggingRequestInterceptor())
    sb.add_global_response_interceptor(LoggingResponseInterceptor())

sa = SkillAdapter(skill=sb.create(), skill_id='test', app=app)
sa.register(app=app, route='/')


#
# Debug-only diagnostic routes
#

if navidrome_log_level == 3:
    logger.warning('AskNavidrome debugging has been enabled, this should only be used when testing!')
    logger.warning('The /buffer, /queue and /history http endpoints are available publicly!')

    @app.route('/queue')
    def view_queue():
        """View the contents of play_queue.queue

        Creates a tabulated page containing the contents of the play_queue.queue deque.
        """

        current_track = state.play_queue.get_current_track()

        return render_template('table.html', title='AskNavidrome - Queued Tracks',
                               tracks=state.play_queue.get_current_queue(), current=current_track)

    @app.route('/history')
    def view_history():
        """View the contents of play_queue.history

        Creates a tabulated page containing the contents of the play_queue.history deque.
        """

        current_track = state.play_queue.get_current_track()

        return render_template('table.html', title='AskNavidrome - Track History',
                               tracks=state.play_queue.get_history(), current=current_track)

    @app.route('/buffer')
    def view_buffer():
        """View the contents of play_queue.buffer

        Creates a tabulated page containing the contents of the play_queue.buffer deque.
        """

        current_track = state.play_queue.get_current_track()

        return render_template('table.html', title='AskNavidrome - Buffered Tracks',
                               tracks=state.play_queue.get_buffer(), current=current_track)


# Run web app by default when file is executed.
if __name__ == '__main__':
    # Start the web service
    app.run(host='0.0.0.0')