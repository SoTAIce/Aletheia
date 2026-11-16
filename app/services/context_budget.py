"""The token budget the context parts share (stage 5c): window, output reserve, safety margin, counter.

The default counter is a mixed-language estimate, not a provider tokenizer. No model window is
inferred from a model's name; configure it explicitly (the model profile does, D-078).
"""

from dataclasses import dataclass, field
from typing import Callable

from app.services.rag_context_builder import ContextBuilder


@dataclass(slots=True)
class ContextBudget:
    window_tokens: int = 32768
    output_tokens: int = 2048
    safety_tokens: int = 1024
    count: Callable[[str], int] = field(default=ContextBuilder.estimate_tokens)

    def __post_init__(self):
        for name, minimum in (('window_tokens', 1), ('output_tokens', 1), ('safety_tokens', 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if self.output_tokens + self.safety_tokens >= self.window_tokens:
            raise ValueError('Output reserve and safety must be smaller than window_tokens')

    @property
    def input_tokens(self) -> int:
        return self.window_tokens - self.output_tokens - self.safety_tokens
