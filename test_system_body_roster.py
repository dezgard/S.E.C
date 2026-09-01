from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import archive_store
import game_link
import launcher
import sharing


ROSTER_BODY = {
    "planet_id": "moon-17",
    "planet_name": "Luna 17",
    "planet_type": "Moon",
    "system_name": "Alpha",
    "is_moon": True,
    "isScanned": False,
    "observedAt": "2026-08-30 10:00:00",
}


class SystemBodyRosterTests(unittest.TestCase):
    def test_window_title_includes_the_release_version(self) -> None:
        self.assertEqual("Star Empire Companion • v9.8", launcher.application_window_title("v9.8"))
        self.assertEqual("Star Empire Companion", launcher.application_window_title(""))

    def test_embedded_verification_requires_system_body_roster_capture(self) -> None:
        strings = {
            "SHOP_CATALOG %s",
            "SHOP_CATALOG capture failed for %s",
            "TRAINING_CATALOG %s",
            "training_inventory",
            "PLANET_SCAN_RESULT %s",
            "Archive snapshot capture failed for %s",
            "STATION_EXTRACTOR_SNAPSHOT",
            "Station extractor capture failed",
            "equipped_module_counts",
        }
        strings.update(game_link.HOOKS.values())
        strings.update(marker for marker, _expression, _anchor in game_link.SNAPSHOT_HOOKS.values())

        with (
            patch.object(game_link, "_embedded_module_code", return_value=object()),
            patch.object(game_link, "_code_strings", return_value=strings),
            patch.object(game_link, "_embedded_panel_layout_compatible", return_value=True),
        ):
            missing_roster = game_link._embedded_part_presence(Path("Client.exe"))
            strings.update({"SYSTEM_BODY_ROSTER", "System body roster capture failed"})
            complete = game_link._embedded_part_presence(Path("Client.exe"))

        self.assertFalse(missing_roster["system body roster"])
        self.assertTrue(complete["system body roster"])

    def test_log_roster_creates_unscanned_body_rows(self) -> None:
        payload = {
            "system_id": "alpha-id",
            "system_name": "Alpha",
            "bodies": [
                {
                    "planet_id": "planet-1",
                    "planet_name": "Alpha I",
                    "planet_type": "Terra",
                    "is_moon": False,
                },
                {
                    "planet_id": "moon-17",
                    "planet_name": "Luna 17",
                    "planet_type": "Moon",
                    "is_moon": True,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "star_empire_client.log"
            log_path.write_text(
                "2026-08-30 10:00:00 INFO SYSTEM_BODY_ROSTER "
                + json.dumps(payload)
                + "\n",
                encoding="utf-8",
            )
            catalog = app.DataStore()._build((), [log_path])

        rows = {row["planet_id"]: row for row in catalog["scans"]}
        self.assertEqual({"planet-1", "moon-17"}, set(rows))
        self.assertFalse(rows["planet-1"]["isScanned"])
        self.assertEqual("Alpha", rows["moon-17"]["system_name"])
        self.assertTrue(launcher.scan_is_moon(rows["moon-17"]))
        self.assertEqual("UNSCANNED", launcher.scan_column_display_value(rows["moon-17"], "status"))
        self.assertIsNone(launcher.scan_column_raw_value(rows["moon-17"], "resources"))

    def test_real_scan_never_reverts_to_an_unscanned_roster_row(self) -> None:
        scanned = {
            **ROSTER_BODY,
            "isScanned": True,
            "ok": True,
            "resources": {"metal_ore": 80},
            "extractors": {"metal_ore": 80},
            "observedAt": "2026-08-30 09:00:00",
        }
        roster_seen_later = {**ROSTER_BODY, "observedAt": "2026-08-30 11:00:00"}

        merged = archive_store._merge_scans([scanned], [roster_seen_later])

        self.assertEqual(1, len(merged))
        self.assertTrue(merged[0]["isScanned"])
        self.assertEqual({"metal_ore": 80}, merged[0]["resources"])
        self.assertEqual("SCANNED", launcher.scan_column_display_value(merged[0], "status"))

    def test_unscanned_roster_creates_a_marked_system_yield_row(self) -> None:
        planet = {
            "planet_id": "planet-1",
            "planet_name": "Alpha I",
            "planet_type": "Terra",
            "system_name": "Alpha",
            "is_moon": False,
            "isScanned": False,
        }
        rows = launcher.system_resource_totals([planet, ROSTER_BODY])
        self.assertEqual(1, len(rows))
        self.assertEqual("Alpha", rows[0]["system"])
        self.assertEqual(2, rows[0]["planets"])
        self.assertEqual(2, rows[0]["unscannedBodies"])
        self.assertEqual(0.0, rows[0]["total"])
        self.assertEqual("UNSCANNED", launcher.system_yield_display_value(rows[0], "total"))
        self.assertEqual(
            {"bodies": 2, "planetBodies": 1, "moonBodies": 1, "maxBases": 4},
            launcher.system_extraction_base_summary([planet, ROSTER_BODY]),
        )

    def test_planet_resource_matrix_keeps_every_resource_and_zero(self) -> None:
        scan = {
            "planet_id": "planet-1",
            "planet_name": "Alpha I",
            "planet_type": "Terra",
            "system_name": "Alpha",
            "isScanned": True,
            "resources": {"metal_ore": 54, "silicon": 42, "oil": 0},
        }
        rows = launcher.scan_resource_matrix_export_rows([(scan, {}, {})])
        headers, values = rows

        self.assertEqual("PLANET", headers[0])
        self.assertEqual("METAL ORE", headers[headers.index("METAL ORE")])
        self.assertEqual("54", values[headers.index("METAL ORE")])
        self.assertEqual("0", values[headers.index("OIL")])
        self.assertEqual("0", values[headers.index("WOOD")])
        self.assertNotIn("resources", launcher.SCAN_COLUMN_SPECS)
        self.assertNotIn("Overview", launcher.SCAN_COLUMN_PRESETS)
        self.assertNotIn("extractor", launcher.SCAN_DEFAULT_COLUMNS)
        legacy_saved_columns = ["system", "type", "colony", "resources", "base", "count", "observed"]
        self.assertTrue(launcher.scan_layout_uses_removed_resource_summary(legacy_saved_columns))
        self.assertFalse(launcher.scan_layout_uses_removed_resource_summary(list(launcher.SCAN_DEFAULT_COLUMNS)))
        self.assertEqual(list(launcher.SCAN_DEFAULT_COLUMNS), launcher.scan_restored_layout_columns(legacy_saved_columns))
        self.assertEqual(["system", "type"], launcher.scan_restored_layout_columns(["system", "unknown", "type"]))
        self.assertIsNone(launcher.scan_column_raw_value(ROSTER_BODY, "res_metal_ore"))

    def test_shared_unscanned_rows_keep_only_safe_roster_fields(self) -> None:
        shared = sharing.sanitise_catalog(
            {
                "scans": [
                    {
                        **ROSTER_BODY,
                        "resources": {"metal_ore": 999},
                        "extractors": {"metal_ore": 999},
                        "owner": "private-player",
                    }
                ],
                "map": {},
            }
        )

        row = shared["scans"][0]
        self.assertEqual("Luna 17", row["planet_name"])
        self.assertTrue(row["is_moon"])
        self.assertFalse(row["isScanned"])
        self.assertNotIn("resources", row)
        self.assertNotIn("extractors", row)
        self.assertNotIn("owner", row)


if __name__ == "__main__":
    unittest.main()
