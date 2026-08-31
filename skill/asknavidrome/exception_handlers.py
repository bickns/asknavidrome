"""
Exception handlers for the AskNavidrome Alexa skill.

Two handlers: SystemExceptionHandler (Alexa's own reported exceptions)
and GeneralExceptionHandler (a catch-all for anything else, including
unhandled Python exceptions from our own code). Both log a full
traceback and the truncated device ID (so a failure can be traced back
to which Echo triggered it in multi-device testing), and speak back
whatever slot values Alexa had passed - where the request type allows
it, so a failed search is diagnosable from the spoken response alone.

Certain request types (AudioPlayer directives, PlaybackController
button events, System.ExceptionEncountered itself) must not receive a
response containing outputSpeech, reprompt, or shouldEndSession -
Alexa rejects such responses outright, and (if the rejected response
came from one of these handlers) sends back a further
System.ExceptionEncountered request as a result. _request_forbids_speech()
exists specifically to prevent that loop.
"""

import traceback

from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.utils import is_request_type, get_intent_name, get_request_type
from ask_sdk_model import Response

from asknavidrome.config import logger
from asknavidrome.handlers import sanitise_speech_output


def _request_forbids_speech(handler_input: HandlerInput) -> bool:
    """Certain request types (AudioPlayer directives, PlaybackController
    button events, System.ExceptionEncountered) must not receive a
    response containing outputSpeech, reprompt, or shouldEndSession -
    Alexa rejects such responses outright. Exception handlers must
    check this before building a spoken response.

    :param handler_input: The current request's HandlerInput
    :return: True if a spoken response would be rejected for this request type
    :rtype: bool
    """
    req_type = get_request_type(handler_input)
    return (req_type.startswith('AudioPlayer.') or
            req_type.startswith('PlaybackController.') or
            req_type == 'System.ExceptionEncountered')


def get_slot_summary(handler_input: HandlerInput) -> str:
    """Extract any filled slot values from the current request, for
    diagnostic speech output when an exception occurs. Returns an
    empty string if this wasn't an IntentRequest or nothing was filled.

    :param handler_input: The current request's HandlerInput
    :return: A comma-separated string of filled slot values, or ''
    :rtype: str
    """

    if get_request_type(handler_input) != 'IntentRequest':
        return ''

    intent_name = get_intent_name(handler_input)
    logger.error(f'Intent Name Was: {intent_name}')

    slots = handler_input.request_envelope.request.intent.slots
    if not slots:
        return ''

    filled = [slot.value for slot in slots.values() if slot and slot.value]
    return ', '.join(filled)


def _get_short_device_id(handler_input: HandlerInput) -> str:
    """Get the last 8 characters of the current request's device ID,
    for log readability - full device IDs run 150+ characters. Guards
    against missing device context, since exception handlers can fire
    for a broader range of request types than the intent handlers,
    where context.system.device isn't always guaranteed to be present.

    :param handler_input: The current request's HandlerInput
    :return: The last 8 characters of the device ID, or 'unknown'
    :rtype: str
    """
    device = handler_input.request_envelope.context.system.device
    if device is None or not device.device_id:
        return 'unknown'
    return device.device_id[-8:]


class SystemExceptionHandler(AbstractExceptionHandler):
    """Handle System.ExceptionEncountered

    Handles exceptions and prints error information, including a full
    traceback and the device that triggered it, in the log. Speaks
    back whatever slot values Alexa passed (if any), so a failed
    search is diagnosable from the spoken response alone - except when
    the request type forbids a spoken response entirely (see
    _request_forbids_speech()).
    """

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return is_request_type('System.ExceptionEncountered')(handler_input)

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In SystemExceptionHandler')

        # Log the exception, including a full traceback and device ID
        logger.error(f'Device: ...{_get_short_device_id(handler_input)}')
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
    """Handle general exceptions

    Handles exceptions and prints error information, including a full
    traceback and the device that triggered it, in the log. Same
    speech-forbidding-request-type guard and slot-summary speech as
    SystemExceptionHandler above.
    """

    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return True

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.debug('In GeneralExceptionHandler')

        # Log the exception, including a full traceback and device ID
        logger.error(f'Device: ...{_get_short_device_id(handler_input)}')
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