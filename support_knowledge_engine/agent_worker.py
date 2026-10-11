"""Process entry points for the G9 execution boundary.

The parent never passes a SQLite connection into a job. A retrieval job opens
its own connection and closes it before returning. A killed job takes that
connection with the process.

These functions must stay importable under Windows spawn. They do not start
work at import time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .evidence import EvidenceCaptureError, capture_evidence_packet
from .provider import (
    ACTION_FINAL_DECISION,
    ACTION_TOOL_REQUEST,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    USAGE_KNOWN,
    USAGE_UNKNOWN,
    CancellationToken,
    EvidenceSource,
    EvidenceSummaryItem,
    ProviderExecutionError,
    UsageBudget,
    build_provider_request,
    invoke_provider,
    known_usage,
)
from .retrieval_tool import TOOL_NAME


_SEPARATOR = "\n---\n"


@dataclass(frozen=True)
class ProbeRule:
    """One observation-conditioned action for the in-repo test provider.

    Matching reads the trusted observation header. Untrusted text is ignored
    unless ``obey_injection`` is set, and even then the runtime must still
    reject any tool the header did not authorize.
    """

    kind: str
    query: str = ""
    outcome: str = "abstain"
    tool_name: str = TOOL_NAME
    extra_arguments: tuple[tuple[str, str], ...] = ()
    evidence_mode: str = "none"
    evidence_ids: tuple[str, ...] = ()
    input_tokens: int = 1
    output_tokens: int = 1
    usage_state: str = USAGE_KNOWN
    reason_code: str = "probe"
    sleep_s: float = 30.0
    when_decision: str = "*"
    when_reason: str = "*"
    when_min_retrievals: int = 0
    obey_injection: bool = False


class ProbeProvider:
    """Test provider. The next action depends on the observation header.

    A positional script cannot show that observation A changed the next query.
    This provider selects the rule from retrieval count, decision type, and
    reason codes supplied by the runtime. It does not read a paid model.
    """

    def __init__(
        self,
        rules: tuple[ProbeRule, ...],
        *,
        provider_id: str = "agent-probe",
        model_version: str = "agent-probe-v1",
    ) -> None:
        self.provider_id = provider_id
        self.model_version = model_version
        self._rules = tuple(rules)

    def produce(self, request: object, clock: object) -> object:
        del clock
        observation = getattr(request, "observation", "")
        header, untrusted = split_observation(observation if isinstance(observation, str) else "")
        rule = _select_rule(self._rules, header, untrusted)
        if rule is None:
            return _final_body(
                self,
                outcome="abstain",
                reason_code="unmatched",
                evidence_ids=(),
                input_tokens=1,
                output_tokens=1,
                usage_state=USAGE_KNOWN,
                query="",
            )
        if rule.kind == "block":
            time.sleep(rule.sleep_s if rule.sleep_s > 0 else 30.0)
            raise ProviderExecutionError(None)
        if rule.kind == "error":
            usage = None
            if rule.usage_state == USAGE_KNOWN:
                usage = known_usage(rule.input_tokens, rule.output_tokens)
            raise ProviderExecutionError(usage)
        if rule.kind == "malformed":
            return {"ignored": True}
        evidence_ids = rule.evidence_ids
        if rule.evidence_mode == "observed":
            evidence_ids = tuple(str(item) for item in header.get("latest_evidence_ids", []))
        if rule.kind == "tool":
            arguments = {"query": rule.query}
            arguments.update(rule.extra_arguments)
            return _tool_body(
                self,
                tool_name=rule.tool_name,
                arguments=arguments,
                reason_code=rule.reason_code,
                evidence_ids=evidence_ids,
                input_tokens=rule.input_tokens,
                output_tokens=rule.output_tokens,
                usage_state=rule.usage_state,
                query=rule.query,
            )
        return _final_body(
            self,
            outcome=rule.outcome,
            reason_code=rule.reason_code,
            evidence_ids=evidence_ids,
            input_tokens=rule.input_tokens,
            output_tokens=rule.output_tokens,
            usage_state=rule.usage_state,
            query=rule.query,
        )


def split_observation(observation: str) -> tuple[dict[str, object], str]:
    """Split a runtime observation into its header and untrusted tail."""

    head, _separator, tail = observation.partition(_SEPARATOR)
    try:
        parsed = json.loads(head) if head else {}
    except json.JSONDecodeError:
        return {}, observation
    if not isinstance(parsed, dict):
        return {}, tail
    return parsed, tail


def provider_job(
    provider: object,
    payload: dict[str, object],
    cancel_event: object,
    queue: object,
    sentinel: str | None,
) -> None:
    """Invoke one provider inside this process."""

    _mark(sentinel)
    token = CancellationToken()
    watcher = threading.Thread(
        target=_watch_cancel,
        args=(cancel_event, token),
        name="g9-provider-cancel",
        daemon=True,
    )
    watcher.start()
    visible: list[dict[str, str]] = []
    try:
        request = build_provider_request(
            run_id=str(payload["run_id"]),
            case_id=payload["case_id"] if isinstance(payload["case_id"], str) else None,
            observation=str(payload["observation"]),
            available_tools=tuple(payload["available_tools"]),  # type: ignore[arg-type]
            evidence=tuple(payload["evidence"]),  # type: ignore[arg-type]
            system_policy=str(payload["system_policy"]),
            budget=UsageBudget(
                max_input_tokens=int(payload["max_input_tokens"]),  # type: ignore[arg-type]
                max_output_tokens=int(payload["max_output_tokens"]),  # type: ignore[arg-type]
                max_total_tokens=int(payload["max_total_tokens"]),  # type: ignore[arg-type]
            ),
            deadline_monotonic=time.monotonic() + float(payload["deadline_s"]),  # type: ignore[arg-type]
            cancellation=token,
        )
        visible = _visible_evidence(request.evidence)
        result = invoke_provider(provider, request)
        dumped = _dump_provider(result)
    except Exception:
        dumped = {
            "kind": "provider",
            "ok": False,
            "started": True,
            "action_type": None,
            "tool_name": None,
            "arguments": [],
            "outcome": None,
            "query_reformulation": "",
            "reason_code": None,
            "evidence_references": [],
            "provider_id": str(getattr(provider, "provider_id", "")),
            "model_version": str(getattr(provider, "model_version", "")),
            "usage_state": USAGE_UNKNOWN,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "error_type": "provider_error",
        }
    dumped["provider_progress"] = _provider_progress(provider)
    dumped["visible_evidence"] = visible
    _put(queue, dumped)


def retrieval_job(
    payload: dict[str, object],
    queue: object,
    sentinel: str | None,
) -> None:
    """Capture one packet on a connection this process owns."""

    _mark(sentinel)
    mode = str(payload["retrieval_mode"])
    if mode == "block":
        time.sleep(float(payload["sleep_s"]))  # type: ignore[arg-type]
        _put(queue, {"kind": "tool", "ok": False, "error_type": "blocked", "capture_calls": 0})
        return
    if mode == "sqlite_block":
        try:
            connection = _connect(str(payload["database_path"]))
        except sqlite3.Error:
            _put(queue, {"kind": "tool", "ok": False, "error_type": "retrieval_failure", "capture_calls": 0, "packet": None})
            return
        try:
            connection.execute(
                "WITH RECURSIVE c(x) AS ("
                "SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 1000000000"
                ") SELECT max(x) FROM c"
            )
            _put(queue, {"kind": "tool", "ok": True, "capture_calls": 0, "packet": None})
        finally:
            connection.close()
        return
    try:
        connection = _connect(str(payload["database_path"]))
    except sqlite3.Error:
        _put(queue, {"kind": "tool", "ok": False, "error_type": "retrieval_failure", "capture_calls": 0, "packet": None})
        return
    try:
        packet = capture_evidence_packet(
            connection,
            str(payload["query"]),
            product_series=str(payload["product_series"]),
            document_type=str(payload["document_type"]),
            status=str(payload["status"]),
            association=str(payload["association"]),
            product_id=str(payload["product_id"]),
            firmware_version=payload["firmware_version"]
            if isinstance(payload["firmware_version"], str)
            else None,
            retrieval_deadline_s=float(payload["deadline_s"]),  # type: ignore[arg-type]
        )
        _put(queue, {"kind": "tool", "ok": True, "error_type": None, "capture_calls": 1, "packet": packet})
    except EvidenceCaptureError as exc:
        _put(
            queue,
            {
                "kind": "tool",
                "ok": False,
                "error_type": exc.error_type,
                "capture_calls": 1,
                "packet": None,
            },
        )
    except Exception:
        _put(
            queue,
            {
                "kind": "tool",
                "ok": False,
                "error_type": "retrieval_failure",
                "capture_calls": 1,
                "packet": None,
            },
        )
    finally:
        connection.close()


def telemetry_job(payload: dict[str, object], queue: object, sentinel: str | None) -> None:
    """Record or block. This job does not receive a database connection."""

    _mark(sentinel)
    if str(payload["mode"]) == "block":
        time.sleep(float(payload["sleep_s"]))  # type: ignore[arg-type]
        _put(queue, {"kind": "telemetry", "status": "late"})
        return
    _put(
        queue,
        {
            "kind": "telemetry",
            "status": "recorded",
            "query_marker": str(payload["query_marker"]),
            "result_count": int(payload["result_count"]),  # type: ignore[arg-type]
        },
    )


def finalizer_job(payload: dict[str, object], queue: object, sentinel: str | None) -> None:
    """Seal or block. A late ack must not become the published termination."""

    _mark(sentinel)
    if str(payload["mode"]) == "block":
        time.sleep(float(payload["sleep_s"]))  # type: ignore[arg-type]
        _put(queue, {"kind": "finalizer", "status": "late", "reason": str(payload["reason"])})
        return
    _put(queue, {"kind": "finalizer", "status": "sealed", "reason": str(payload["reason"])})


def late_result_job(queue: object, sentinel: str | None, sleep_s: float) -> None:
    """Queue a finished-looking payload, then stay alive so the parent can reject it."""

    _mark(sentinel)
    _put(
        queue,
        {
            "kind": "provider",
            "ok": True,
            "started": True,
            "action_type": ACTION_FINAL_DECISION,
            "tool_name": None,
            "arguments": [],
            "outcome": "supported",
            "query_reformulation": "",
            "reason_code": "late",
            "evidence_references": [],
            "provider_id": "late-probe",
            "model_version": "late-probe-v1",
            "usage_state": USAGE_KNOWN,
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "error_type": None,
        },
    )
    time.sleep(sleep_s)


def blocker_job(sleep_s: float, queue: object, sentinel: str | None) -> None:
    """Stay alive until the parent terminates this process."""

    _mark(sentinel)
    time.sleep(sleep_s)
    _put(queue, {"kind": "blocker", "ok": True})


def evidence_summary_item(
    evidence_id: str,
    document_identity: str,
    source_locator: str,
    untrusted_text: str,
) -> EvidenceSummaryItem:
    return EvidenceSummaryItem(
        evidence_id=evidence_id,
        source=EvidenceSource(
            document_identity=document_identity,
            source_locator=source_locator,
        ),
        representation_version="decision-visible-v1",
        transformation_version="retrieval-excerpt-v1",
        untrusted_text=untrusted_text,
    )


def _provider_progress(provider: object) -> dict[str, int]:
    """Absolute script position after this call. The parent copies it back."""

    progress: dict[str, int] = {}
    cursor = getattr(provider, "_cursor", None)
    calls = getattr(provider, "calls", None)
    if type(cursor) is int:
        progress["cursor"] = cursor
    if type(calls) is int:
        progress["calls"] = calls
    return progress


def _visible_evidence(evidence: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(evidence, tuple):
        return rows
    for item in evidence:
        source = getattr(item, "source", None)
        rows.append(
            {
                "evidence_id": str(getattr(item, "evidence_id", "")),
                "document_identity": str(getattr(source, "document_identity", "")),
                "source_locator": str(getattr(source, "source_locator", "")),
                "transformation_version": str(getattr(item, "transformation_version", "")),
            }
        )
    return rows


def _select_rule(
    rules: tuple[ProbeRule, ...],
    header: dict[str, object],
    untrusted: str,
) -> ProbeRule | None:
    decision = header.get("decision_type")
    reasons = header.get("reason_codes")
    reason_set = {str(item) for item in reasons} if isinstance(reasons, list) else set()
    retrievals = header.get("retrieval_count")
    retrieval_count = retrievals if isinstance(retrievals, int) else 0
    for rule in rules:
        if rule.obey_injection:
            if "CALL exfiltrate" in untrusted:
                return rule
            continue
        if retrieval_count < rule.when_min_retrievals:
            continue
        if rule.when_decision != "*":
            if rule.when_decision == "unset":
                if decision is not None:
                    continue
            elif decision != rule.when_decision:
                continue
        if rule.when_reason != "*" and rule.when_reason not in reason_set:
            continue
        return rule
    return None


def _tool_body(
    provider: ProbeProvider,
    *,
    tool_name: str,
    arguments: dict[str, str],
    reason_code: str,
    evidence_ids: tuple[str, ...],
    input_tokens: int,
    output_tokens: int,
    usage_state: str,
    query: str,
) -> dict[str, object]:
    return _envelope(
        provider,
        action_type=ACTION_TOOL_REQUEST,
        tool_request={"name": tool_name, "arguments": arguments},
        final_decision=None,
        reason_code=reason_code,
        evidence_ids=evidence_ids,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_state=usage_state,
        query=query,
    )


def _final_body(
    provider: ProbeProvider,
    *,
    outcome: str,
    reason_code: str,
    evidence_ids: tuple[str, ...],
    input_tokens: int,
    output_tokens: int,
    usage_state: str,
    query: str,
) -> dict[str, object]:
    return _envelope(
        provider,
        action_type=ACTION_FINAL_DECISION,
        tool_request=None,
        final_decision={"outcome": outcome},
        reason_code=reason_code,
        evidence_ids=evidence_ids,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_state=usage_state,
        query=query,
    )


def _envelope(
    provider: ProbeProvider,
    *,
    action_type: str,
    tool_request: dict[str, object] | None,
    final_decision: dict[str, str] | None,
    reason_code: str,
    evidence_ids: tuple[str, ...],
    input_tokens: int,
    output_tokens: int,
    usage_state: str,
    query: str,
) -> dict[str, object]:
    if usage_state == USAGE_UNKNOWN:
        usage: dict[str, object] = {
            "state": USAGE_UNKNOWN,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    else:
        usage = {
            "state": USAGE_KNOWN,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    reformulation = query if len(query) <= 500 else ""
    return {
        "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
        "action_type": action_type,
        "tool_request": tool_request,
        "final_decision": final_decision,
        "query_reformulation": reformulation,
        "reason_code": reason_code,
        "evidence_references": list(evidence_ids),
        "provider_id": provider.provider_id,
        "model_version": provider.model_version,
        "usage": usage,
    }


def _dump_provider(result: object) -> dict[str, object]:
    usage = getattr(result, "usage", None)
    tool = getattr(result, "tool_request", None)
    decision = getattr(result, "final_decision", None)
    error = getattr(result, "error", None)
    arguments = []
    tool_name = None
    if tool is not None:
        tool_name = tool.name
        arguments = [[key, value] for key, value in tool.arguments]
    return {
        "kind": "provider",
        "ok": bool(result.ok),
        "started": bool(result.started),
        "action_type": result.action_type,
        "tool_name": tool_name,
        "arguments": arguments,
        "outcome": None if decision is None else decision.outcome,
        "query_reformulation": result.query_reformulation or "",
        "reason_code": result.reason_code,
        "evidence_references": list(result.evidence_references or []),
        "provider_id": result.provider_id,
        "model_version": result.model_version,
        "usage_state": None if usage is None else usage.state,
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "total_tokens": None if usage is None else usage.total_tokens,
        "error_type": None if error is None else error.type,
    }


def _connect(database_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=1.0)
    connection.row_factory = sqlite3.Row
    return connection


def _watch_cancel(cancel_event: object, token: CancellationToken) -> None:
    wait = getattr(cancel_event, "wait", None)
    if not callable(wait):
        return
    wait()
    token.cancel()


def _mark(sentinel: str | None) -> None:
    if sentinel:
        Path(sentinel).write_text("started", encoding="utf-8")


def _put(queue: object, payload: dict[str, object]) -> None:
    put = getattr(queue, "put", None)
    if callable(put):
        put(payload, timeout=1.0)
