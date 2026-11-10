"""The operating system's own "choose a folder" window, for the local web page (workspace-001).

A browser cannot tell a page the full path of a folder the person picks, so on a loopback host
the server opens the system dialog itself: it appears on the same machine as the browser. The
dialog runs in a child Python process (tkinter is in the standard library), so a crash or a
hung window never takes the server with it. The path it returns still goes through
WorkspacePolicy.check like a typed one.
"""

from __future__ import annotations

import asyncio
import os
import sys

PICK_TIMEOUT_SECONDS = 600.0

_SCRIPT = r'''
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
path = filedialog.askdirectory(parent=root, title=sys.argv[2], initialdir=initial, mustexist=True)
root.destroy()
sys.stdout.buffer.write((path or "").encode("utf-8"))
'''


class FolderPickerBusy(RuntimeError):
    """Another choose-a-folder window is already open."""


class FolderPicker:
    """Opens one system folder dialog at a time; returns the chosen path or None when cancelled."""

    def __init__(self, *, timeout_seconds: float = PICK_TIMEOUT_SECONDS, python: str = sys.executable) -> None:
        self.timeout_seconds = timeout_seconds
        self.python = python
        self._lock = asyncio.Lock()

    async def __call__(self, initial: str | None = None) -> str | None:
        if self._lock.locked():
            raise FolderPickerBusy('已经打开了一个选择目录的窗口。')
        async with self._lock:
            start = initial if initial and os.path.isdir(initial) else ''
            process = await asyncio.create_subprocess_exec(
                self.python, '-I', '-c', _SCRIPT, start, '选择工作目录',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return None
            path = out.decode('utf-8', errors='replace').strip()
            return os.path.normpath(path) if path else None
