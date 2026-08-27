#!/usr/bin/env python3
"""Colour palettes and operating-system theme detection.

The GUI reads its colours from module-level names in `gui`. Switching theme
rebinds those names and rebuilds the ttk styles, so a palette here is just a
mapping from those names to colours.

Every text/background pair below clears WCAG AA (4.5:1). Two names exist only
because one colour cannot do both jobs in both themes: FIELD is the surface
under typed text and table rows, and ON_ACCENT is text sitting on an accent
fill. In light mode both are white; in dark mode FIELD goes near-black while
ON_ACCENT stays dark against a lightened accent.
"""

from __future__ import annotations

import os
import subprocess
import sys

LIGHT = "light"
DARK = "dark"
SYSTEM = "system"

# What the Appearance control offers, in the order it is shown.
THEME_CHOICES = (SYSTEM, LIGHT, DARK)
THEME_LABELS = {SYSTEM: "System", LIGHT: "Light", DARK: "Dark"}
DEFAULT_CHOICE = SYSTEM

PALETTES: dict[str, dict[str, str]] = {
    LIGHT: {
        "BG": "#FFFFFF",
        "PANEL": "#F7F8F8",
        "SUBTLE": "#EFF1F0",
        "INK": "#16191B",
        "MUTED": "#5F6B66",
        "ACCENT": "#0B6E4F",
        "ACCENT_DARK": "#095540",
        "ACCENT_SOFT": "#E8F2EC",
        "DANGER": "#A61B1B",
        "DANGER_SOFT": "#F8E8E8",
        "DISABLED": "#9BB3A8",
        "LINE": "#E3E5E4",
        "ROW_ALT": "#EFF1F0",
        "FOCUS": "#0B6E4F",
        "FIELD": "#FFFFFF",
        "ON_ACCENT": "#FFFFFF",
        "RECEIVED_BG": "#F1F7F3",
    },
    DARK: {
        "BG": "#14171A",
        "PANEL": "#1C2024",
        "SUBTLE": "#252A2F",
        "INK": "#E8EBEC",
        "MUTED": "#A7B2AD",
        "ACCENT": "#4ECB8F",
        "ACCENT_DARK": "#3FB47C",
        "ACCENT_SOFT": "#1E3A2E",
        "DANGER": "#F2777A",
        "DANGER_SOFT": "#3A2323",
        "DISABLED": "#3A4A43",
        "LINE": "#2E353B",
        "ROW_ALT": "#22272C",
        "FOCUS": "#4ECB8F",
        "FIELD": "#111518",
        "ON_ACCENT": "#062018",
        "RECEIVED_BG": "#1B2A22",
    },
}


def normalize_choice(value: object) -> str:
    """Coerce a stored or user-supplied choice to one this module knows."""
    choice = str(value or "").strip().lower()
    return choice if choice in THEME_CHOICES else DEFAULT_CHOICE


def _windows_theme() -> str | None:
    try:
        import winreg
    except ImportError:  # not Windows
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            # 0 means the app should paint itself dark.
            apps_use_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return LIGHT if int(apps_use_light) else DARK
    except (OSError, ValueError):
        return None


def _command_theme(command: list[str], dark_marker: str) -> str | None:
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return DARK if dark_marker in done.stdout.strip().lower() else LIGHT


def _linux_theme() -> str | None:
    # The freedesktop colour-scheme key is the modern answer; the GTK theme
    # name is the fallback for desktops that never adopted it.
    for command, marker in (
        (["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"], "dark"),
        (["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"], "dark"),
    ):
        found = _command_theme(command, marker)
        if found is not None:
            return found
    return None


def _macos_theme() -> str | None:
    found = _command_theme(["defaults", "read", "-g", "AppleInterfaceStyle"], "dark")
    # The key is absent entirely in light mode, which surfaces as a non-zero
    # exit and therefore None; treat that as light rather than unknown.
    return found if found is not None else LIGHT


def detect_os_theme() -> str:
    """Best guess at the desktop's current theme. Falls back to light."""
    if os.name == "nt":
        return _windows_theme() or LIGHT
    if sys.platform == "darwin":
        return _macos_theme() or LIGHT
    return _linux_theme() or LIGHT


def resolve(choice: object) -> str:
    """Turn a stored choice into the palette to actually paint."""
    normalized = normalize_choice(choice)
    if normalized == SYSTEM:
        return detect_os_theme()
    return normalized


def palette(choice: object) -> dict[str, str]:
    return dict(PALETTES[resolve(choice)])
