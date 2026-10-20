from dataclasses import dataclass, field
from datetime import datetime,timezone
from enum import StrEnum
from uuid import uuid4

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


