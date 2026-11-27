"""Static checks for web run notifications (D-108)."""

import re
import shutil
import subprocess

import pytest

from app.api.app import STATIC


def _read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding='utf-8')


def test_index_has_notify_toggle_off_by_default():
    html = _read_static('index.html')
    match = re.search(r'<button\b[^>]*\bid="toggle-notify"[^>]*>', html)
    assert match is not None
    assert 'aria-pressed="false"' in match.group(0)


def test_notification_permission_and_payload_are_constrained():
    script = _read_static('app.js')
    assert script.count('requestPermission') == 1
    assert script.count('new Notification(') == 1

    lines = script.splitlines()
    hits = [i for i, line in enumerate(lines) if 'new Notification(' in line]
    assert len(hits) == 1
    idx = hits[0]
    window = '\n'.join(lines[max(0, idx - 3):min(len(lines), idx + 4)])
    for forbidden in ('event.', 'content', 'question', 'message', 'preview', 'title:'):
        assert forbidden not in window, forbidden


def test_run_notices_table_has_only_the_four_fixed_events():
    script = _read_static('app.js')
    match = re.search(r'const\s+RUN_NOTICES\s*=\s*\{(.*?)\};', script, re.DOTALL)
    assert match is not None
    body = match.group(1)
    keys = re.findall(r'^\s*([a-z_]+)\s*:', body, re.MULTILINE)
    assert keys == ['completed', 'blocked', 'failed', 'approval_required']
    assert 'cancelled' not in body


def test_visibility_reset_and_session_storage_only():
    script = _read_static('app.js')
    assert 'visibilitychange' in script
    assert 'NOTIFY_KEY' in script
    assert 'localStorage' not in script


def test_app_js_passes_node_syntax_check():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    result = subprocess.run(
        [node, '--check', str(STATIC / 'app.js')],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
