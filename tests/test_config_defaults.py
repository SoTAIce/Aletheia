"""The default per-call output cap fits a whole module and still leaves room for input (D-036)."""

from app.config import Settings
from app.services.context_manager import ContextManager


def test_default_output_cap_is_large_enough_for_whole_files():
    fields = Settings.model_fields
    window = fields['context_window_tokens'].default
    output = fields['context_output_tokens'].default
    safety = fields['context_safety_tokens'].default
    assert output == 8192
    assert window - output - safety >= 16384   # input headroom stays at least half the window
    manager = ContextManager(window_tokens=window, output_tokens=output, safety_tokens=safety)
    assert manager.output_tokens == output
