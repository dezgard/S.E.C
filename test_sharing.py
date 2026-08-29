from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path

from archive_store import ArchiveStore
import sharing


def catalog() -> dict:
    return {
        "meta": {"logPath": "C:/private/star_empire_client.log"},
        "items": [{
            "id": "weapon:laser", "name": "Laser", "category": "weapon",
            "stats": {"Damage": 12},
            "markets": [{
                "stationId": "station-1", "stationName": "Alpha Market",
                "source": "NPC_STATION_DOCK_OK", "buyPrice": 99,
                "observedAt": "2026-08-29 12:00:00", "accountToken": "never-share",
            }],
        }],
        "stations": [{
            "id": "station-1", "name": "Alpha Market", "systemName": "Alpha",
            "isMine": True, "lastSeen": "2026-08-29 12:00:00",
        }],
        "scans": [{
            "planet_id": "planet-1", "planet_name": "Alpha I", "planet_type": "terran",
            "system_name": "Alpha", "resources": {"iron": 5},
            "owner": "private-player", "observedAt": "2026-08-29 12:00:00",
        }],
        "training": {"offers": [{
            "skillId": "piloting", "stationId": "station-1", "stationName": "Alpha Market",
            "currentLevel": 8, "availableCredits": 900000, "offeredMax": 10,
            "observedAt": "2026-08-29 12:00:00",
        }]},
        "player": {"hasData": True, "credits": 123456, "accountId": "private"},
        "ship": {"hasData": True, "inventory": [{"item_type": "private"}]},
        "map": {
            "hasData": True,
            "systems": [{
                "id": "alpha", "name": "Alpha", "x": 1, "y": 2,
                "npcStationCount": 1, "stationIds": ["station-1"],
                "stationCounts": {"mine": 1, "coalition": 3, "others": 2},
                "ownership": "coalition", "owner": "private-player",
            }],
            "edges": [{"source": "Alpha", "target": "Beta", "private": True}],
            "territory": {
                "Alpha": {
                    "coalition_id": 4,
                    "coalition_name": "The Blazing Phoenix",
                    "color": "#ff4500",
                    "private": "discard me",
                }
            },
            "territoryPositions": {
                "Alpha": {"coord_x": 1, "coord_y": 2, "private": True},
                "Beta": {"coord_x": 3, "coord_y": 4},
            },
        },
    }


class SharedIntelTests(unittest.TestCase):
    def test_export_excludes_personal_and_preserves_public_control_data(self) -> None:
        bundle = sharing.create_bundle(catalog())
        public = bundle["catalog"]
        self.assertEqual({"hasData": False}, public["player"])
        self.assertEqual({"hasData": False}, public["ship"])
        self.assertNotIn("isMine", public["stations"][0])
        self.assertNotIn("accountToken", public["items"][0]["markets"][0])
        self.assertNotIn("owner", public["scans"][0])
        self.assertNotIn("stationIds", public["map"]["systems"][0])
        self.assertEqual({"coalition": 3, "others": 2}, public["map"]["systems"][0]["stationCounts"])
        self.assertNotIn("mine", public["map"]["systems"][0]["stationCounts"])
        self.assertEqual("coalition", public["map"]["systems"][0]["ownership"])
        self.assertEqual(
            {
                "coalition_id": 4,
                "coalition_name": "The Blazing Phoenix",
                "color": "#ff4500",
            },
            public["map"]["territory"]["Alpha"],
        )
        self.assertEqual(
            {"coord_x": 1.0, "coord_y": 2.0},
            public["map"]["territoryPositions"]["Alpha"],
        )
        self.assertEqual(1, bundle["summary"]["territorySystems"])
        self.assertNotIn("currentLevel", public["training"]["offers"][0])
        self.assertEqual(1, bundle["summary"]["scans"])

    def test_import_re_sanitises_an_edited_bundle_and_preserves_local_player(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive = ArchiveStore(Path(folder) / "archive.json")
            archive.merge(catalog())
            bundle_path = Path(folder) / "community.secintel.json"
            bundle = sharing.write_bundle(bundle_path, catalog())
            bundle["catalog"]["player"] = {"hasData": True, "credits": 1}
            bundle["catalog"]["stations"][0]["isMine"] = True
            bundle["catalog"]["map"]["systems"][0]["id"] = "42"
            bundle["catalog"]["map"]["systems"][0]["x"] = 99
            bundle["catalog"]["map"]["systems"][0]["y"] = 98
            bundle["catalog"]["map"]["territory"]["Alpha"]["coalition_id"] = 9
            bundle["catalog"]["map"]["territoryPositions"] = {
                "Alpha": {"coord_x": 99, "coord_y": 98, "secret": True}
            }
            bundle["catalog"]["map"]["territory"]["Alpha"]["account"] = "private"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

            imported, merged = sharing.import_bundle(bundle_path, archive)

            self.assertEqual({"hasData": False}, imported["catalog"]["player"])
            self.assertNotIn("isMine", imported["catalog"]["stations"][0])
            self.assertTrue(merged["player"]["hasData"])
            self.assertEqual(123456, merged["player"]["credits"])
            self.assertEqual("Alpha I", merged["scans"][0]["planet_name"])
            alpha_rows = [
                row for row in merged["map"]["systems"]
                if str(row.get("name") or "").casefold() == "alpha"
            ]
            self.assertEqual(1, len(alpha_rows))
            self.assertEqual("42", alpha_rows[0]["id"])
            self.assertEqual((1, 2), (alpha_rows[0]["x"], alpha_rows[0]["y"]))
            self.assertEqual(4, merged["map"]["territory"]["Alpha"]["coalition_id"])
            self.assertEqual(
                {"Alpha", "Beta"}, set(merged["map"]["territoryPositions"])
            )
            self.assertNotIn("account", imported["catalog"]["map"]["territory"]["Alpha"])
            self.assertNotIn(
                "secret",
                imported["catalog"]["map"]["territoryPositions"]["Alpha"],
            )

    def test_blank_archive_accepts_shared_territory_then_local_snapshot_can_clear_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive = ArchiveStore(Path(folder) / "archive.json")
            bundle_path = Path(folder) / "community.secintel.json"
            sharing.write_bundle(bundle_path, catalog())

            _bundle, imported = sharing.import_bundle(bundle_path, archive)

            self.assertEqual(4, imported["map"]["territory"]["Alpha"]["coalition_id"])
            self.assertEqual({"Alpha", "Beta"}, set(imported["map"]["territoryPositions"]))

            local = copy.deepcopy(catalog())
            local["map"]["territory"] = {}
            cleared = archive.merge(local)

            self.assertEqual({}, cleared["map"]["territory"])
            self.assertEqual({"Alpha", "Beta"}, set(cleared["map"]["territoryPositions"]))

    def test_rejects_unrecognised_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "not-secintel.json"
            path.write_text('{"format":"other","version":1,"catalog":{}}', encoding="utf-8")
            with self.assertRaises(sharing.SharedIntelError):
                sharing.read_bundle(path)
