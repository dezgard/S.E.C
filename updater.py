from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


GITHUB_REPOSITORY = "dezgard/S.E.C"
CURRENT_RELEASE_TAG = "v0.10"
RELEASE_ASSET_NAME = "StarEmpireCompanion.exe"
RELEASE_CHECKSUM_NAME = f"{RELEASE_ASSET_NAME}.sha256"
GITHUB_API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 20


class UpdateError(RuntimeError):
    """A GitHub release could not be safely used as an application update."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int


@dataclass(frozen=True)
class CompanionRelease:
    tag: str
    executable: ReleaseAsset
    checksum: ReleaseAsset


def _release_version(tag: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(tag or "").strip(), re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_release(remote_tag: str, current_tag: str = CURRENT_RELEASE_TAG) -> bool:
    """Compare numeric public release tags such as v0.9 and v0.10."""
    remote = _release_version(remote_tag)
    current = _release_version(current_tag)
    if remote is None or current is None:
        return False
    width = max(len(remote), len(current))
    return remote + (0,) * (width - len(remote)) > current + (0,) * (width - len(current))


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "StarEmpireCompanion-Updater",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )


def _release_asset(raw: Any, expected_name: str) -> ReleaseAsset:
    if not isinstance(raw, dict):
        raise UpdateError(f"The {expected_name} release asset is missing.")
    url = str(raw.get("browser_download_url") or "").strip()
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if str(raw.get("name") or "") != expected_name or not url.startswith("https://") or size <= 0:
        raise UpdateError(f"The {expected_name} release asset is invalid.")
    return ReleaseAsset(expected_name, url, size)


def fetch_latest_release(
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> CompanionRelease:
    """Fetch the pinned repository's latest public release and its two assets."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
    try:
        with opener(_request(api_url), timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub release check failed (HTTP {error.code}).") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UpdateError("Could not read the latest GitHub release.") from error
    if not isinstance(payload, dict):
        raise UpdateError("GitHub returned an invalid release response.")
    tag = str(payload.get("tag_name") or "").strip()
    if _release_version(tag) is None:
        raise UpdateError("The latest GitHub release has an unsupported version tag.")
    assets = {
        str(asset.get("name") or ""): asset
        for asset in payload.get("assets", [])
        if isinstance(asset, dict)
    }
    return CompanionRelease(
        tag=tag,
        executable=_release_asset(assets.get(RELEASE_ASSET_NAME), RELEASE_ASSET_NAME),
        checksum=_release_asset(assets.get(RELEASE_CHECKSUM_NAME), RELEASE_CHECKSUM_NAME),
    )


def _expected_checksum(text: str, asset_name: str) -> str:
    for line in str(text or "").splitlines():
        fields = line.strip().split()
        if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            continue
        if len(fields) == 1 or fields[-1].lstrip("*") == asset_name:
            return fields[0].upper()
    raise UpdateError(f"The {RELEASE_CHECKSUM_NAME} asset has no checksum for {asset_name}.")


def update_staging_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "StarEmpireCompanion" / "updates"


def stage_verified_update(
    release: CompanionRelease,
    directory: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Path:
    """Download the release executable only after matching its release checksum."""
    target_dir = directory or update_staging_directory()
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with opener(_request(release.checksum.download_url), timeout=REQUEST_TIMEOUT_SECONDS) as response:
            checksum_text = response.read().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise UpdateError("Could not download the release checksum.") from error
    expected = _expected_checksum(checksum_text, release.executable.name)
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", release.tag).strip(".-") or "latest"
    destination = target_dir / f"StarEmpireCompanion-{safe_tag}.update.exe"
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(_request(release.executable.download_url), timeout=REQUEST_TIMEOUT_SECONDS) as response:
            with tempfile.NamedTemporaryFile("wb", dir=target_dir, delete=False, suffix=".part") as handle:
                temporary_path = Path(handle.name)
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    digest.update(chunk)
                    handle.write(chunk)
        if received != release.executable.size:
            raise UpdateError("The downloaded update size does not match the GitHub release.")
        if digest.hexdigest().upper() != expected:
            raise UpdateError("The downloaded update checksum does not match the GitHub release.")
        temporary_path.replace(destination)
        return destination
    except (OSError, urllib.error.HTTPError) as error:
        raise UpdateError("Could not download the Companion update.") from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def schedule_replacement(target: Path, staged_update: Path, parent_pid: int) -> None:
    """Replace a packaged Companion after its running process has exited."""
    if target.name.casefold() != RELEASE_ASSET_NAME.casefold() or not staged_update.is_file():
        raise UpdateError("The selected application update is not valid.")
    script = staged_update.with_suffix(".apply.ps1")
    script.write_text(
        "param([string]$Target, [string]$Update, [int]$ParentPid)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$FailureLog = \"$Update.failure.log\"\n"
        "try {\n"
        "  while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 200 }\n"
        "  $deadline = [DateTime]::UtcNow.AddSeconds(30)\n"
        "  while ($true) {\n"
        "    try {\n"
        "      Move-Item -LiteralPath $Update -Destination $Target -Force -ErrorAction Stop\n"
        "      break\n"
        "    } catch {\n"
        "      if ([DateTime]::UtcNow -ge $deadline) { throw }\n"
        "      Start-Sleep -Milliseconds 250\n"
        "    }\n"
        "  }\n"
        "  Start-Process -FilePath $Target -ErrorAction Stop\n"
        "  Remove-Item -LiteralPath $PSCommandPath -Force\n"
        "} catch {\n"
        "  $_ | Out-File -LiteralPath $FailureLog -Encoding utf8\n"
        "}\n",
        encoding="utf-8",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Target", str(target), "-Update", str(staged_update), "-ParentPid", str(parent_pid),
            ],
            creationflags=flags,
        )
    except OSError as error:
        script.unlink(missing_ok=True)
        raise UpdateError("Could not start the Companion updater.") from error
