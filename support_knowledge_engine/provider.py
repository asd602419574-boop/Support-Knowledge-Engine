"""Vendor-neutral model provider boundary.

The boundary validates one request, calls one provider once, and returns one
structured result. A tool request is only a request. A final decision is only
the provider's structured opinion. Neither one retrieves, writes a case,
writes a trace, or moves a workflow.

System policy, the tool allow-list, budgets, and the deadline come from the
trusted request fields. Observation text and evidence text stay in their own
fields. This module does not parse that text into policy, tools, or limits.

Usage is known only when the provider supplies a consistent account. Unknown
consumption uses state "unknown" and null counts. It is never stored as zero.

Timeout, cancellation, and an exhausted budget are decided before the provider
runs when they are already true. A blocking provider must observe the
cancellation token and the deadline. wait_for_release is bounded. This gate
does not run an agent loop, call a paid model, or prove that a real model
would ignore malicious document text.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

PROVIDER_BOUNDARY_VERSION = "1"
PROVIDER_REQUEST_SCHEMA_VERSION = "1"
PROVIDER_RESPONSE_SCHEMA_VERSION = "1"

ACTION_TOOL_REQUEST = "tool_request"
ACTION_FINAL_DECISION = "final_decision"
OUTCOME_SUPPORTED = "supported"
OUTCOME_ABSTAIN = "abstain"
OUTCOME_CONFLICT = "conflict"
OUTCOME_CLARIFY = "clarify"

USAGE_KNOWN = "known"
USAGE_UNKNOWN = "unknown"

ERROR_INVALID_REQUEST = "invalid_request"
ERROR_TIMEOUT = "timeout"
ERROR_CANCELLATION = "cancellation"
ERROR_PROVIDER = "provider_error"
ERROR_MALFORMED = "malformed_output"
ERROR_INVALID_ACTION = "invalid_action"
ERROR_BUDGET = "budget_exceeded"

INVALID_REQUEST_MESSAGE = "provider request 不满足合同。"
TIMEOUT_MESSAGE = "provider 调用在 deadline 到达时终止。"
CANCELLATION_MESSAGE = "provider 调用已取消。"
PROVIDER_ERROR_MESSAGE = "provider 调用失败。"
MALFORMED_OUTPUT_MESSAGE = "provider 输出不符合 response schema。"
INVALID_ACTION_MESSAGE = "provider 动作不在可信权限内。"
BUDGET_EXCEEDED_MESSAGE = "provider 调用超出 usage budget。"

_MESSAGES = {
    ERROR_INVALID_REQUEST: INVALID_REQUEST_MESSAGE,
    ERROR_TIMEOUT: TIMEOUT_MESSAGE,
    ERROR_CANCELLATION: CANCELLATION_MESSAGE,
    ERROR_PROVIDER: PROVIDER_ERROR_MESSAGE,
    ERROR_MALFORMED: MALFORMED_OUTPUT_MESSAGE,
    ERROR_INVALID_ACTION: INVALID_ACTION_MESSAGE,
    ERROR_BUDGET: BUDGET_EXCEEDED_MESSAGE,
}
_OUTCOMES = frozenset(
    {OUTCOME_SUPPORTED, OUTCOME_ABSTAIN, OUTCOME_CONFLICT, OUTCOME_CLARIFY}
)
_RESPONSE_KEYS = frozenset(
    {
        "response_schema_version",
        "action_type",
        "tool_request",
        "final_decision",
        "query_reformulation",
        "reason_code",
        "evidence_references",
        "provider_id",
        "model_version",
        "usage",
    }
)
_USAGE_KEYS = frozenset(
    {"state", "input_tokens", "output_tokens", "total_tokens"}
)
_TOOL_KEYS = frozenset({"name", "arguments"})
_DECISION_KEYS = frozenset({"outcome"})
_ID_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_CASE_ID = re.compile(r"^case1-[0-9a-f]{32}$")
_EVIDENCE_ID = re.compile(r"^ev1-[0-9a-f]{64}$")
_DOCUMENT_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCATOR = re.compile(r"^[A-Za-z0-9:._/-]{1,120}$")
_VERSION_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_ITEMS = 8
_MAX_POLICY_CHARS = 4000
_MAX_UNTRUSTED_CHARS = 16000
_MAX_REFORMULATION_CHARS = 500
_MAX_ARGUMENT_CHARS = 200
_MAX_COUNTED_TOKENS = 1_000_000


class ProviderExecutionError(Exception):
    """The provider failed after it started. The message stays static."""

    def __init__(self, usage: object = None) -> None:
        super().__init__(PROVIDER_ERROR_MESSAGE)
        self.usage = usage


class CooperativeTimeout(Exception):
    """The provider saw the deadline and returned no action."""

    def __init__(self) -> None:
        super().__init__(TIMEOUT_MESSAGE)


class CooperativeCancellation(Exception):
    """The provider saw cancellation and returned no action."""

    def __init__(self) -> None:
        super().__init__(CANCELLATION_MESSAGE)


class CancellationToken:
    """A testable cancel flag. Setting it does not kill a thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True)
class RunContext:
    run_id: str
    case_id: str | None


@dataclass(frozen=True)
class AvailableTool:
    """One tool the trusted runtime allows. Evidence text cannot add one."""

    name: str


@dataclass(frozen=True)
class EvidenceSource:
    """Source identity copied from the trusted caller. Text cannot replace it."""

    document_identity: str
    source_locator: str


@dataclass(frozen=True)
class EvidenceSummaryItem:
    evidence_id: str
    source: EvidenceSource
    representation_version: str
    transformation_version: str
    untrusted_text: str


@dataclass(frozen=True)
class UsageBudget:
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int


@dataclass(frozen=True)
class ProviderRequest:
    request_schema_version: str
    run_context: RunContext
    observation: str
    available_tools: tuple[AvailableTool, ...]
    evidence: tuple[EvidenceSummaryItem, ...]
    system_policy: str
    budget: UsageBudget
    deadline_monotonic: float
    cancellation: CancellationToken


@dataclass(frozen=True)
class ToolRequest:
    """A requested tool call. The boundary does not run it."""

    name: str
    arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProviderFinalDecision:
    """Provider opinion only. This is not a G3 evidence decision."""

    outcome: str


@dataclass(frozen=True)
class UsageAccount:
    """Known counts are integers. Unknown counts are None, never zero."""

    state: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ProviderFailure:
    type: str
    message: str


@dataclass(frozen=True)
class ProviderBoundaryResult:
    ok: bool
    boundary_version: str
    response_schema_version: str
    request: ProviderRequest | None
    started: bool
    action_type: str | None
    tool_request: ToolRequest | None
    final_decision: ProviderFinalDecision | None
    query_reformulation: str | None
    reason_code: str | None
    evidence_references: tuple[str, ...] | None
    provider_id: str
    model_version: str
    usage: UsageAccount
    error: ProviderFailure | None


@dataclass(frozen=True)
class ChannelSeparation:
    """Trusted channels copied out beside untrusted text."""

    system_policy: str
    untrusted_observation: str
    untrusted_evidence_text: tuple[str, ...]
    available_tool_names: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    deadline_monotonic: float


@dataclass(frozen=True)
class _Accepted:
    action_type: str
    tool_request: ToolRequest | None
    final_decision: ProviderFinalDecision | None
    query_reformulation: str
    reason_code: str
    evidence_references: tuple[str, ...]
    usage: UsageAccount


@dataclass(frozen=True)
class _RejectedOutput:
    error_type: str
    usage: UsageAccount


def unknown_usage() -> UsageAccount:
    return UsageAccount(USAGE_UNKNOWN, None, None, None)


def known_usage(input_tokens: int, output_tokens: int) -> UsageAccount:
    if type(input_tokens) is not int or type(output_tokens) is not int:
        raise ValueError("token counts must be integers")
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    total = input_tokens + output_tokens
    if total > _MAX_COUNTED_TOKENS:
        raise ValueError("token counts exceed the boundary account limit")
    return UsageAccount(USAGE_KNOWN, input_tokens, output_tokens, total)


def build_provider_request(
    *,
    run_id: str,
    case_id: str | None,
    observation: str,
    available_tools: tuple[AvailableTool, ...],
    evidence: tuple[EvidenceSummaryItem, ...],
    system_policy: str,
    budget: UsageBudget,
    deadline_monotonic: float,
    cancellation: CancellationToken | None = None,
) -> ProviderRequest:
    """Assemble a request without reading observation or evidence text."""

    return ProviderRequest(
        request_schema_version=PROVIDER_REQUEST_SCHEMA_VERSION,
        run_context=RunContext(run_id=run_id, case_id=case_id),
        observation=observation,
        available_tools=available_tools,
        evidence=evidence,
        system_policy=system_policy,
        budget=budget,
        deadline_monotonic=deadline_monotonic,
        cancellation=cancellation if cancellation is not None else CancellationToken(),
    )


def separate_channels(request: ProviderRequest) -> ChannelSeparation:
    """Copy trusted limits and untrusted text into separate fields."""

    return ChannelSeparation(
        system_policy=request.system_policy,
        untrusted_observation=request.observation,
        untrusted_evidence_text=tuple(item.untrusted_text for item in request.evidence),
        available_tool_names=tuple(tool.name for tool in request.available_tools),
        max_input_tokens=request.budget.max_input_tokens,
        max_output_tokens=request.budget.max_output_tokens,
        max_total_tokens=request.budget.max_total_tokens,
        deadline_monotonic=float(request.deadline_monotonic),
    )


def wait_for_release(
    request: ProviderRequest,
    clock: Callable[[], float],
    limit_s: float,
) -> None:
    """Wait until cancellation, the deadline, or this provider's own bound.

    Reaching limit_s is a provider failure. It is not a contract timeout.
    The wait returns. It does not leave a background task behind.
    """

    if type(limit_s) not in (int, float) or limit_s <= 0 or not _finite(limit_s):
        raise ProviderExecutionError(None)
    started = time.monotonic()
    while True:
        if request.cancellation.is_cancelled():
            raise CooperativeCancellation()
        current = _clock_tick(clock)
        if current >= request.deadline_monotonic:
            raise CooperativeTimeout()
        elapsed = time.monotonic() - started
        if elapsed >= limit_s:
            raise ProviderExecutionError(None)
        remaining = max(0.0, min(0.01, limit_s - elapsed))
        request.cancellation.wait(remaining)


def invoke_provider(
    provider: object,
    request: object,
    *,
    clock: Callable[[], float] | None = None,
) -> ProviderBoundaryResult:
    """Run one provider call inside the trusted boundary.

    Entry order is request shape, cancellation, deadline, then budget.
    A request that fails one of those checks does not call the provider.
    After a call starts, cancellation still wins over a passed deadline,
    and both win over provider output. Output that fails the schema, asks
    for an unknown tool or evidence id, or reports usage over the budget
    is not a success.
    """

    tick = time.monotonic if clock is None else clock
    identity = _provider_identity(provider)
    if identity is None:
        return _failure(None, "unidentified", "unidentified", False, unknown_usage(), ERROR_PROVIDER)
    provider_id, model_version = identity
    if not isinstance(request, ProviderRequest) or not _request_ok(request):
        kept = request if isinstance(request, ProviderRequest) else None
        return _failure(
            kept, provider_id, model_version, False, unknown_usage(), ERROR_INVALID_REQUEST
        )
    if request.cancellation.is_cancelled():
        return _failure(
            request, provider_id, model_version, False, unknown_usage(), ERROR_CANCELLATION
        )
    if _past_deadline(tick, request):
        return _failure(
            request, provider_id, model_version, False, unknown_usage(), ERROR_TIMEOUT
        )
    if not _budget_can_start(request.budget):
        return _failure(
            request, provider_id, model_version, False, unknown_usage(), ERROR_BUDGET
        )
    produce = getattr(provider, "produce", None)
    if not callable(produce):
        return _failure(
            request, provider_id, model_version, False, unknown_usage(), ERROR_PROVIDER
        )

    try:
        raw = produce(request, tick)
    except CooperativeCancellation:
        return _failure(
            request, provider_id, model_version, True, unknown_usage(), ERROR_CANCELLATION
        )
    except CooperativeTimeout:
        if request.cancellation.is_cancelled():
            return _failure(
                request, provider_id, model_version, True, unknown_usage(), ERROR_CANCELLATION
            )
        return _failure(
            request, provider_id, model_version, True, unknown_usage(), ERROR_TIMEOUT
        )
    except ProviderExecutionError as exc:
        usage = _usage_from_provider(exc.usage)
        return _failure_after_start(request, provider_id, model_version, tick, usage)
    except Exception:
        return _failure_after_start(
            request, provider_id, model_version, tick, unknown_usage()
        )

    peeked = _surface_usage(raw)
    if request.cancellation.is_cancelled():
        return _failure(
            request, provider_id, model_version, True, peeked, ERROR_CANCELLATION
        )
    if _past_deadline(tick, request):
        return _failure(request, provider_id, model_version, True, peeked, ERROR_TIMEOUT)
    classified = _classify(raw, request, provider_id, model_version)
    if isinstance(classified, _RejectedOutput):
        return _failure(
            request,
            provider_id,
            model_version,
            True,
            classified.usage,
            classified.error_type,
        )
    return ProviderBoundaryResult(
        ok=True,
        boundary_version=PROVIDER_BOUNDARY_VERSION,
        response_schema_version=PROVIDER_RESPONSE_SCHEMA_VERSION,
        request=request,
        started=True,
        action_type=classified.action_type,
        tool_request=classified.tool_request,
        final_decision=classified.final_decision,
        query_reformulation=classified.query_reformulation,
        reason_code=classified.reason_code,
        evidence_references=classified.evidence_references,
        provider_id=provider_id,
        model_version=model_version,
        usage=classified.usage,
        error=None,
    )


def _failure_after_start(
    request: ProviderRequest,
    provider_id: str,
    model_version: str,
    clock: Callable[[], float],
    usage: UsageAccount,
) -> ProviderBoundaryResult:
    if request.cancellation.is_cancelled():
        error_type = ERROR_CANCELLATION
    elif _past_deadline(clock, request):
        error_type = ERROR_TIMEOUT
    else:
        error_type = ERROR_PROVIDER
    return _failure(request, provider_id, model_version, True, usage, error_type)


def _failure(
    request: ProviderRequest | None,
    provider_id: str,
    model_version: str,
    started: bool,
    usage: UsageAccount,
    error_type: str,
) -> ProviderBoundaryResult:
    return ProviderBoundaryResult(
        ok=False,
        boundary_version=PROVIDER_BOUNDARY_VERSION,
        response_schema_version=PROVIDER_RESPONSE_SCHEMA_VERSION,
        request=request,
        started=started,
        action_type=None,
        tool_request=None,
        final_decision=None,
        query_reformulation=None,
        reason_code=None,
        evidence_references=None,
        provider_id=provider_id,
        model_version=model_version,
        usage=usage,
        error=ProviderFailure(type=error_type, message=_MESSAGES[error_type]),
    )


def _provider_identity(provider: object) -> tuple[str, str] | None:
    provider_id = getattr(provider, "provider_id", None)
    model_version = getattr(provider, "model_version", None)
    if type(provider_id) is not str or type(model_version) is not str:
        return None
    if _ID_TOKEN.fullmatch(provider_id) is None or _ID_TOKEN.fullmatch(model_version) is None:
        return None
    return provider_id, model_version


def _request_ok(request: ProviderRequest) -> bool:
    if request.request_schema_version != PROVIDER_REQUEST_SCHEMA_VERSION:
        return False
    context = request.run_context
    if not isinstance(context, RunContext):
        return False
    if type(context.run_id) is not str or _RUN_ID.fullmatch(context.run_id) is None:
        return False
    if context.case_id is not None and (
        type(context.case_id) is not str or _CASE_ID.fullmatch(context.case_id) is None
    ):
        return False
    if type(request.observation) is not str or len(request.observation) > _MAX_UNTRUSTED_CHARS:
        return False
    if not _tools_ok(request.available_tools):
        return False
    if not _evidence_ok(request.evidence):
        return False
    if (
        type(request.system_policy) is not str
        or not request.system_policy
        or len(request.system_policy) > _MAX_POLICY_CHARS
        or "\x00" in request.system_policy
    ):
        return False
    if not isinstance(request.budget, UsageBudget) or not _budget_shape(request.budget):
        return False
    if not _finite(request.deadline_monotonic):
        return False
    return isinstance(request.cancellation, CancellationToken)


def _tools_ok(tools: object) -> bool:
    if type(tools) is not tuple or len(tools) > _MAX_ITEMS:
        return False
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, AvailableTool) or type(tool.name) is not str:
            return False
        if _TOOL_NAME.fullmatch(tool.name) is None or tool.name in names:
            return False
        names.add(tool.name)
    return True


def _evidence_ok(evidence: object) -> bool:
    if type(evidence) is not tuple or len(evidence) > _MAX_ITEMS:
        return False
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, EvidenceSummaryItem):
            return False
        if type(item.evidence_id) is not str or _EVIDENCE_ID.fullmatch(item.evidence_id) is None:
            return False
        if item.evidence_id in seen:
            return False
        seen.add(item.evidence_id)
        source = item.source
        if not isinstance(source, EvidenceSource):
            return False
        if (
            type(source.document_identity) is not str
            or _DOCUMENT_IDENTITY.fullmatch(source.document_identity) is None
        ):
            return False
        if type(source.source_locator) is not str or _LOCATOR.fullmatch(source.source_locator) is None:
            return False
        if not _version_ok(item.representation_version) or not _version_ok(
            item.transformation_version
        ):
            return False
        if type(item.untrusted_text) is not str or len(item.untrusted_text) > _MAX_UNTRUSTED_CHARS:
            return False
    return True


def _version_ok(value: object) -> bool:
    return type(value) is str and _VERSION_TOKEN.fullmatch(value) is not None


def _budget_shape(budget: UsageBudget) -> bool:
    for value in (
        budget.max_input_tokens,
        budget.max_output_tokens,
        budget.max_total_tokens,
    ):
        if type(value) is not int or value < 0 or value > _MAX_COUNTED_TOKENS:
            return False
    return True


def _budget_can_start(budget: UsageBudget) -> bool:
    return (
        budget.max_input_tokens >= 1
        and budget.max_output_tokens >= 1
        and budget.max_total_tokens >= 1
    )


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _past_deadline(clock: Callable[[], float], request: ProviderRequest) -> bool:
    current = _clock_tick(clock)
    return current >= request.deadline_monotonic


def _clock_tick(clock: Callable[[], float]) -> float:
    try:
        current = clock()
    except Exception:
        return float("inf")
    if not _finite(current):
        return float("inf")
    return float(current)


def _classify(
    raw: object,
    request: ProviderRequest,
    provider_id: str,
    model_version: str,
) -> _Accepted | _RejectedOutput:
    peeked = _surface_usage(raw)
    if type(raw) is not dict or set(raw) != _RESPONSE_KEYS:
        return _RejectedOutput(ERROR_MALFORMED, peeked)
    usage = _parse_usage(raw.get("usage"))
    if usage is None or usage.state != USAGE_KNOWN:
        return _RejectedOutput(ERROR_MALFORMED, unknown_usage())
    if (
        raw.get("response_schema_version") != PROVIDER_RESPONSE_SCHEMA_VERSION
        or raw.get("provider_id") != provider_id
        or raw.get("model_version") != model_version
    ):
        return _RejectedOutput(ERROR_MALFORMED, usage)
    reformulation = raw.get("query_reformulation")
    reason_code = raw.get("reason_code")
    if (
        type(reformulation) is not str
        or len(reformulation) > _MAX_REFORMULATION_CHARS
        or "\x00" in reformulation
        or type(reason_code) is not str
        or _REASON.fullmatch(reason_code) is None
    ):
        return _RejectedOutput(ERROR_MALFORMED, usage)
    references = _reference_list(raw.get("evidence_references"))
    if references is None:
        return _RejectedOutput(ERROR_MALFORMED, usage)
    action_type = raw.get("action_type")
    if action_type == ACTION_TOOL_REQUEST:
        tool = _tool_request(raw.get("tool_request"))
        if tool is None or raw.get("final_decision") is not None:
            return _RejectedOutput(ERROR_MALFORMED, usage)
        if not _action_authorized(tool.name, references, request):
            return _RejectedOutput(ERROR_INVALID_ACTION, usage)
        if _over_budget(usage, request.budget):
            return _RejectedOutput(ERROR_BUDGET, usage)
        return _Accepted(
            action_type=ACTION_TOOL_REQUEST,
            tool_request=tool,
            final_decision=None,
            query_reformulation=reformulation,
            reason_code=reason_code,
            evidence_references=references,
            usage=usage,
        )
    if action_type == ACTION_FINAL_DECISION:
        decision = _final_decision(raw.get("final_decision"))
        if decision is None or raw.get("tool_request") is not None:
            return _RejectedOutput(ERROR_MALFORMED, usage)
        if not _action_authorized(None, references, request):
            return _RejectedOutput(ERROR_INVALID_ACTION, usage)
        if _over_budget(usage, request.budget):
            return _RejectedOutput(ERROR_BUDGET, usage)
        return _Accepted(
            action_type=ACTION_FINAL_DECISION,
            tool_request=None,
            final_decision=decision,
            query_reformulation=reformulation,
            reason_code=reason_code,
            evidence_references=references,
            usage=usage,
        )
    return _RejectedOutput(ERROR_MALFORMED, usage)


def _tool_request(value: object) -> ToolRequest | None:
    if type(value) is not dict or set(value) != _TOOL_KEYS:
        return None
    name = value.get("name")
    arguments = value.get("arguments")
    if type(name) is not str or _TOOL_NAME.fullmatch(name) is None:
        return None
    if type(arguments) is not dict or len(arguments) > _MAX_ITEMS:
        return None
    pairs: list[tuple[str, str]] = []
    for key, argument in arguments.items():
        if type(key) is not str or _TOOL_NAME.fullmatch(key) is None:
            return None
        if type(argument) is not str or len(argument) > _MAX_ARGUMENT_CHARS or "\x00" in argument:
            return None
        pairs.append((key, argument))
    return ToolRequest(name=name, arguments=tuple(sorted(pairs)))


def _final_decision(value: object) -> ProviderFinalDecision | None:
    if type(value) is not dict or set(value) != _DECISION_KEYS:
        return None
    outcome = value.get("outcome")
    if type(outcome) is not str or outcome not in _OUTCOMES:
        return None
    return ProviderFinalDecision(outcome=outcome)


def _reference_list(value: object) -> tuple[str, ...] | None:
    if type(value) is not list or len(value) > _MAX_ITEMS:
        return None
    seen: set[str] = set()
    references: list[str] = []
    for item in value:
        if type(item) is not str or _EVIDENCE_ID.fullmatch(item) is None or item in seen:
            return None
        seen.add(item)
        references.append(item)
    return tuple(references)


def _action_authorized(
    tool_name: str | None,
    references: tuple[str, ...],
    request: ProviderRequest,
) -> bool:
    known_ids = {item.evidence_id for item in request.evidence}
    if any(evidence_id not in known_ids for evidence_id in references):
        return False
    if tool_name is None:
        return True
    return tool_name in {tool.name for tool in request.available_tools}


def _over_budget(usage: UsageAccount, budget: UsageBudget) -> bool:
    return (
        usage.input_tokens is None
        or usage.output_tokens is None
        or usage.total_tokens is None
        or usage.input_tokens > budget.max_input_tokens
        or usage.output_tokens > budget.max_output_tokens
        or usage.total_tokens > budget.max_total_tokens
    )


def _surface_usage(raw: object) -> UsageAccount:
    if type(raw) is not dict:
        return unknown_usage()
    parsed = _parse_usage(raw.get("usage"))
    if parsed is None:
        return unknown_usage()
    return parsed


def _parse_usage(value: object) -> UsageAccount | None:
    if type(value) is not dict or set(value) != _USAGE_KEYS:
        return None
    state = value.get("state")
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    total_tokens = value.get("total_tokens")
    if state == USAGE_UNKNOWN:
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return unknown_usage()
        return None
    if state != USAGE_KNOWN:
        return None
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or type(total_tokens) is not int
    ):
        return None
    if min(input_tokens, output_tokens, total_tokens) < 0:
        return None
    if max(input_tokens, output_tokens, total_tokens) > _MAX_COUNTED_TOKENS:
        return None
    if input_tokens + output_tokens != total_tokens:
        return None
    return UsageAccount(USAGE_KNOWN, input_tokens, output_tokens, total_tokens)


def _usage_from_provider(value: object) -> UsageAccount:
    if not isinstance(value, UsageAccount):
        return unknown_usage()
    if value.state == USAGE_UNKNOWN:
        if (
            value.input_tokens is None
            and value.output_tokens is None
            and value.total_tokens is None
        ):
            return value
        return unknown_usage()
    if value.state != USAGE_KNOWN:
        return unknown_usage()
    parsed = _parse_usage(
        {
            "state": value.state,
            "input_tokens": value.input_tokens,
            "output_tokens": value.output_tokens,
            "total_tokens": value.total_tokens,
        }
    )
    if parsed is None:
        return unknown_usage()
    return parsed
