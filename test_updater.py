from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import launcher
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
    def test_packaged_executable_path_finds_the_running_renamed_executable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "My Star Empire Companion.exe"
            executable.write_bytes(b"packaged application")
            with (
                patch.object(updater.sys, "frozen", True, create=True),
                patch.object(updater.sys, "executable", str(executable)),
            ):
                self.assertEqual(updater.packaged_executable_path(), executable.resolve())

            with patch.object(updater.sys, "frozen", False, create=True):
                self.assertIsNone(updater.packaged_executable_path())

    def test_latest_release_requires_expected_assets_and_compares_tags(self) -> None:
        payload = {
            "tag_name": "v0.12",
            "assets": [
                {"name": updater.RELEASE_ASSET_NAME, "browser_download_url": "https://example.test/app.exe", "size": 3},
                {"name": updater.RELEASE_CHECKSUM_NAME, "browser_download_url": "https://example.test/app.sha256", "size": 90},
            ],
        }

        release = updater.fetch_latest_release(lambda *_args, **_kwargs: _Response(json.dumps(payload).encode("utf-8")))

        self.assertEqual(release.tag, "v0.12")
        self.assertTrue(updater.is_newer_release(release.tag, "v0.11"))
        self.assertFalse(updater.is_newer_release("v0.11", "v0.11"))
        self.assertTrue(updater.is_newer_release("v0.20"))
        self.assertFalse(updater.is_newer_release("v0.18"))
        self.assertFalse(updater.is_newer_release("v0.15"))
        self.assertFalse(updater.is_newer_release("release-candidate", "v0.11"))

    def test_verified_update_schedules_the_real_window_close(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "Companion.exe"
            staged = Path(folder) / "StarEmpireCompanion-v0.17.update.exe"
            target.write_bytes(b"current")
            staged.write_bytes(b"verified update")
            desktop = object.__new__(launcher.StarEmpireDesktop)
            desktop.root = Mock()
            desktop._set_update_button_ready = Mock()
            desktop._packaged_companion_path = Mock(return_value=target)
            desktop.close = Mock()

            with (
                patch.object(launcher.messagebox, "askyesno", return_value=True),
                patch.object(launcher.messagebox, "showinfo"),
                patch.object(launcher.updater, "schedule_replacement") as schedule,
                patch.object(launcher.os, "getpid", return_value=1234),
            ):
                desktop._finish_update_download(staged, None)

        schedule.assert_called_once_with(target, staged, 1234)
        desktop.root.after.assert_called_once_with(120, desktop.close)
        desktop.root.after.call_args.args[1]()
        desktop.close.assert_called_once_with()

    def test_verified_download_requires_release_checksum(self) -> None:
        content = b"verified application payload"
        checksum = hashlib.sha256(content).hexdigest().upper()
        release = updater.CompanionRelease(
            tag="v0.12",
            executable=updater.ReleaseAsset(updater.RELEASE_ASSET_NAME, "https://example.test/app.exe", len(content)),
            checksum=updater.ReleaseAsset(updater.RELEASE_CHECKSUM_NAME, "https://example.test/app.sha256", 90),
        )

        def opener(request, **_kwargs):
            if request.full_url.endswith("app.sha256"):
                return _Response(f"{checksum} *{updater.RELEASE_ASSET_NAME}\n".encode("utf-8"))
            return _Response(content)

        with tempfile.TemporaryDirectory() as folder:
            staged = updater.stage_verified_update(release, Path(folder), opener)
            self.assertEqual(staged.name, "StarEmpireCompanion-v0.12.update.exe")
            self.assertEqual(staged.read_bytes(), content)

    def test_bad_checksum_does_not_leave_a_staged_update(self) -> None:
        content = b"unexpected payload"
        release = updater.CompanionRelease(
            tag="v0.12",
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

    def test_replacement_helper_uses_verified_rename_swap_rollback_and_uac_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "Renamed Companion.exe"
            target.write_bytes(b"current")
            staged = Path(folder) / "StarEmpireCompanion-v0.12.update.exe"
            staged.write_bytes(b"verified update")
            with patch.object(updater.subprocess, "Popen") as popen:
                updater.schedule_replacement(target, staged, 1234)

            script = staged.with_suffix(".apply.ps1")
            content = script.read_text(encoding="utf-8")
            self.assertIn("[DateTime]::UtcNow.AddSeconds(30)", content)
            self.assertIn("Copy-Item -LiteralPath $Update -Destination $Replacement -Force -ErrorAction Stop", content)
            self.assertIn("function Get-Sha256", content)
            self.assertIn("[Security.Cryptography.SHA256]::Create()", content)
            self.assertIn("if ((Get-Sha256 $Replacement) -ne $ExpectedHash)", content)
            self.assertIn("[IO.File]::Move($Target, $Rollback)", content)
            self.assertIn("[IO.File]::Move($Replacement, $Target)", content)
            self.assertIn("function Restore-Target", content)
            self.assertIn("$Replacement = \"$Target.pending\"", content)
            self.assertIn("$Rollback = \"$Target.previous.$ParentPid\"", content)
            self.assertIn("Start-Sleep -Milliseconds 250", content)
            self.assertIn("Test-AccessDenied", content)
            self.assertIn("-Verb RunAs", content)
            self.assertIn("-Elevated", content)
            self.assertIn("$Update.failure.log", content)
            self.assertNotIn("Get-FileHash", content)
            self.assertNotIn("[IO.File]::Replace", content)
            popen.assert_called_once()
