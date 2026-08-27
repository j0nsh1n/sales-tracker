#!/usr/bin/env python3
"""Launch a frozen Sales Tracker build and prove a real window opens.

The unit suite cannot see how a packaged build behaves. Two failures got past
it and shipped a dead binary:

  * PyInstaller bundled no Tcl/Tk data, so the runtime hook raised
    FileNotFoundError before any Python ran.
  * The import chain read sys.stdout at module level, which is None in a
    windowed build that was double-clicked rather than started from a shell.

Neither one exits non-zero on Windows. PyInstaller catches the traceback and
shows an "Unhandled exception in script" dialog, so the process sits there
alive with no usable UI. Checking an exit code would call that a pass, so this
script waits for a genuine visible top-level window instead.

The binary is started detached with no console and no inherited stdio, which
is what reproduces the None-stdout path.

Usage:
    python tools/smoke_test.py                          # auto-detect dist/
    python tools/smoke_test.py --binary dist/SalesTracker.exe
    python tools/smoke_test.py --binary dist/SalesTracker.exe --wine

Exit codes: 0 window appeared, 1 launch failed, 2 harness could not run.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

# Titles PyInstaller and Tk use when the app died on its way up. Seeing one of
# these is a definite failure and gives a far better message than a timeout.
FAILURE_TITLES = (
    "unhandled exception in script",
    "failed to execute script",
    "fatal error",
)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class SmokeError(RuntimeError):
    """The harness itself could not run (missing tool, missing binary)."""


# --------------------------------------------------------------------------
# Locating the binary
# --------------------------------------------------------------------------


def default_binary(repo_root: Path) -> Path:
    """Pick the frozen binary this platform builds, and say so if it is absent."""
    names = ["SalesTracker.exe"] if os.name == "nt" else [
        "SalesTracker",
        "SalesTracker-linux-x86_64",
    ]
    for name in names:
        candidate = repo_root / "dist" / name
        if candidate.exists():
            return candidate
    raise SmokeError(
        "no frozen binary found in "
        + str(repo_root / "dist")
        + "; build one with: python -m PyInstaller --noconfirm SalesTracker.spec"
    )


# --------------------------------------------------------------------------
# Window discovery, Windows
# --------------------------------------------------------------------------


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _process_tree_windows(root_pid: int) -> set[int]:
    """Return root_pid plus every descendant.

    A onefile build re-execs itself, so the Tk window belongs to a child of the
    process we started, not to that process. Matching only the launched pid
    finds nothing but the bootloader's hidden window.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        raise SmokeError("CreateToolhelp32Snapshot failed")

    children: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(
                entry.th32ProcessID
            )
            more = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    tree: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in tree:
            continue
        tree.add(pid)
        pending.extend(children.get(pid, []))
    return tree


def _visible_titles_windows(root_pid: int) -> list[str]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    pids = _process_tree_windows(root_pid)
    titles: list[str] = []

    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value:
                titles.append(buffer.value)
        return True

    user32.EnumWindows(proc_type(callback), 0)
    return titles


# --------------------------------------------------------------------------
# Window discovery, X11 (native Linux build and Wine alike)
# --------------------------------------------------------------------------


def _visible_titles_x11() -> list[str]:
    """List visible top-level window names on the current X display.

    Wine gives its windows a pid X11 never learns about, so this matches on
    name across the display rather than by process. That is safe on a CI
    runner and on a throwaway Xvfb display.
    """
    if not shutil.which("xdotool"):
        raise SmokeError(
            "xdotool is required to detect windows on Linux; install it with: "
            "sudo apt-get install -y xdotool"
        )
    found = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", "."],
        capture_output=True,
        text=True,
    )
    titles: list[str] = []
    for window_id in found.stdout.split():
        named = subprocess.run(
            ["xdotool", "getwindowname", window_id],
            capture_output=True,
            text=True,
        )
        title = named.stdout.strip()
        if title:
            titles.append(title)
    return titles


def visible_titles(root_pid: int, use_x11: bool) -> list[str]:
    if use_x11:
        return _visible_titles_x11()
    return _visible_titles_windows(root_pid)


# --------------------------------------------------------------------------
# Launching and polling
# --------------------------------------------------------------------------


def launch(binary: Path, db_path: Path, use_wine: bool) -> subprocess.Popen:
    """Start the build the way a double-click does: detached, no console.

    Inheriting this shell's stdio would hand the child a working sys.stdout and
    hide exactly the bug this test exists to catch.
    """
    command = [str(binary), "--db", str(db_path)]
    if use_wine:
        if not shutil.which("wine"):
            raise SmokeError(
                "--wine given but wine is not installed; install it with: "
                "sudo apt-get install -y wine64"
            )
        command = ["wine", *command]

    kwargs: dict = {"cwd": str(binary.parent)}
    if os.name == "nt":
        # Deliberately no stdio redirection here. Handing the child DEVNULL
        # would give it a valid handle to the null device, so sys.stdout would
        # be a real stream and the None-stdout bug would not reproduce.
        # DETACHED_PROCESS with no handles passed is what a double-click does:
        # the child gets no console, and Python sets sys.stdout to None.
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def terminate(process: subprocess.Popen, use_x11: bool) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    except OSError:
        pass
    if use_x11:
        # Wine leaves its server running between invocations; a stray one would
        # let the next run see this run's window.
        subprocess.run(["wineserver", "-k"], capture_output=True)


def run(binary: Path, want_title: str, timeout: float, use_wine: bool) -> int:
    use_x11 = os.name != "nt"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        process = launch(binary, db_path, use_wine)
        print("launched " + binary.name + " (pid " + str(process.pid) + ") detached")
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        try:
            while time.monotonic() < deadline:
                seen = visible_titles(process.pid, use_x11)
                for title in seen:
                    if title.strip().lower() in FAILURE_TITLES:
                        print("FAIL: the build showed an error dialog: " + title)
                        return 1
                if any(want_title.lower() in t.lower() for t in seen):
                    print("PASS: visible window titled " + repr(want_title))
                    print("windows seen: " + repr(seen))
                    return 0
                if process.poll() is not None and not seen:
                    print(
                        "FAIL: the build exited (code "
                        + str(process.returncode)
                        + ") without opening a window"
                    )
                    return 1
                time.sleep(0.5)
        finally:
            terminate(process, use_x11)

    print(
        "FAIL: no window titled "
        + repr(want_title)
        + " within "
        + str(timeout)
        + "s"
    )
    print("windows seen: " + repr(seen))
    return 1


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--binary",
        type=Path,
        help="frozen build to launch (default: the one this platform builds in dist/)",
    )
    parser.add_argument(
        "--wine",
        action="store_true",
        help="run a Windows .exe through Wine, for checking that build from Linux",
    )
    parser.add_argument(
        "--title",
        default="Sales Tracker",
        help="window title that means the app came up (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the window (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        binary = args.binary or default_binary(repo_root)
        if not binary.exists():
            raise SmokeError("no such binary: " + str(binary))
        if args.wine and os.name == "nt":
            raise SmokeError("--wine is for running a Windows build from Linux")
        print("platform: " + platform.platform())
        print("binary:   " + str(binary))
        print("mode:     " + ("wine" if args.wine else "native"))
        return run(binary, args.title, args.timeout, args.wine)
    except SmokeError as exc:
        print("SMOKE HARNESS ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
