from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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
            "tag_name": "v0.7",
            "assets": [
                {"name": updater.RELEASE_ASSET_NAME, "browser_download_url": "https://example.test/app.exe", "size": 3},
                {"name": updater.RELEASE_CHECKSUM_NAME, "browser_download_url": "https://example.test/app.sha256", "size": 90},
            ],
        }

        release = updater.fetch_latest_release(lambda *_args, **_kwargs: _Response(json.dumps(payload).encode("utf-8")))

        self.assertEqual(release.tag, "v0.7")
        self.assertTrue(updater.is_newer_release(release.tag, "v0.6"))
        self.assertFalse(updater.is_newer_release("v0.6", "v0.6"))
        self.assertTrue(updater.is_newer_release("v0.8"))
        self.assertFalse(updater.is_newer_release("v0.7"))
        self.assertFalse(updater.is_newer_release("release-candidate", "v0.6"))

    def test_verified_download_requires_release_checksum(self) -> None:
        content = b"verified application payload"
        checksum = hashlib.sha256(content).hexdigest().upper()
        release = updater.CompanionRelease(
            tag="v0.7",
            executable=updater.ReleaseAsset(updater.RELEASE_ASSET_NAME, "https://example.test/app.exe", len(content)),
            checksum=updater.ReleaseAsset(updater.RELEASE_CHECKSUM_NAME, "https://example.test/app.sha256", 90),
        )

        def opener(request, **_kwargs):
            if request.full_url.endswith("app.sha256"):
                return _Response(f"{checksum} *{updater.RELEASE_ASSET_NAME}\n".encode("utf-8"))
            return _Response(content)

        with tempfile.TemporaryDirectory() as folder:
            staged = updater.stage_verified_update(release, Path(folder), opener)
            self.assertEqual(staged.name, "StarEmpireCompanion-v0.7.update.exe")
            self.assertEqual(staged.read_bytes(), content)

    def test_bad_checksum_does_not_leave_a_staged_update(self) -> None:
        content = b"unexpected payload"
        release = updater.CompanionRelease(
            tag="v0.7",
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
