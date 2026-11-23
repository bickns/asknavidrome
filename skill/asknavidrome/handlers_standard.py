"""
Standard handlers: everything that isn't about choosing what to play.

  - skill plumbing: launch, help, fallback, audio-interface check,
    session/skill events
  - controls: pause/stop/cancel, resume, next, previous, randomise,
    what's playing, star/unstar
  - the AudioPlayer events Alexa sends by itself: PlaybackStarted,
    PlaybackStopped, PlaybackNearlyFinished, PlaybackFinished,
    PlaybackFailed

The search handlers are in handlers_search.py and the whole-collection
handlers (playlist, genre, ...) are in handlers_collections.py. This
file rarely changes.
"""

from datetime import datetime

from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model import Response

import asknavidrome.controller as controller
from asknavidrome import state
from asknavidrome.config import connection, logger
from asknavidrome.helpers import (
    device_and_queue,
    sanitise_speech_output,
    log_playback_start,
)
from asknavidrome.similar import start_similar_lookup, ensure_next_track


#
# Skill plumbing
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

    AMAZON.FallbackIntent is no longer in the interaction model, so this
    should never fire. It is kept as a safety net: if Alexa ever does
    send one, this avoids an unhandled "Unable to find a suitable
    request handler" exception. Delete it once the log has shown no
    "FallbackIntent triggered" line for a good while.

    Amazon deliberately withholds the raw spoken text and any slot
    value for this intent, so the query cannot be recovered here.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('AMAZON.FallbackIntent')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.warning('FallbackIntent triggered - Alexa could not confidently match the utterance')
        text = sanitise_speech_output("I didn't catch that as something to play. Try saying play, followed by a song, artist, or genre.")
        handler_input.response_builder.speak(text).ask(text)
        return handler_input.response_builder.response


#
# Controls
#

class NaviSonicRandomiseQueue(AbstractRequestHandler):
    """Handle NaviSonicRandomiseQueue Intent

    Shuffle the current play queue
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicRandomiseQueue')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicRandomiseQueue Handler')
        device_id, play_queue = device_and_queue(handler_input)

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
        device_id, play_queue = device_and_queue(handler_input)

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
        device_id, play_queue = device_and_queue(handler_input)

        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')

        return handler_input.response_builder.response


class NaviSonicUnstarSong(AbstractRequestHandler):
    """Handle NaviSonicUnstarSong Intent

    Remove the star / favourite from the current song
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name('NaviSonicUnstarSong')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NaviSonicUnstarSong Handler')
        device_id, play_queue = device_and_queue(handler_input)

        current_track = play_queue.get_current_track()

        song_id = current_track.id
        connection.star_entry(song_id, 'song')
        connection.unstar_entry(song_id, 'song')

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
        device_id, play_queue = device_and_queue(handler_input)
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
        device_id, play_queue = device_and_queue(handler_input)

        current_track = play_queue.get_current_track()

        if current_track.offset > 0:
            logger.info('Resuming ' + str(current_track.title))
            logger.info('Offset ' + str(current_track.offset))

            return controller.start_playback('play', None, None, current_track, handler_input)

        elif play_queue.get_queue_count() > 0 and current_track.offset == 0:
            logger.info('Resuming - There was no paused track, getting next track from queue')
            track_details = play_queue.get_next_track()

            return controller.start_playback('play', None, None, track_details, handler_input)

        logger.info('Resume requested but there is nothing to resume')
        return handler_input.response_builder.response


class NextPlaybackHandler(AbstractRequestHandler):
    """Handle NextIntent.

    If the queue is empty when "next" is requested, the waiting similar
    song is moved into the queue first. If nothing is waiting yet, say
    so (voice only - PlaybackController.NextCommandIssued, a
    hardware/touch button event, cannot carry outputSpeech) and make
    sure a lookup is running.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return (is_intent_name('AMAZON.NextIntent')(handler_input) or
                is_request_type('PlaybackController.NextCommandIssued')(handler_input))

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In NextPlaybackHandler')
        device_id, play_queue = device_and_queue(handler_input)
        logger.debug(f'Queue count before next: {play_queue.get_queue_count()}')

        if play_queue.get_queue_count() == 0:
            ensure_next_track(play_queue, device_id)

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing queued and no similar song waiting yet')
            if is_intent_name('AMAZON.NextIntent')(handler_input):
                text = sanitise_speech_output("I'm still finding the next song, try again in a few seconds.")
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
        device_id, play_queue = device_and_queue(handler_input)

        if play_queue.get_history_count() == 0:
            logger.info('No previous track in history')
            if is_intent_name('AMAZON.PreviousIntent')(handler_input):
                text = sanitise_speech_output("There's nothing before this.")
                handler_input.response_builder.speak(text)
            return handler_input.response_builder.response

        track_details = play_queue.get_previous_track()
        track_details.offset = 0
        return controller.start_playback('play', None, None, track_details, handler_input)


#
# AudioPlayer events (sent by Alexa itself; none may carry speech)
#

class PlaybackStartedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackStarted Directive received.

    Confirming that the requested audio file began playing. Do not
    send any specific response.

    This is also where the search for the waiting similar song begins
    when nothing is queued behind the track that has just started -
    which is always the case after a single-track "play X". The call is
    harmless if a song is already waiting or a lookup is already running.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackStarted')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackStartedHandler')
        logger.info('Playback started')

        device_id, play_queue = device_and_queue(handler_input)

        if play_queue.get_queue_count() == 0:
            start_similar_lookup(play_queue, device_id)

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
        device_id, play_queue = device_and_queue(handler_input)

        play_queue.set_current_track_offset(handler_input.request_envelope.request.offset_in_milliseconds)

        current_track = play_queue.get_current_track()
        logger.debug(f'Stored track offset of: {current_track.offset} ms for {current_track.title}')
        logger.info('Playback stopped')

        return handler_input.response_builder.response


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackNearlyFinished Directive received.

    Alexa asks for the next track a few seconds before the current one
    ends. If nothing is queued, the waiting similar song is moved into
    the queue first. If there is still nothing, playback simply ends
    after this track.

    Handing the next track over also marks it as committed in the
    queue, so an insert arriving now goes behind it rather than ahead
    of it. If nothing is queued behind the track handed over, the
    search for the next waiting song starts now.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackNearlyFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackNearlyFinishedHandler')
        device_id, play_queue = device_and_queue(handler_input)

        if len(play_queue.get_buffer()) == 0:
            ensure_next_track(play_queue, device_id)

        if len(play_queue.get_buffer()) == 0:
            logger.info('Nothing to hand to Alexa - playback will end after this track')
            return handler_input.response_builder.response

        logger.info('Queuing next track...')
        track_details = play_queue.enqueue_next_track()

        if play_queue.get_queue_count() <= 1:
            start_similar_lookup(play_queue, device_id)

        return controller.start_playback('continue', None, None, track_details, handler_input)


class PlaybackFinishedHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFinished Directive received.

    Fires when a track runs to its natural end (a skipped or stopped
    track produces PlaybackStopped instead). The finished track is
    scrobbled and recorded in state, so that similar-song choices can
    be seeded from it and avoid repeating it.

    The queue then moves on to the track Alexa has already been given.
    Nothing is added to the queue here: finding a next song is done
    earlier, in the background.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFinished')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFinishedHandler')
        device_id, play_queue = device_and_queue(handler_input)

        timestamp_ms = datetime.now().timestamp()
        current_track = play_queue.get_current_track()
        connection.scrobble(current_track.id, timestamp_ms)
        state.record_finished_track(device_id, current_track.id)
        logger.info(f'Recorded finished track: {current_track.title} by {current_track.artist} [id={current_track.id}]')

        if play_queue.get_queue_count() > 0:
            play_queue.get_next_track()
        else:
            logger.info('Queue is empty - nothing left to play')

        return handler_input.response_builder.response


class PlaybackFailedEventHandler(AbstractRequestHandler):
    """AudioPlayer.PlaybackFailed Directive received.

    Logging the error and moving on to the next track with no output
    speech. If the queue is empty, the waiting similar song is moved
    into it first.
    """

    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type('AudioPlayer.PlaybackFailed')(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        logger.debug('In PlaybackFailedHandler')
        device_id, play_queue = device_and_queue(handler_input)

        current_track = play_queue.get_current_track()
        song_id = current_track.id

        logger.error(f'Playback Failed: {handler_input.request_envelope.request.error}')
        logger.error(f'Failed playing track with ID: {song_id}')

        if play_queue.get_queue_count() == 0:
            ensure_next_track(play_queue, device_id)

        if play_queue.get_queue_count() == 0:
            logger.info('Nothing left to play after a playback failure')
            return handler_input.response_builder.response

        track_details = play_queue.get_next_track()
        track_details.offset = 0
        log_playback_start(play_queue, None)

        return controller.start_playback('play', None, None, track_details, handler_input)