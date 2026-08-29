from __future__ import annotations

import unittest

import launcher


class ItemNumericSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = {
            "id": "engine:swift_drive",
            "name": "Swift Drive",
            "category": "engine",
            "tech": 4,
            "cargoSize": 3,
            "stats": {
                "Max Speed": 15,
                "Damage": 120,
                "Max Range": 35,
            },
        }
        self.markets = [{
            "stationName": "Test Station",
            "stock": 4,
            "buyPrice": 90,
            "sellPrice": 110,
        }]

    def test_item_speed_comparison_uses_the_displayed_speed_stat(self) -> None:
        self.assertTrue(launcher.item_matches_query(self.item, "speed>=10"))
        self.assertTrue(launcher.item_matches_query(self.item, "SPEED<16"))
        self.assertFalse(launcher.item_matches_query(self.item, "speed>15"))

    def test_station_item_speed_comparison_matches_the_same_item_stat(self) -> None:
        self.assertTrue(
            launcher.station_item_matches_query(self.item, self.markets, "speed>=10")
        )
        self.assertFalse(
            launcher.station_item_matches_query(self.item, self.markets, "speed<15")
        )


if __name__ == "__main__":
    unittest.main()
