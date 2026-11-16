"""Execution reliability that outlived plan mode (stage 4c): every failure kind has a Chinese
message (D-033) and the sandbox step deadline (D-033). The envelope repair, unavailable-tool and
side-effect tests of the plan path went with it; the loop path is covered by tests/test_agent_loop.py
and the LoopRun golden files (tests/test_loop_run_equivalence.py)."""

from types import SimpleNamespace

from app.agent.runtime import FAILURE_MESSAGES


def test_every_failure_kind_has_a_chinese_message():
    assert all(text and not text.isascii() for text in FAILURE_MESSAGES.values())


def test_a_task_deadline_shorter_than_a_sandbox_call_is_refused_at_start_up(monkeypatch):
    # Stage 5b: the deadline is no longer widened behind the user's back (D-033).
    from app.agent.__main__ import deadline_problem
    from app.agent.delegation import DelegationOptions
    from app.config import config
    from app.tools.sandbox.executor import host_call_timeout_seconds
    call = host_call_timeout_seconds()
    assert deadline_problem(False, DelegationOptions(timeout_seconds=1)) is None
    assert deadline_problem(True, DelegationOptions()) is None          # defaults: 480 / 180 > one call
    short = deadline_problem(True, DelegationOptions(timeout_seconds=call))
    assert 'team' in short and 'single' not in short and '--team-timeout' in short
    monkeypatch.setattr(config, 'agent_task_timeout_seconds', call - 1)
    assert 'single' in deadline_problem(True, None)
