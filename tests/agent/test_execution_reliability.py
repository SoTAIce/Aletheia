"""Execution reliability that outlived plan mode (stage 4c): every failure kind has a Chinese
message (D-033) and the sandbox step deadline (D-033). The envelope repair, unavailable-tool and
side-effect tests of the plan path went with it; the loop path is covered by tests/test_agent_loop.py
and the LoopRun golden files (tests/test_loop_run_equivalence.py)."""

from types import SimpleNamespace

from app.agent.runtime import FAILURE_MESSAGES


def test_every_failure_kind_has_a_chinese_message():
    assert all(text and not text.isascii() for text in FAILURE_MESSAGES.values())


def test_the_step_deadline_outlasts_a_sandbox_call():
    from app.agent.__main__ import step_timeout_for
    from app.tools.sandbox.executor import host_call_timeout_seconds
    assert step_timeout_for(False, None) is None
    assert step_timeout_for(True, None) >= host_call_timeout_seconds() + 30
    assert step_timeout_for(True, SimpleNamespace(timeout_seconds=400)) == 405
