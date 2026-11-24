"""Find browser profiles on Linux, macOS and Windows."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CHROMIUM = {
    # browser: {platform: [paths relative to home / LOCALAPPDATA]}
    "chrome": {
        "linux": [".config/google-chrome", ".var/app/com.google.Chrome/config/google-chrome"],
        "darwin": ["Library/Application Support/Google/Chrome"],
        "windows": ["Google/Chrome/User Data"],
    },
    "chromium": {
        "linux": [".config/chromium", ".var/app/org.chromium.Chromium/config/chromium", "snap/chromium/common/chromium"],
        "darwin": ["Library/Application Support/Chromium"],
        "windows": ["Chromium/User Data"],
    },
    "brave": {
        "linux": [".config/BraveSoftware/Brave-Browser", ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"],
        "darwin": ["Library/Application Support/BraveSoftware/Brave-Browser"],
        "windows": ["BraveSoftware/Brave-Browser/User Data"],
    },
    "edge": {
        "linux": [".config/microsoft-edge", ".var/app/com.microsoft.Edge/config/microsoft-edge"],
        "darwin": ["Library/Application Support/Microsoft Edge"],
        "windows": ["Microsoft/Edge/User Data"],
    },
    "vivaldi": {
        "linux": [".config/vivaldi", ".var/app/com.vivaldi.Vivaldi/config/vivaldi"],
        "darwin": ["Library/Application Support/Vivaldi"],
        "windows": ["Vivaldi/User Data"],
    },
}
FIREFOX = {
    "firefox": {
        "linux": [".mozilla/firefox", ".var/app/org.mozilla.firefox/.mozilla/firefox", "snap/firefox/common/.mozilla/firefox"],
        "darwin": ["Library/Application Support/Firefox/Profiles"],
        "windows": ["Mozilla/Firefox/Profiles"],  # under APPDATA (roaming)
    },
    "librewolf": {
        "linux": [".librewolf", ".var/app/io.gitlab.librewolf-community/.librewolf"],
        "darwin": ["Library/Application Support/librewolf/Profiles"],
        "windows": ["librewolf/Profiles"],
    },
}
BROWSERS = tuple(CHROMIUM) + tuple(FIREFOX) + ("safari",)
FAMILY = {**{b: "chromium" for b in CHROMIUM}, **{b: "firefox" for b in FIREFOX}, "safari": "safari"}
HISTORY_FILE = {"chromium": "History", "firefox": "places.sqlite", "safari": "History.db"}
SKIP_CHROMIUM_DIRS = {"System Profile", "Guest Profile"}


@dataclass
class Profile:
    browser: str
    family: str
    key: str  # stable: browser/profile-folder
    name: str
    history: Path
    folder: Path

    @property
    def bookmarks(self):
        if self.family == "chromium":
            return self.folder / "Bookmarks"
        if self.family == "firefox":
            return self.history
        return None


def platform_name(value=None):
    value = (value or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith("darwin") or value == "macos":
        return "darwin"
    return "linux"


def bases(home, platform, env, roaming=False):
    if platform != "windows":
        return home
    key = "APPDATA" if roaming else "LOCALAPPDATA"
    default = home / "AppData" / ("Roaming" if roaming else "Local")
    return Path(env.get(key) or default)


def chromium_names(user_data):
    try:
        cache = json.loads((user_data / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]
        return {folder: str(info.get("name") or folder) for folder, info in cache.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def discover(home, platform=None, env=None, browsers=None):
    platform = platform_name(platform)
    env = os.environ if env is None else env
    home = Path(home)
    wanted = set(browsers or BROWSERS)
    found = []
    for browser, paths in CHROMIUM.items():
        if browser not in wanted:
            continue
        for relative in paths.get(platform, []):
            user_data = bases(home, platform, env) / relative
            if not user_data.is_dir():
                continue
            names = chromium_names(user_data)
            for folder in sorted(user_data.iterdir()):
                if folder.name in SKIP_CHROMIUM_DIRS or not (folder / "History").is_file():
                    continue
                found.append(Profile(browser, "chromium", f"{browser}/{folder.name}",
                                     names.get(folder.name, folder.name), folder / "History", folder))
    for browser, paths in FIREFOX.items():
        if browser not in wanted:
            continue
        for relative in paths.get(platform, []):
            root = bases(home, platform, env, roaming=True) / relative
            if not root.is_dir():
                continue
            for folder in sorted(root.iterdir()):
                if (folder / "places.sqlite").is_file():
                    name = folder.name.split(".", 1)[-1] if "." in folder.name else folder.name
                    found.append(Profile(browser, "firefox", f"{browser}/{folder.name}", name,
                                         folder / "places.sqlite", folder))
    if "safari" in wanted and platform == "darwin":
        history = home / "Library" / "Safari" / "History.db"
        if history.is_file():
            found.append(Profile("safari", "safari", "safari/default", "Safari", history, history.parent))
    unique = {}
    for profile in found:  # flatpak and native installs of one browser share folder names
        unique.setdefault(profile.key, profile)
    return list(unique.values())


def explicit(value):
    """BROWSER_HISTORY_PROFILES: `browser:path` items, comma separated;
    path is a profile folder or its history database."""
    profiles = []
    for item in [part.strip() for part in (value or "").split(",") if part.strip()]:
        browser, sep, path = item.partition(":")
        browser = browser.strip().lower()
        if not sep or browser not in FAMILY:
            raise ValueError(f"BROWSER_HISTORY_PROFILES item must be browser:path with browser in {', '.join(BROWSERS)}")
        family = FAMILY[browser]
        path = Path(path.strip()).expanduser()
        history = path / HISTORY_FILE[family] if path.is_dir() else path
        folder = history.parent
        label = re.sub(r"[^A-Za-z0-9_. -]+", "-", folder.name) or "profile"
        profiles.append(Profile(browser, family, f"{browser}/{label}", label, history, folder))
    keys = [p.key for p in profiles]
    if len(keys) != len(set(keys)):
        raise ValueError("BROWSER_HISTORY_PROFILES names two profiles with the same folder name")
    return profiles
