#!/usr/bin/env python3
"""Self-update for the packaged build.

The protocol is a manifest, ``update.json``, published beside each release's
binaries::

    {
      "version": "0.2.0",
      "published": "2026-10-01",
      "notes": "What changed, from CHANGELOG.md",
      "assets": {
        "windows-x86_64": {"name": "SalesTracker.exe", "url": "...",
                           "size": 13421772, "sha256": "..."},
        "linux-x86_64":   {"name": "SalesTracker-linux-x86_64", "url": "...",
                           "size": 17206520, "sha256": "..."}
      }
    }

An *update source* is wherever that manifest lives. Three kinds are
understood, so the app is not tied to GitHub or to a network at all:

``github:owner/repo``
    The default. A public repo is read through the
    ``releases/latest/download/update.json`` redirect, which is an ordinary
    web fetch and does not count against GitHub's API rate limit. A private
    repo needs a token (``SALES_TRACKER_UPDATE_TOKEN`` or the ``update_token``
    setting); the token is used for the API and is never forwarded to the
    signed asset host GitHub redirects to, which would reject it.
``https://host/path/`` or ``https://host/path/update.json``
    Any web server. Relative asset URLs resolve against the manifest.
A folder path
    A local or shared folder holding ``update.json`` and the binaries.

Checks are cached with the manifest's ETag and run at most once a day.
Installing renames the running binary to ``.old`` and puts the verified
download in its place; both Windows and Linux allow that while the program
runs. The ``.old`` copy is kept so the previous version can be restored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from salestracker.models import TrackerError

DEFAULT_SOURCE = "github:j0nsh1n/sales-tracker"
MANIFEST_NAME = "update.json"
TOKEN_ENV = "SALES_TRACKER_UPDATE_TOKEN"
CHECK_INTERVAL = timedelta(hours=24)
USER_AGENT = "SalesTracker-updater"
# Settings-table keys. Preferences, not ledger data; a reset leaves them.
SETTING_SOURCE = "update_source"
SETTING_TOKEN = "update_token"
SETTING_LAST_CHECK = "update_last_check"
SETTING_ETAG = "update_etag"
SETTING_CACHED = "update_cached_manifest"


class UpdateError(TrackerError):
    """A check or install that could not be completed, in the operator's words."""


@dataclass(frozen=True)
class Asset:
    key: str
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    version: str
    published: str
    notes: str
    assets: dict[str, Asset] = field(default_factory=dict)

    def asset_for(self, key: str) -> Asset:
        try:
            return self.assets[key]
        except KeyError as exc:
            raise UpdateError(
                f"Version {self.version} has no build for this platform ({key})."
            ) from exc


# ------------------------------------------------------------------ versions

def parse_version(text: object) -> tuple[int, ...]:
    """"v0.2.0" -> (0, 2, 0). Anything non-numeric is ignored."""
    parts = re.findall(r"\d+", str(text or ""))
    if not parts:
        raise UpdateError(f"{text!r} is not a version number.")
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def platform_key() -> str:
    machine = platform.machine().lower() or "unknown"
    if machine in ("amd64", "x86_64"):
        machine = "x86_64"
    if os.name == "nt":
        return f"windows-{machine}"
    if sys.platform == "darwin":
        return f"macos-{machine}"
    return f"linux-{machine}"


def asset_key_for_name(name: str) -> str | None:
    """Platform key a release file name is for, or None if it is not a build."""
    lowered = name.lower()
    if lowered.endswith(".exe"):
        return "windows-x86_64"
    match = re.search(r"-(linux|macos)-([a-z0-9_]+)$", lowered)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def target_path() -> Path | None:
    """The running binary when frozen; None from a source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


# ------------------------------------------------------------------ manifest

def parse_manifest(text: str | bytes) -> Release:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise UpdateError("The update manifest is not valid JSON.") from exc
    if not isinstance(data, dict) or "version" not in data:
        raise UpdateError("The update manifest has no version.")
    parse_version(data["version"])
    assets: dict[str, Asset] = {}
    for key, raw in (data.get("assets") or {}).items():
        try:
            assets[key] = Asset(
                key=key, name=str(raw["name"]), url=str(raw["url"]),
                size=int(raw["size"]), sha256=str(raw["sha256"]).lower(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError(f"The update manifest's {key} entry is incomplete.") from exc
    return Release(
        version=str(data["version"]).lstrip("v"),
        published=str(data.get("published", "")),
        notes=str(data.get("notes", "")),
        assets=assets,
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def changelog_notes(text: str, version: str) -> str:
    """The CHANGELOG section for a version, or "" when there is none."""
    heading = re.compile(r"^## \[?" + re.escape(version) + r"\]?", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    rest = rest.split("\n", 1)[1] if "\n" in rest else ""
    end = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    return body.strip()


def write_manifest(
    files: list[Path], version: str, base_url: str, *,
    published: str | None = None, notes: str = "",
) -> str:
    """The manifest for a release, as JSON text. base_url is where the files
    will be served; a folder path works as well as a URL."""
    assets = {}
    for path in files:
        key = asset_key_for_name(path.name)
        if key is None:
            raise UpdateError(f"{path.name} is not named like a release build.")
        assets[key] = {
            "name": path.name,
            "url": base_url + path.name if base_url else path.name,
            "size": path.stat().st_size,
            "sha256": sha256_of(path),
        }
    manifest = {
        "version": version.lstrip("v"),
        "published": published or datetime.now().date().isoformat(),
        "notes": notes,
        "assets": assets,
    }
    return json.dumps(manifest, indent=2) + "\n"


# ------------------------------------------------------------------ sources

@dataclass(frozen=True)
class Source:
    kind: str            # "github", "url", or "folder"
    manifest: str        # where update.json is read from
    base: str            # what relative asset urls resolve against
    owner: str = ""
    repo: str = ""


def resolve_source(text: str | None) -> Source:
    raw = (text or DEFAULT_SOURCE).strip()
    if raw.startswith("github:"):
        spec = raw[len("github:"):].strip("/")
        if spec.count("/") != 1 or not all(spec.split("/")):
            raise UpdateError("A GitHub source looks like github:owner/repo.")
        owner, repo = spec.split("/")
        base = f"https://github.com/{owner}/{repo}/releases/latest/download/"
        return Source("github", base + MANIFEST_NAME, base, owner, repo)
    if raw.lower().startswith(("http://", "https://")):
        if raw.lower().endswith(".json"):
            manifest = raw
            base = raw.rsplit("/", 1)[0] + "/"
        else:
            base = raw if raw.endswith("/") else raw + "/"
            manifest = base + MANIFEST_NAME
        return Source("url", manifest, base)
    folder = Path(raw).expanduser()
    if raw.lower().endswith(".json"):
        return Source("folder", str(folder), str(folder.parent))
    return Source("folder", str(folder / MANIFEST_NAME), str(folder))


def resolve_asset_url(source: Source, url: str) -> str:
    if url.lower().startswith(("http://", "https://")):
        return url
    if source.kind == "folder":
        return str(Path(source.base) / url)
    return urllib.parse.urljoin(source.base, url)


# ------------------------------------------------------------------ fetching

@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class _NoTokenAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves the original host.

    GitHub answers an asset request with a redirect to a signed URL on a
    storage host that rejects any request still carrying the token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host != new_host:
                new.remove_header("Authorization")
        return new


def http_fetch(url: str, headers: dict[str, str] | None = None,
               timeout: float = 20.0) -> Response:
    """One GET. 3xx are followed; other non-2xx come back as a Response."""
    opener = urllib.request.build_opener(_NoTokenAcrossHosts())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with opener.open(request, timeout=timeout) as reply:
            return Response(reply.status, {k.lower(): v for k, v in reply.headers.items()},
                            reply.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()},
                        exc.read() if exc.fp else b"")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UpdateError(f"Could not reach {urllib.parse.urlsplit(url).hostname or url}: {reason}") from exc


Fetch = Callable[..., Response]


def _rate_limit_message(response: Response) -> str:
    reset = response.headers.get("x-ratelimit-reset")
    retry = response.headers.get("retry-after")
    when = ""
    if reset and reset.isdigit():
        when = " until " + datetime.fromtimestamp(int(reset)).strftime("%H:%M")
    elif retry and retry.isdigit():
        when = f" for another {retry} seconds"
    return ("GitHub is rate-limiting this address" + when +
            ". Add a token in Settings to lift the limit, or try again later.")


# ------------------------------------------------------------------ updater

class Updater:
    """Check, download, verify, install. Settings come through a get/set pair
    so the GUI, the CLI and the tests all share one implementation."""

    def __init__(
        self,
        current_version: str,
        get_setting: Callable[[str, str], str],
        set_setting: Callable[[str, str], str],
        *,
        fetch: Fetch = http_fetch,
        now: Callable[[], datetime] = datetime.now,
        platform: str | None = None,
    ) -> None:
        self.current_version = current_version
        self._get = get_setting
        self._set = set_setting
        self._fetch = fetch
        self._now = now
        self.platform = platform or platform_key()

    # settings

    @property
    def source_text(self) -> str:
        return self._get(SETTING_SOURCE, "") or DEFAULT_SOURCE

    def set_source(self, text: str) -> None:
        resolve_source(text or DEFAULT_SOURCE)
        self._set(SETTING_SOURCE, (text or "").strip())
        self._set(SETTING_ETAG, "")
        self._set(SETTING_CACHED, "")

    @property
    def token(self) -> str:
        return os.environ.get(TOKEN_ENV, "").strip() or self._get(SETTING_TOKEN, "").strip()

    def set_token(self, token: str) -> None:
        self._set(SETTING_TOKEN, (token or "").strip())

    def last_check(self) -> datetime | None:
        stamp = self._get(SETTING_LAST_CHECK, "")
        try:
            return datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            return None

    def due(self) -> bool:
        """Whether a quiet background check should run now."""
        last = self.last_check()
        return last is None or self._now() - last >= CHECK_INTERVAL

    # checking

    def _github_api(self, source: Source, path: str) -> Response:
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {self.token}"}
        return self._fetch(f"https://api.github.com/repos/{source.owner}/{source.repo}{path}",
                           headers)

    def _read_manifest(self, source: Source) -> tuple[str, str]:
        """(manifest text, etag). Uses the cached copy on a 304."""
        if source.kind == "folder":
            path = Path(source.manifest)
            try:
                return path.read_text(encoding="utf-8"), ""
            except OSError as exc:
                raise UpdateError(f"No {MANIFEST_NAME} at {path.parent}.") from exc
        headers = {}
        etag = self._get(SETTING_ETAG, "")
        cached = self._get(SETTING_CACHED, "")
        if etag and cached:
            headers["If-None-Match"] = etag
        response = self._fetch(source.manifest, headers)
        if response.status == 304 and cached:
            return cached, etag
        if response.status == 404 and source.kind == "github":
            if not self.token:
                raise UpdateError(
                    f"No release manifest at {source.owner}/{source.repo}. "
                    "If the repository is private, add a token in Settings."
                )
            return self._read_private_github(source)
        if response.status in (403, 429):
            raise UpdateError(_rate_limit_message(response))
        if response.status != 200:
            raise UpdateError(f"The update source answered {response.status}.")
        return response.body.decode("utf-8", "replace"), response.headers.get("etag", "")

    def _read_private_github(self, source: Source) -> tuple[str, str]:
        latest = self._github_api(source, "/releases/latest")
        if latest.status in (403, 429):
            raise UpdateError(_rate_limit_message(latest))
        if latest.status == 401:
            raise UpdateError("GitHub refused the token. Check it in Settings.")
        if latest.status != 200:
            raise UpdateError(f"GitHub answered {latest.status} for the latest release.")
        try:
            data = json.loads(latest.body)
            assets = {a["name"]: a for a in data.get("assets", [])}
        except (ValueError, TypeError, KeyError) as exc:
            raise UpdateError("GitHub's release listing could not be read.") from exc
        if MANIFEST_NAME not in assets:
            raise UpdateError(f"The latest release has no {MANIFEST_NAME}.")
        # Each asset's API url serves the file with this Accept header, via a
        # redirect to a signed storage URL that must not see the token.
        manifest = self._fetch(assets[MANIFEST_NAME]["url"], {
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {self.token}",
        })
        if manifest.status != 200:
            raise UpdateError(f"GitHub answered {manifest.status} for {MANIFEST_NAME}.")
        text = manifest.body.decode("utf-8", "replace")
        # Binaries in a private repo are fetched the same way; rewrite the
        # manifest's public download links to their API urls.
        release = parse_manifest(text)
        rewritten = json.loads(text)
        for key, asset in release.assets.items():
            if asset.name in assets:
                rewritten["assets"][key]["url"] = assets[asset.name]["url"]
        return json.dumps(rewritten), ""

    def check(self, *, quiet: bool = False) -> Release:
        """The latest release at the source. Records the check time."""
        source = resolve_source(self.source_text)
        text, etag = self._read_manifest(source)
        release = parse_manifest(text)
        if source.kind != "folder":
            self._set(SETTING_ETAG, etag)
            self._set(SETTING_CACHED, text)
        self._set(SETTING_LAST_CHECK, self._now().isoformat(timespec="seconds"))
        return release

    def available(self, release: Release) -> bool:
        return is_newer(release.version, self.current_version)

    # downloading and installing

    def download(self, release: Release, into: Path,
                 progress: Callable[[int, int], None] | None = None) -> Path:
        """Fetch this platform's build into ``into`` and verify it.

        Returns the verified file, named ``<asset>.new``. A file that fails
        the size or hash check is deleted and the failure raised.
        """
        asset = release.asset_for(self.platform)
        source = resolve_source(self.source_text)
        url = resolve_asset_url(source, asset.url)
        into.mkdir(parents=True, exist_ok=True)
        target = into / (asset.name + ".new")
        if source.kind == "folder":
            try:
                shutil.copyfile(url, target)
            except OSError as exc:
                raise UpdateError(f"Could not copy {asset.name} from {source.base}: {exc}") from exc
        else:
            headers = {}
            if source.kind == "github" and self.token and "api.github.com" in url:
                headers = {"Accept": "application/octet-stream",
                           "Authorization": f"Bearer {self.token}"}
            response = self._fetch(url, headers, 300.0)
            if response.status in (403, 429):
                raise UpdateError(_rate_limit_message(response))
            if response.status != 200:
                raise UpdateError(f"The download of {asset.name} answered {response.status}.")
            target.write_bytes(response.body)
        if progress:
            progress(asset.size, asset.size)
        size = target.stat().st_size
        if size != asset.size:
            target.unlink(missing_ok=True)
            raise UpdateError(
                f"{asset.name} arrived incomplete ({size:,} of {asset.size:,} bytes)."
            )
        digest = sha256_of(target)
        if digest != asset.sha256:
            target.unlink(missing_ok=True)
            raise UpdateError(f"{asset.name} did not match its published checksum; not installed.")
        return target

    @staticmethod
    def install(new_file: Path, target: Path | None = None) -> Path:
        """Put the verified file in the running binary's place.

        The running file is renamed to ``.old`` first (a rename, which both
        Windows and Linux allow on a running program) and kept for
        ``restore_previous``. Returns the installed path.
        """
        target = target or target_path()
        if target is None:
            raise UpdateError(
                "Updates install into the packaged build only. From a source "
                "checkout, pull the repository instead."
            )
        backup = target.with_name(target.name + ".old")
        if backup.exists():
            backup.unlink()
        os.replace(target, backup)
        try:
            os.replace(new_file, target)
        except OSError:
            os.replace(backup, target)
            raise
        if os.name != "nt":
            target.chmod(target.stat().st_mode | 0o111)
        return target

    @staticmethod
    def previous(target: Path | None = None) -> Path | None:
        target = target or target_path()
        if target is None:
            return None
        backup = target.with_name(target.name + ".old")
        return backup if backup.exists() else None

    @classmethod
    def restore_previous(cls, target: Path | None = None) -> Path:
        target = target or target_path()
        backup = cls.previous(target)
        if target is None or backup is None:
            raise UpdateError("There is no previous version to go back to.")
        swap = target.with_name(target.name + ".swap")
        os.replace(target, swap)
        os.replace(backup, target)
        os.replace(swap, backup)
        return target


# ------------------------------------------------------------------ command line

def main(argv: list[str] | None = None) -> int:
    """Release-side tooling: write the manifest CI attaches to a release."""
    parser = argparse.ArgumentParser(description="Sales Tracker update manifest tool.")
    sub = parser.add_subparsers(dest="command", required=True)
    writer = sub.add_parser("write-manifest", help="Print update.json for a release")
    writer.add_argument("files", nargs="+", type=Path, help="The release binaries")
    writer.add_argument("--version", required=True, help="Release version, e.g. v0.2.0")
    writer.add_argument("--base-url", default="", help="Where the files will be served")
    writer.add_argument("--notes-from", type=Path, help="CHANGELOG.md to take notes from")
    args = parser.parse_args(argv)
    notes = ""
    if args.notes_from:
        notes = changelog_notes(args.notes_from.read_text(encoding="utf-8"),
                                args.version.lstrip("v"))
    try:
        sys.stdout.write(write_manifest(args.files, args.version, args.base_url, notes=notes))
    except (UpdateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
