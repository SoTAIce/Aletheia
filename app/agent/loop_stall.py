"""Notice a tool loop going round in circles (docs/loop_stall_plan.md, D-092).

A round is stale when every tool it ran only read, and each read was either an exact repeat of an
earlier call or touched a file whose whole text the model already has (a read_file without a range
whose result reached the model whole). A search over a directory is stale when its result shows it
could only have looked at such files, and a listing of a directory already listed in full is stale
whatever its paging (plan section 11). After `rounds` stale rounds in a row the loop reminds the
model once; after as many more it wraps up as if the tool budget were used up. A whole read that
returns exactly the recorded text is answered with a pointer to the earlier result instead.

Any write, run or external call forgets everything recorded: files may have changed. The record
holds paths, call ids, argument keys and digests of tool results, never model text, and lives in
memory for one loop only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath

READ_FILE = 'read_file'
SEARCH_FILES = 'search_files'
LIST_DIRECTORY = 'list_directory'
# The directories search_files never walks (app/tools/filesystem/file_access.py, checked by a test).
SEARCH_SKIP_DIRS = frozenset({'__pycache__', 'node_modules', 'site-packages', 'venv', '.venv'})
RANGE_KEYS = ('start_line', 'max_lines', 'offset', 'limit')
STALL_REMINDER = ('HOST: your last {rounds} tool rounds only re-read or searched files whose full text is '
                  'already in your results. Answer now from what you have. Call a tool only for information '
                  'that is not in your results yet.')
STALL_WRAP_UP = ('HOST: you kept re-reading files whose full text is already in your results, so no more '
                 'tools can be called in this task. Reply now with the final answer from the results you '
                 'already have, and say what is missing.')
UNCHANGED_FIELD = 'unchanged_since'


def unchanged_note(call_id: str) -> str:
    return (f'HOST: unchanged since your full read of this file (tool call {call_id}); '
            'its full text is in that result.')


def _path(arguments) -> str | None:
    value = arguments.get('path') if isinstance(arguments, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return posixpath.normpath(value.strip().replace('\\', '/'))


def _key(call) -> str:
    return call.tool_name + '\0' + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _whole(call) -> bool:
    return (call.tool_name == READ_FILE and isinstance(call.arguments, dict)
            and not any(call.arguments.get(key) is not None for key in RANGE_KEYS))


def _payload(content) -> dict | None:
    if isinstance(content, dict):
        return content
    try:
        value = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _under(path: str, directory: str) -> bool:
    return directory == '.' or path == directory or path.startswith(directory.rstrip('/') + '/')


def _plain(path: str) -> bool:
    """A relative path a directory search would walk: no hidden or skipped directory on the way."""
    parts = path.split('/')
    return (not path.startswith('/') and ':' not in path
            and not any(part == '..' or part.startswith('.') or part in SEARCH_SKIP_DIRS for part in parts))


def _digest(text) -> str:
    return hashlib.sha256(str(text).encode('utf-8', 'surrogatepass')).hexdigest()


class StallWatch:
    """One loop's record of what the model already has; `effect(name)` gives a tool's effect."""

    def __init__(self, rounds: int, wrap_up: bool, effect):
        self.limit, self.wrap_up, self._effect = rounds, wrap_up, effect
        self._whole: dict[str, tuple[str, str]] = {}   # path -> (call id of the whole read, digest)
        self._keys: set[str] = set()
        self._listed: set[str] = set()                  # directories listed in full
        self.streak = 0
        self.reminded = self.stalled = False
        self._counted = self._stale = False

    @property
    def on(self) -> bool:
        return self.limit > 0

    def start_round(self) -> None:
        self._counted, self._stale = False, True

    def unchanged(self, call, result) -> str | None:
        """The call id of the earlier whole read when this whole read returned exactly its text."""
        if not self.on or not _whole(call) or not result.success:
            return None
        seen = self._whole.get(_path(call.arguments))
        return seen[0] if seen is not None and seen[1] == _digest(result.content) else None

    def other(self) -> None:
        """A call the host ran itself (a host tool): not a read of the workspace."""
        if self.on:
            self._counted, self._stale = True, False

    def note(self, call, result, view_whole: bool, unchanged: bool) -> None:
        """Record one executed call of this round, after its result is known."""
        if not self.on:
            return
        self._counted = True
        if self._effect(call.tool_name) != 'read':
            # A write, run or external call may have changed files: forget what was read.
            self._stale = False
            self._whole.clear()
            self._keys.clear()
            self._listed.clear()
            return
        key, path = _key(call), _path(call.arguments)
        seen = self._whole.get(path)
        if _whole(call) and seen is not None:
            stale = unchanged
        else:
            stale = key in self._keys or seen is not None or self._known(call, result, path or '.')
        if not stale:
            self._stale = False
        self._keys.add(key)
        if _whole(call) and result.success and view_whole and not unchanged and path is not None:
            self._whole[path] = (call.call_id, _digest(result.content))
        if call.tool_name == LIST_DIRECTORY and result.success:
            listing = _payload(result.content)
            if listing is not None and listing.get('offset') == 0 and listing.get('has_more') is False:
                self._listed.add(path or '.')

    def _known(self, call, result, directory: str) -> bool:
        """True when a directory listing or search could only show what the model already has."""
        if not result.success:
            return False
        if call.tool_name == LIST_DIRECTORY:
            return directory in self._listed
        if call.tool_name != SEARCH_FILES:
            return False
        found = _payload(result.content)
        pattern = call.arguments.get('glob') if isinstance(call.arguments, dict) else None
        if (found is None or found.get('truncated') is not False or not isinstance(found.get('matches'), list)
                or type(found.get('files_searched')) is not int
                or (pattern is not None and not isinstance(pattern, str))):
            return False
        for match in found['matches']:
            if not isinstance(match, dict) or _path(match) not in self._whole:
                return False
        scope = [path for path in self._whole if _under(path, directory)]
        if not all(_plain(path) for path in scope) or len({path.casefold() for path in scope}) != len(scope):
            return False                   # a path the search would skip, or one file under two spellings
        # The search walks every plain file under the directory, so it saw each whole-read one; when it
        # saw no more files than that, it saw nothing else.
        wanted = sum(1 for path in scope if pattern is None or fnmatch.fnmatch(posixpath.basename(path), pattern)
                     or fnmatch.fnmatch(path, pattern))
        return found['files_searched'] == wanted

    def end_round(self) -> str | None:
        """'reminder' or 'wrap_up' when this round completes a stale streak, else None."""
        if not self.on or not self._counted:
            return None                    # a round of control tools only neither counts nor breaks a streak
        self.streak = self.streak + 1 if self._stale else 0
        if self.streak < self.limit:
            return None
        self.streak = 0
        if not self.reminded:
            self.reminded = True
            return 'reminder'
        if self.wrap_up and not self.stalled:
            self.stalled = True
            return 'wrap_up'
        return None


def pinned_outputs(messages) -> set[str]:
    """Call ids of tool outputs a later unchanged-read note points to; the model view keeps them whole."""
    from app.agent.step_models import ToolMessage
    pinned = set()
    for message in messages:
        if isinstance(message, ToolMessage) and UNCHANGED_FIELD in message.content:
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            target = payload.get(UNCHANGED_FIELD) if isinstance(payload, dict) else None
            if isinstance(target, str):
                pinned.add(target)
    return pinned
