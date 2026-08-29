from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result = self.content[self.offset:]
            self.offset = len(self.content)
            return result
        result = self.content[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class UpdaterTests(unittest.TestCase):
    def test_latest_release_requires_expected_assets_and_compares_tags(self) -> None:
        payload = {
            "tag_name": "v0.9",
            "assets": [
                {"name": updater.RELEASE_ASSET_NAME, "browser_download_url": "https://example.test/app.exe", "size": 3},
                {"name": updater.RELEASE_CHECKSUM_NAME, "browser_download_url": "https://example.test/app.sha256", "size": 90},
            ],
        }

        release = updater.fetch_latest_release(lambda *_args, **_kwargs: _Response(json.dumps(payload).encode("utf-8")))

        self.assertEqual(release.tag, "v0.9")
        self.assertTrue(updater.is_newer_release(release.tag, "v0.8"))
        self.assertFalse(updater.is_newer_release("v0.8", "v0.8"))
        self.assertTrue(updater.is_newer_release("v1.0"))
        self.assertFalse(updater.is_newer_release("v0.9"))
        self.assertFalse(updater.is_newer_release("release-candidate", "v0.8"))

    def test_verified_download_requires_release_checksum(self) -> None:
        content = b"verified application payload"
        checksum = hashlib.sha256(content).hexdigest().upper()
        release = updater.CompanionRelease(
            tag="v0.9",
            executable=updater.ReleaseAsset(updater.RELEASE_ASSET_NAME, "https://example.test/app.exe", len(content)),
            checksum=updater.ReleaseAsset(updater.RELEASE_CHECKSUM_NAME, "https://example.test/app.sha256", 90),
        )

        def opener(request, **_kwargs):
            if request.full_url.endswith("app.sha256"):
                return _Response(f"{checksum} *{updater.RELEASE_ASSET_NAME}\n".encode("utf-8"))
            return _Response(content)

        with tempfile.TemporaryDirectory() as folder:
            staged = updater.stage_verified_update(release, Path(folder), opener)
            self.assertEqual(staged.name, "StarEmpireCompanion-v0.9.update.exe")
            self.assertEqual(staged.read_bytes(), content)

    def test_bad_checksum_does_not_leave_a_staged_update(self) -> None:
        content = b"unexpected payload"
        release = updater.CompanionRelease(
            tag="v0.9",
            executable=updater.ReleaseAsset(updater.RELEASE_ASSET_NAME, "https://example.test/app.exe", len(content)),
            checksum=updater.ReleaseAsset(updater.RELEASE_CHECKSUM_NAME, "https://example.test/app.sha256", 90),
        )

        def opener(request, **_kwargs):
            if request.full_url.endswith("app.sha256"):
                return _Response(f"{'0' * 64} *{updater.RELEASE_ASSET_NAME}\n".encode("utf-8"))
            return _Response(content)

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(updater.UpdateError):
                updater.stage_verified_update(release, Path(folder), opener)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_replacement_helper_retries_a_locked_executable_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / updater.RELEASE_ASSET_NAME
            target.write_bytes(b"current")
            staged = Path(folder) / "StarEmpireCompanion-v0.9.update.exe"
            staged.write_bytes(b"verified update")
            with patch.object(updater.subprocess, "Popen") as popen:
                updater.schedule_replacement(target, staged, 1234)

            script = staged.with_suffix(".apply.ps1")
            content = script.read_text(encoding="utf-8")
            self.assertIn("[DateTime]::UtcNow.AddSeconds(30)", content)
            self.assertIn("Move-Item -LiteralPath $Update -Destination $Target -Force -ErrorAction Stop", content)
            self.assertIn("Start-Sleep -Milliseconds 250", content)
            self.assertIn("$Update.failure.log", content)
            popen.assert_called_once()
