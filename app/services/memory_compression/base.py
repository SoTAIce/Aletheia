from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.conversation_memory import ConversationTurn


class ConversationCompressor(ABC):
    @abstractmethod
    async def compress(
        self,
        previous_summary: str | None,
        turns: Sequence[ConversationTurn],
    ) -> str:
        """Combine the previous summary and turns into a new summary."""
        raise NotImplementedError
