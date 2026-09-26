"""Realtime function-calling callbacks (tools) for the Yandex dialog.

Every tool has two parts:

  * the JSON schema sent to Yandex in ``session.update`` → ``tools``
    (``TOOLS`` / ``emotion_tool()`` / ``weather_tool()``);
  * the server-side handler executed when the model calls the function
    (``dispatch()``).

The model decides itself when to call a tool (guided by the system prompt);
Yandex reports the call as ``response.output_item.done`` with
``item.type == "function_call"``, and the service runs the corresponding
handler with a ``CallbackContext`` (what the server can do). Handlers may
return a STRING — the result goes back to the model as ``function_call_output``
and the model voices it (weather) — or None (side-effect only, e.g. emotion).

Add a new function in three steps: append its schema to ``TOOLS``, extend
``CallbackContext`` with the needed action, and add a branch in ``dispatch()``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import protocol as proto

logger = logging.getLogger("uvicorn")


def emotion_tool() -> Dict[str, Any]:
    """JSON schema of the ``emotion`` tool (sent to Yandex in session tools).

    The model CALLS ``emotion(name)`` instead of speaking/writing the command,
    so the answer audio never contains "Emotion: ...".
    """
    return {
        "type": "function",
        "name": "emotion",
        "description": "Показать роботу текущую эмоцию, уместную по контексту "
                       "диалога. Вызывай эту функцию вместо того, чтобы "
                       "произносить или писать слово Emotion.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": list(proto.ROBOT_EMOTIONS),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    }


def weather_tool() -> Dict[str, Any]:
    """JSON schema of the ``weather`` tool (sent to Yandex in session tools).

    The model CALLS ``weather(city)`` when the user asks about the weather;
    the server fetches the data and returns a text summary which the model
    then voices.
    """
    return {
        "type": "function",
        "name": "weather",
        "description": "Узнать текущую погоду в указанном городе. Вызывай "
                       "эту функцию, когда пользователь спрашивает о погоде "
                       "(например, \"какая погода в Омске\").",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Название города (например, Омск, Москва).",
                },
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    }


# All tools exposed to the model in the Realtime session (session.update).
TOOLS: List[Dict[str, Any]] = [emotion_tool(), weather_tool()]


class CallbackContext:
    """Actions the server can perform when the model calls a function.

    ``send_emotion(name)`` — sends the robot the EMOTION:<name> command
    (sync/async callable, may be None).
    ``get_weather(city)`` — returns a short weather summary string for the
    city (sync/async callable, may be None).

    Extend this class with new actions when adding tools.
    """

    def __init__(
            self,
            send_emotion: Optional[Callable[[str], Any]] = None,
            get_weather: Optional[Callable[[str], Any]] = None) -> None:
        self.send_emotion = send_emotion
        self.get_weather = get_weather


async def dispatch(name: str, args: Dict[str, Any],
                   ctx: Optional[CallbackContext] = None) -> Optional[str]:
    """Executes the model's function call on the server (the "functions").

    ``name`` — the tool name ("emotion" / "weather"); ``args`` — parsed JSON
    arguments; ``ctx`` — server capabilities.

    Returns a string with the result for the model (goes into
    ``function_call_output`` and is voiced by the model) or None for
    side-effect-only calls.
    """
    if name == "emotion":
        await _on_emotion(args, ctx)
        logger.info("[callbacks] emotion(%s) -> done", args)
        return None
    if name == "weather":
        result = await _on_weather(args, ctx)
        logger.info("[callbacks] weather(%s) -> %s",
                    args, (result or "")[:200])
        return result
    logger.info("[callbacks] unhandled function call: %s(%s)", name, args)
    return None


async def _on_emotion(args: Dict[str, Any],
                      ctx: Optional[CallbackContext]) -> None:
    """The ``emotion(name)`` handler: robot EMOTION:<name> command."""
    emotion = str(args.get("name", "")).strip().lower()
    if emotion not in proto.ROBOT_EMOTIONS:
        logger.warning("[callbacks] emotion: unknown name %r", emotion)
        return
    logger.info("[callbacks] emotion(%r)", emotion)
    if ctx is None or ctx.send_emotion is None:
        logger.info("[callbacks] emotion(%r) — no send_emotion handler",
                    emotion)
        return
    try:
        result = ctx.send_emotion(emotion)
        if asyncio.iscoroutine(result):
            result = await result
        logger.info("[callbacks] emotion(%r) sent -> %r", emotion, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[callbacks] emotion(%r) error: %s", emotion, exc)


async def _on_weather(args: Dict[str, Any],
                      ctx: Optional[CallbackContext]) -> str:
    """The ``weather(city)`` handler: fetches and returns a text summary.

    The returned string goes back to the model (function_call_output), which
    then voices the weather answer to the user.
    """
    city = str(args.get("city", "")).strip() or "Москва"
    logger.info("[callbacks] weather(%r)", city)
    if ctx is None or ctx.get_weather is None:
        logger.info("[callbacks] weather(%r) — no get_weather handler", city)
        return f"Не удалось получить погоду для города {city}."
    try:
        result = ctx.get_weather(city)
        if asyncio.iscoroutine(result):
            result = await result
        text = str(result or "").strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("[callbacks] weather(%r) error: %s", city, exc)
    return f"Не удалось получить погоду для города {city}."