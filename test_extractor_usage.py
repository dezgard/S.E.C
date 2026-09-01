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

    def test_observed_module_production_calculates_the_server_tick_output(self) -> None:
        record = app._normalise_extractor_snapshot(
            {
                "station_id": "metal-base",
                "system_name": "Peacock Station",
                "equipped_module_counts": {
                    "metal_drill": 100,
                    "advanced_metal_drill": 200,
                    "industrial_metal_drill": 90,
                },
            }
        )
        assert record is not None
        output = app.extractor_record_output_per_tick(
            record,
            [
                {"type": "metal_drill", "stats": {"Production": "8,640/day"}},
                {"type": "advanced_metal_drill", "stats": {"Production": "17,280/day"}},
                {"type": "industrial_metal_drill", "stats": {"Production": "25,920/day"}},
            ],
            7_200,
        )

        self.assertEqual(output, {"metal_ore": 554_400.0})

    def test_equipped_production_counts_extractors_processors_inputs_and_credits(self) -> None:
        record = app._normalise_extractor_snapshot(
            {
                "station_id": "industrial-base",
                "system_name": "Peacock Station",
                "equipped_module_counts": {
                    "metal_drill": 100,
                    "metal_foundry": 10,
                    "ration_processor": 2,
                    "station_shield_mk1": 1,
                },
            }
        )
        assert record is not None
        self.assertEqual(record["moduleCounts"], {"metal_drill": 100, "metal_foundry": 10, "ration_processor": 2})
        self.assertEqual(record["resourceSlots"], {"metal_ore": 100})
        production = app.equipped_module_production_per_tick(
            record,
            [
                {"type": "metal_drill", "stats": {"Production": "8,640/day (Metal Ore)"}},
                {"type": "metal_foundry", "stats": {"Cycle Input": "1 Metal Ore", "Cycle Output": "1 Metal", "Cycle Time": "12 seconds"}},
                {"type": "ration_processor", "stats": {"Cycle Input": "2 Space Corn + 5 Credits", "Cycle Output": "1 Ration", "Cycle Time": "12 seconds"}},
            ],
            7_200,
        )

        self.assertEqual(production["outputs"], {"metal_ore": 72_000.0, "metal": 6_000.0, "rations": 1_200.0})
        self.assertEqual(production["inputs"], {"metal_ore": 6_000.0, "space_corn": 2_400.0})
        self.assertEqual(production["credits"], {"credits": 6_000.0})

    def test_colony_support_uses_the_limiting_server_basket_resource(self) -> None:
        basket = [
            {"resource": "metal_ore", "perCapita": 2},
            {"resource": "silicon", "perCapita": 5},
        ]
        estimate = app.colony_baseline_support_estimate(
            {"metal_ore": 554_400, "silicon": 7_200},
            basket,
        )

        self.assertEqual(estimate["supportedPopulation"], 1_440)
        self.assertEqual(estimate["limitingResources"], ["silicon"])
        self.assertEqual(estimate["missingResources"], [])
        incomplete = app.colony_baseline_support_estimate({"metal_ore": 554_400}, basket)
        self.assertIsNone(incomplete["supportedPopulation"])
        self.assertEqual(incomplete["missingResources"], ["silicon"])

    def test_ration_processor_projection_uses_logged_recipe_and_colony_demand(self) -> None:
        projection = app.ration_projection(
            75_600,
            7_200,
            [{
                "type": "ration_processor",
                "stats": {
                    "Cycle Input": "2 Space Corn + 5 Credits",
                    "Cycle Output": "1 Ration",
                    "Cycle Time": "12 seconds",
                },
            }],
            0.1,
        )

        self.assertEqual(projection["rationsPerTick"], 37_800.0)
        self.assertEqual(projection["processorsRequired"], 63)
        self.assertEqual(projection["creditsPerTick"], 189_000.0)
        self.assertEqual(projection["sustainablePopulation"], 378_000)

    def test_galaxy_projection_combines_body_capacity_current_tiers_and_ration_chain(self) -> None:
        extractor = app._normalise_extractor_snapshot(
            {
                "station_id": "terra-base",
                "station_name": "Terra Base",
                "system_name": "Cornworld",
                "planet_id": "terra-body",
                "equipped_module_counts": {"harvester": 1, "advanced_harvester": 1},
            }
        )
        assert extractor is not None
        projection = launcher.galaxy_extraction_projection(
            [
                {"system_name": "Cornworld", "planet_id": "terra-body", "planet_name": "Terra", "planet_type": "Terra", "extractors": {"space_corn": 10}},
                {"system_name": "Cornworld", "planet_id": "moon-body", "planet_name": "Moon", "planet_type": "Moon", "extractors": {"space_corn": 5}},
            ],
            [extractor],
            [{"stationId": "terra-base", "tickIntervalSeconds": 7_200, "basket": [{"resource": "rations", "perCapita": 0.1}]}],
            [
                {"type": "harvester", "stats": {"Production": "8,640/day (Space Corn)"}},
                {"type": "advanced_harvester", "stats": {"Production": "17,280/day (Space Corn)"}},
                {"type": "industrial_harvester", "stats": {"Production": "25,920/day (Space Corn)"}},
                {"type": "ration_processor", "stats": {"Cycle Input": "2 Space Corn + 5 Credits", "Cycle Output": "1 Ration", "Cycle Time": "12 seconds"}},
            ],
        )

        terra = next(row for row in projection["rows"] if row["bodyName"] == "Terra" and row["resource"] == "space_corn")
        self.assertEqual(terra["currentSlots"], 2)
        self.assertEqual(terra["tierSummary"], "1× T3  ·  1× T6")
        self.assertEqual(terra["currentPerTick"], 2_160.0)
        self.assertEqual(terra["maxSlots"], 30.0)
        self.assertEqual(terra["maxTier"], 9)
        self.assertEqual(terra["maxPerTick"], 64_800.0)
        self.assertEqual(projection["maximumByResource"], {"space_corn": 75_600.0})
        self.assertEqual(projection["ration"]["sustainablePopulation"], 378_000)

    def test_latest_private_colony_economy_snapshot_survives_log_rotation(self) -> None:
        previous = [{
            "stationId": "metal-base",
            "systemName": "Peacock Station",
            "tickIntervalSeconds": 7_200,
            "basket": [{"resource": "metal_ore", "perCapita": 2}],
            "observedAt": "2026-08-29 10:00:00",
        }]
        current = [{
            "stationId": "metal-base",
            "stationName": "Metal Base",
            "systemName": "Peacock Station",
            "tickIntervalSeconds": 7_200,
            "population": 1_200,
            "basket": [{"resource": "metal_ore", "perCapita": 2}],
            "observedAt": "2026-08-29 11:00:00",
        }]

        merged = archive_store._merge_private_colony_economy(previous, current)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stationName"], "Metal Base")
        self.assertEqual(merged[0]["population"], 1_200.0)
    def test_colony_panel_entries_use_server_tick_and_limiting_resource(self) -> None:
        extractor = app._normalise_extractor_snapshot(
            {
                "station_id": "metal-base",
                "station_name": "Metal Base",
                "system_name": "Peacock Station",
                "equipped_module_counts": {"metal_drill": 100},
            }
        )
        assert extractor is not None
        entries = launcher.colony_economy_entries(
            [extractor],
            [{
                "stationId": "metal-base",
                "systemName": "Peacock Station",
                "tickIntervalSeconds": 7_200,
                "population": 700,
                "basket": [{"resource": "metal_ore", "perCapita": 2}],
            }],
            [{"type": "metal_drill", "stats": {"Production": "8,640/day"}}],
        )

        self.assertEqual(entries[0]["outputEntries"], [("metal_ore", 72_000.0)])
        self.assertEqual(entries[0]["estimate"]["supportedPopulation"], 36_000)
        widget = _TextCapture()
        desktop = object.__new__(launcher.StarEmpireDesktop)
        desktop._insert_colony_economy_entries(widget, entries)
        self.assertIn("Metal Ore 72,000 / tick", widget.text)
        self.assertIn("36,000 population", widget.text)
        self.assertIn("ESTIMATED HEADROOM: 35,300", widget.text)

    def test_extractor_output_uses_the_observed_global_tick_without_local_colony_data(self) -> None:
        extractor = app._normalise_extractor_snapshot(
            {
                "station_id": "mats-1",
                "station_name": "MATS1",
                "system_name": "Mjolnir Hollows",
                "equipped_module_counts": {"metal_drill": 150, "silicon_drill": 84},
            }
        )
        assert extractor is not None
        entries = launcher.colony_economy_entries(
            [extractor],
            [{"stationId": "other-base", "tickIntervalSeconds": 7_200, "basket": [{"resource": "rations", "perCapita": 0.1}]}],
            [
                {"type": "metal_drill", "stats": {"Production": "8,640/day (Metal Ore)"}},
                {"type": "silicon_drill", "stats": {"Production": "8,640/day (Silicon)"}},
            ],
        )

        self.assertFalse(entries[0]["hasColonyData"])
        self.assertTrue(entries[0]["usesSharedTick"])
        self.assertEqual(entries[0]["outputEntries"], [("metal_ore", 108_000.0), ("silicon", 60_480.0)])
        widget = _TextCapture()
        desktop = object.__new__(launcher.StarEmpireDesktop)
        desktop._insert_colony_economy_entries(widget, entries)
        self.assertIn("LAST OBSERVED SERVER TICK: 2h 0m", widget.text)
        self.assertIn("Metal Ore 108,000 / tick", widget.text)
        self.assertIn("Silicon 60,480 / tick", widget.text)
        self.assertIn("support and population estimate", widget.text)

    def test_body_entries_keep_every_body_and_attach_named_base_output(self) -> None:
        record = app._normalise_extractor_snapshot(
            {
                "station_id": "base-1",
                "station_name": "Corn&Wood1",
                "system_name": "Mjolnir Hollows",
                "planet_id": "planet-1",
                "planet_name": "Belxayn",
                "equipped_module_counts": {"harvester": 10},
            }
        )
        assert record is not None
        entries = launcher.system_body_extraction_entries(
            [
                {"system_name": "Mjolnir Hollows", "planet_id": "planet-1", "planet_name": "Belxayn", "planet_type": "Terra", "isScanned": True, "extractors": {"space_corn": 40}},
                {"system_name": "Mjolnir Hollows", "planet_id": "moon-1", "planet_name": "Belxayn Moon", "planet_type": "Moon", "is_moon": True, "isScanned": False},
            ],
            [record],
            [{"stationId": "tick-source", "tickIntervalSeconds": 7_200, "basket": [{"resource": "rations", "perCapita": 0.1}]}],
            [{"type": "harvester", "stats": {"Production": "8,640/day (Space Corn)"}}],
            "Mjolnir Hollows",
        )
        self.assertEqual([entry["bodyName"] for entry in entries], ["Belxayn", "Belxayn Moon"])
        self.assertTrue(entries[0]["scanned"])
        self.assertFalse(entries[1]["scanned"])
        self.assertEqual(entries[0]["maxBases"], 3)
        self.assertEqual(entries[1]["maxBases"], 1)
        station = entries[0]["stations"][0]
        self.assertEqual(station["stationName"], "Corn&Wood1")
        self.assertEqual(station["outputEntries"], [("space_corn", 7_200.0)])
        self.assertEqual(entries[1]["stations"], [])

    def test_private_extractor_usage_cannot_enter_shared_intel(self) -> None:
        clean = sharing.sanitise_catalog(
            {
                "privateExtractorUsage": [{
                    "stationId": "metal-base",
                    "systemName": "Peacock Station",
                    "resourceSlots": {"metal_ore": 390},
                }],
                "privateColonyEconomy": [{
                    "stationId": "metal-base",
                    "systemName": "Peacock Station",
                    "tickIntervalSeconds": 7200,
                    "basket": [{"resource": "metal_ore", "perCapita": 2}],
                }],
                "map": {},
            }
        )

        self.assertNotIn("privateExtractorUsage", clean)
        self.assertNotIn("privateColonyEconomy", clean)


if __name__ == "__main__":
    unittest.main()
