"""Support workflow state machine for one case.

Events are append-only. The case starts at opened when it has no events.
opened may move to investigating. investigating may move to resolved or
abstained. Resolved cites one evidence id already stored on that case.
Abstained cites the decision reference derived from the stored abstain
decision. This module does not retrieve, decide, or rewrite case evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .cases import CaseRecord, CaseStoreError, read_case
from .evidence import DECISION_ABSTAIN


WORKFLOW_EVENT_SCHEMA_VERSION = "1"
DECISION_REFERENCE_SCHEMA_VERSION = "1"
STATE_OPENED = "opened"
STATE_INVESTIGATING = "investigating"
STATE_RESOLVED = "resolved"
STATE_ABSTAINED = "abstained"
WORKFLOW_NOT_DURABLE = "workflow_not_durable"
WORKFLOW_INVALID = "workflow_invalid"
WORKFLOW_LOCKED = "workflow_locked"
WORKFLOW_NOT_DURABLE_MESSAGE = "workflow 不能在调用者未提交的事务中保证持久化。"
WORKFLOW_INVALID_MESSAGE = "workflow 拒绝不满足状态迁移合同的变更。"
WORKFLOW_LOCKED_MESSAGE = "workflow 在数据库锁定时拒绝状态迁移。"
_TRANSITIONS = frozenset(
    {
        (STATE_OPENED, STATE_INVESTIGATING),
        (STATE_INVESTIGATING, STATE_RESOLVED),
        (STATE_INVESTIGATING, STATE_ABSTAINED),
    }
)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


class WorkflowError(Exception):
    """The workflow refused the operation. No partial event is committed."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class WorkflowEvent:
    sequence: int
    from_state: str
    to_state: str
    evidence_id: str | None
    decision_reference: str | None
    recorded_at: str


@dataclass(frozen=True)
class WorkflowRecord:
    case_id: str
    state: str
    events: tuple[WorkflowEvent, ...]


def workflow_decision_reference(case: CaseRecord) -> str:
    """Return the reference implied by one already stored case decision.

    The digest covers only the persisted decision fields. It is not a second
    decision and it does not copy evidence text.
    """

    payload = {
        "case_id": case.case_id,
        "decision_type": case.decision_type,
        "evidence_ids": [item.evidence_id for item in case.evidence],
        "packet_schema_version": case.packet_schema_version,
        "reason_codes": list(case.reason_codes),
        "reference_schema_version": DECISION_REFERENCE_SCHEMA_VERSION,
        "retrieval_state": case.retrieval_state,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "dec1-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_workflow(connection: sqlite3.Connection, case_id: str) -> WorkflowRecord:
    """Replay one case workflow. Live documents are not consulted."""

    case = read_case(connection, case_id)
    events = _load_events(connection, case.case_id)
    state = _replay(case, events)
    return WorkflowRecord(case_id=case.case_id, state=state, events=events)


def transition_workflow(
    connection: sqlite3.Connection,
    case_id: str,
    to_state: str,
    *,
    evidence_id: str | None = None,
    decision_reference: str | None = None,
) -> WorkflowRecord:
    """Append one legal transition and commit only that event."""

    _require_durable(connection)
    if not isinstance(to_state, str):
        _invalid()
    _begin_immediate(connection)
    try:
        case = read_case(connection, case_id)
        events = _load_events(connection, case.case_id)
        current = _replay(case, events)
        _require_move(case, current, to_state, evidence_id, decision_reference)
        connection.execute(
            """INSERT INTO workflow_events (
                   case_id, sequence, from_state, to_state, evidence_id,
                   decision_reference, recorded_at, event_schema_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case.case_id,
                len(events) + 1,
                current,
                to_state,
                evidence_id,
                decision_reference,
                _now(),
                WORKFLOW_EVENT_SCHEMA_VERSION,
            ),
        )
        connection.commit()
    except WorkflowError:
        connection.rollback()
        raise
    except CaseStoreError:
        connection.rollback()
        raise
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower():
            _locked()
        _invalid()
    except sqlite3.Error:
        connection.rollback()
        _invalid()
    return read_workflow(connection, case_id)


def _load_events(connection: sqlite3.Connection, case_id: str) -> tuple[WorkflowEvent, ...]:
    rows = connection.execute(
        """SELECT sequence, from_state, to_state, evidence_id,
                  decision_reference, recorded_at
           FROM workflow_events
           WHERE case_id = ?
           ORDER BY sequence, id""",
        (case_id,),
    ).fetchall()
    events: list[WorkflowEvent] = []
    for row in rows:
        sequence = row["sequence"]
        recorded_at = row["recorded_at"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            _invalid()
        if not isinstance(recorded_at, str) or _TIMESTAMP.fullmatch(recorded_at) is None:
            _invalid()
        events.append(
            WorkflowEvent(
                sequence=sequence,
                from_state=_required_text(row["from_state"]),
                to_state=_required_text(row["to_state"]),
                evidence_id=_optional_text(row["evidence_id"]),
                decision_reference=_optional_text(row["decision_reference"]),
                recorded_at=recorded_at,
            )
        )
    return tuple(events)


def _replay(case: CaseRecord, events: tuple[WorkflowEvent, ...]) -> str:
    current = STATE_OPENED
    for index, event in enumerate(events, start=1):
        if event.sequence != index or event.from_state != current:
            _invalid()
        _require_move(case, current, event.to_state, event.evidence_id, event.decision_reference)
        current = event.to_state
    return current


def _require_move(
    case: CaseRecord,
    current: str,
    to_state: str,
    evidence_id: str | None,
    decision_reference: str | None,
) -> None:
    if (current, to_state) not in _TRANSITIONS:
        _invalid()
    if to_state == STATE_RESOLVED:
        _require_case_evidence(case, evidence_id)
        if decision_reference is not None:
            _invalid()
        return
    if to_state == STATE_ABSTAINED:
        _require_abstain_reference(case, decision_reference)
        if evidence_id is not None:
            _invalid()
        return
    if evidence_id is not None or decision_reference is not None:
        _invalid()


def _require_case_evidence(case: CaseRecord, evidence_id: str | None) -> None:
    if not isinstance(evidence_id, str):
        _invalid()
    if evidence_id not in {item.evidence_id for item in case.evidence}:
        _invalid()


def _require_abstain_reference(case: CaseRecord, decision_reference: str | None) -> None:
    # The stored decision stays as it was written. A supported or conflict
    # case cannot satisfy this check.
    if case.decision_type != DECISION_ABSTAIN or not isinstance(decision_reference, str):
        _invalid()
    if decision_reference != workflow_decision_reference(case):
        _invalid()


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _require_durable(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        _not_durable()


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            _locked()
        _invalid()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _invalid() -> None:
    raise WorkflowError(WORKFLOW_INVALID, WORKFLOW_INVALID_MESSAGE)


def _locked() -> None:
    raise WorkflowError(WORKFLOW_LOCKED, WORKFLOW_LOCKED_MESSAGE)


def _not_durable() -> None:
    raise WorkflowError(WORKFLOW_NOT_DURABLE, WORKFLOW_NOT_DURABLE_MESSAGE)
