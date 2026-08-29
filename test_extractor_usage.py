from __future__ import annotations

import unittest

import app
import archive_store
import launcher
import sharing


class _TextCapture:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def insert(self, _position: str, text: str, *_tags: str) -> None:
        self.parts.append(str(text))

    @property
    def text(self) -> str:
        return "".join(self.parts)


class ExtractorUsageTests(unittest.TestCase):
    def test_docked_tier_counts_aggregate_to_one_resource_total(self) -> None:
        record = app._normalise_extractor_snapshot(
            {
                "station_id": "metal-base",
                "station_name": "Metal Base",
                "system_name": "Peacock Station",
                "planet_id": "metal-world",
                "equipped_module_counts": {
                    "metal_drill": 100,
                    "advanced_metal_drill": 200,
                    "industrial_metal_drill": 90,
                    "station_shield_mk1": 1,
                },
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["resourceSlots"], {"metal_ore": 390})
        self.assertEqual(
            app.system_extractor_tier_counts([record], "peacock station"),
            {"metal_ore": {3: 100, 6: 200, 9: 90}},
        )
        observed = app.system_extractor_slots([record], "peacock station")
        self.assertEqual(observed, {"metal_ore": 390})
        self.assertEqual(
            launcher.extractor_slot_entries({"metal_ore": 1770}, observed),
            [("Metal Ore", 390, 1770.0)],
        )
        self.assertEqual(
            launcher.extractor_tier_summary({3: 100, 6: 200, 9: 90}),
            "100× T3  ·  200× T6  ·  90× T9",
        )

    def test_slot_maximum_uses_three_planet_bases_and_one_moon_base(self) -> None:
        maximums = launcher.system_extractor_slot_capacities([
            {
                "system_name": "Peacock Station",
                "planet_type": "Terra",
                "extractors": {"metal_ore": 100},
            },
            {
                "system_name": "Peacock Station",
                "planet_type": "Moon",
                "extractors": {"metal_ore": 50},
            },
        ])

        self.assertEqual(maximums, {"metal_ore": 350.0})
        self.assertEqual(
            launcher.system_extraction_base_summary([
                {"planet_type": "Terra"},
                {"planet_type": "Moon"},
            ]),
            {"bodies": 2, "planetBodies": 1, "moonBodies": 1, "maxBases": 4},
        )

    def test_system_yield_rows_keep_one_base_total_and_show_moon_aware_maximum(self) -> None:
        rows = launcher.system_resource_totals([
            {"system_name": "Peacock Station", "planet_type": "Terra", "extractors": {"metal_ore": 100}},
            {"system_name": "Peacock Station", "planet_type": "Moon", "extractors": {"metal_ore": 50}},
        ])

        metal_ore_column = f"{launcher.SCAN_RESOURCE_COLUMN_PREFIX}metal_ore"
        maximum_metal_ore_column = f"max_{metal_ore_column}"
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][metal_ore_column], 150.0)
        self.assertEqual(rows[0][maximum_metal_ore_column], 350.0)
        self.assertEqual(rows[0]["maxTotal"], 350.0)
        self.assertEqual(
            launcher.system_yield_display_value(rows[0], metal_ore_column),
            "150 (350)",
        )

    def test_legacy_aggregate_snapshot_does_not_invent_a_tier_mix(self) -> None:
        self.assertEqual(
            app.system_extractor_tier_counts(
                [{
                    "stationId": "old-base",
                    "systemName": "Peacock Station",
                    "resourceSlots": {"metal_ore": 390},
                }],
                "Peacock Station",
            ),
            {},
        )

    def test_extractor_detail_renders_detailed_capacity_and_observed_tiers(self) -> None:
        widget = _TextCapture()
        desktop = object.__new__(launcher.StarEmpireDesktop)

        entries = launcher.system_extraction_capacity_entries(
            {"metal_ore": 1770},
            {"metal_ore": 390},
            {"metal_ore": {3: 100, 6: 200, 9: 90}},
        )
        desktop._insert_system_extraction_entries(
            widget,
            entries,
        )

        self.assertEqual(
            widget.text,
            "Metal Ore\n"
            "  KNOWN USED: 390 / 1,770 slots · 22.0% OF MAX\n"
            "  NOT LOCALLY OBSERVED AS USED: 1,380 slots\n"
            "  OBSERVED TIERS: 100× T3  ·  200× T6  ·  90× T9\n",
        )

    def test_latest_private_station_snapshot_survives_log_rotation(self) -> None:
        previous = [{
            "stationId": "metal-base",
            "systemName": "Peacock Station",
            "moduleCounts": {"metal_drill": 100},
            "resourceSlots": {"metal_ore": 100},
            "observedAt": "2026-08-29 10:00:00",
        }]
        current = [{
            "stationId": "metal-base",
            "stationName": "Metal Base",
            "systemName": "Peacock Station",
            "moduleCounts": {
                "metal_drill": 100,
                "advanced_metal_drill": 200,
                "industrial_metal_drill": 90,
            },
            "resourceSlots": {"metal_ore": 390},
            "observedAt": "2026-08-29 11:00:00",
        }]

        merged = archive_store._merge_private_extractor_usage(previous, current)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["resourceSlots"], {"metal_ore": 390})
        self.assertEqual(
            merged[0]["moduleCounts"],
            {
                "metal_drill": 100,
                "advanced_metal_drill": 200,
                "industrial_metal_drill": 90,
            },
        )
        self.assertEqual(merged[0]["stationName"], "Metal Base")

    def test_private_extractor_usage_cannot_enter_shared_intel(self) -> None:
        clean = sharing.sanitise_catalog(
            {
                "privateExtractorUsage": [{
                    "stationId": "metal-base",
                    "systemName": "Peacock Station",
                    "resourceSlots": {"metal_ore": 390},
                }],
                "map": {},
            }
        )

        self.assertNotIn("privateExtractorUsage", clean)


if __name__ == "__main__":
    unittest.main()
