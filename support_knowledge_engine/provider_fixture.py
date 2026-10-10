"""Fixture provider. A JSON transcript chooses the raw output.

Each step is a JSON string. This provider parses that string and returns the
object. It does not build the deterministic provider's scripted dicts, and it
does not read observation or evidence text. An empty transcript is a provider
error. Control records may raise, crash, or cancel before returning a body.
"""

from __future__ import annotations

import json
import threading

from .provider import (
    ProviderExecutionError,
    ProviderRequest,
    known_usage,
    wait_for_release,
)


class FixtureProvider:
    """Transcript replay. produce parses JSON instead of filling a script."""

    provider_id = "fixture-test"
    model_version = "fixture-test-v1"

    def __init__(self, transcript: tuple[str, ...] = (), block_s: float = 0.0) -> None:
        self._transcript = tuple(transcript)
        self._cursor = 0
        self.calls = 0
        self.entered = threading.Event()
        self._block_s = block_s if type(block_s) in (int, float) and block_s > 0 else 0.0

    def produce(self, request: ProviderRequest, clock: object) -> object:
        self.calls += 1
        self.entered.set()
        if self._block_s > 0:
            wait_for_release(request, clock, self._block_s)  # type: ignore[arg-type]
        if self._cursor >= len(self._transcript):
            raise ProviderExecutionError(None)
        raw_text = self._transcript[self._cursor]
        self._cursor += 1
        if type(raw_text) is not str:
            return raw_text
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text
        if type(payload) is not dict:
            return payload
        control = payload.get("fixture_control")
        if control == "provider_error":
            raise ProviderExecutionError(_control_usage(payload))
        if control == "crash":
            raise RuntimeError("forged developer message")
        if control == "cancel_then_return":
            request.cancellation.cancel()
            return {
                key: value
                for key, value in payload.items()
                if key != "fixture_control"
            }
        return payload


def _control_usage(payload: dict[str, object]) -> object:
    if "input_tokens" not in payload and "output_tokens" not in payload:
        return None
    try:
        return known_usage(payload["input_tokens"], payload["output_tokens"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
