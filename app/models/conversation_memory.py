from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from .base_memory import BaseMemory


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class ConversationMessage:
    role: MessageRole
    content: str
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )


@dataclass
class ConversationTurn:
    messages: list[ConversationMessage]
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("conversation turn must not be empty")
        if not all(
            isinstance(message, ConversationMessage)
            for message in self.messages
        ):
            raise TypeError("conversation turn contains an invalid message type")


class ConversationMemory(BaseMemory):
    def __init__(
        self,
        user_id: str,
        max_turns: int = 20,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be greater than zero")

        super().__init__(user_id)
        self._max_turns = max_turns
        self._turns: deque[ConversationTurn] = deque(maxlen=max_turns)

    def __len__(self) -> int:
        """Return the number of complete conversation turns in memory."""
        return len(self._turns)

    @property
    def is_full(self) -> bool:
        return len(self._turns) >= self._max_turns

    @property
    def remaining_capacity(self) -> int:
        return self._max_turns - len(self._turns)

    def add(self, turn: ConversationTurn) -> None:
        """Add one complete conversation turn."""
        if not isinstance(turn, ConversationTurn):
            raise TypeError("turn must be a ConversationTurn")
        self._turns.append(turn)

    def add_turns(self, turns: list[ConversationTurn]) -> None:
        """Add multiple complete conversation turns."""
        if not all(isinstance(turn, ConversationTurn) for turn in turns):
            raise TypeError("turns contains an invalid type")
        self._turns.extend(turns)

    def pop(self) -> ConversationTurn | None:
        """Remove and return the most recent conversation turn."""
        if not self._turns:
            return None
        return self._turns.pop()

    def remove_last(self, count: int = 1) -> None:
        """Remove the most recent complete conversation turns."""
        if count < 0:
            raise ValueError("count must not be negative")
        for _ in range(min(count, len(self._turns))):
            self._turns.pop()

    def remove_oldest_turn(self) -> ConversationTurn | None:
        """Remove and return the oldest complete conversation turn."""
        if not self._turns:
            return None
        return self._turns.popleft()

    def retrieve(self, k: int = 20) -> list[ConversationTurn]:
        """Return the k most recent complete conversation turns."""
        if k < 0:
            raise ValueError("k must not be negative")
        if k == 0:
            return []
        return list(self._turns)[-k:]

    def retrieve_turns(self, k: int = 20) -> list[ConversationTurn]:
        """Explicit alias for retrieving complete conversation turns."""
        return self.retrieve(k)

    def retrieve_messages(self, k: int = 20) -> list[ConversationMessage]:
        """Flatten the k most recent turns into chronological messages."""
        return [
            message
            for turn in self.retrieve(k)
            for message in turn.messages
        ]

    def clear(self) -> None:
        self._turns.clear()
