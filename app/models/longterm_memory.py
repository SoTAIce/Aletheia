from dataclasses import dataclass, field
from datetime import datetime,timezone
from enum import StrEnum
from uuid import uuid4

class _AllPersonas:
    """The scope that filters nothing: every memory of the user, whatever persona it belongs to."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return 'ALL_PERSONAS'


# A persona scope (persona-memory, docs/persona_memory_plan.md): ALL_PERSONAS filters nothing,
# None means shared memories only, a persona id means shared ones plus that persona's own.
ALL_PERSONAS = _AllPersonas()


def check_scope(scope) -> None:
    if scope is not ALL_PERSONAS and scope is not None and (
            not isinstance(scope, str) or not scope.strip() or len(scope) > 100):
        raise ValueError("persona scope must be ALL_PERSONAS, None or a persona id")


def in_scope(persona_id: str | None, scope) -> bool:
    """Whether a memory that belongs to persona_id (None: shared) is visible in scope."""
    if scope is ALL_PERSONAS or persona_id is None:
        return True
    return persona_id == scope


class MemoryCategory(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    PROJECT = "project"
    EXPERIENCE = "experience"

@dataclass(frozen=True, slots=True)
class MemoryRecord:
    user_id: str
    content: str
    category: MemoryCategory
    source: str

    memory_id: str = field(
        default_factory = lambda: str(uuid4())
    )

    source_id: str | None = None

    version: int = 1

    created_at: datetime = field(
        default_factory = lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory = lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime | None = None
    # The persona the memory belongs to; None is shared by every persona (persona-memory).
    persona_id: str | None = None

@dataclass(frozen=True, slots=True)
class MemoryHit:
    record: MemoryRecord
    score: float

@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    content: str
    category: MemoryCategory
    source: str
    source_id: str | None = None

class MemoryAction(StrEnum):
    ADD = "add"
    SKIP = "skip"
    UPDATE = "update"
    CONFLICT = "conflict"

@dataclass(frozen=True, slots=True)
class MemoryDecision:
    action: MemoryAction
    target_memory_id: str | None = None
    reason: str = ""


