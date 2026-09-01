from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from user_state import UserStateStore


class MapViewStateTests(unittest.TestCase):
    def test_extraction_systems_are_ordered_deduplicated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "user_data.json"
            store = UserStateStore(path)
            self.assertTrue(store.add_extraction_system("Peacock Station"))
            self.assertFalse(store.add_extraction_system("  peacock station  "))
            self.assertTrue(store.add_extraction_system("Keldeus Verge"))
            self.assertEqual(store.extraction_systems(), ["Peacock Station", "Keldeus Verge"])
            self.assertTrue(store.remove_extraction_system("PEACOCK STATION"))
            self.assertEqual(UserStateStore(path).extraction_systems(), ["Keldeus Verge"])

    def test_coalition_control_visibility_defaults_on_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "user_data.json"
            store = UserStateStore(path)
            self.assertTrue(store.map_view()["showCoalitionControl"])

            store.set_map_view(
                zoom=1.5,
                pan_x=12,
                pan_y=-8,
                show_names=True,
                show_coalition_control=False,
                selected_system="Alpha",
                overlay_sizes={"detail": (540, 360), "search": (999, 999)},
            )

            restored = UserStateStore(path).map_view()
            self.assertFalse(restored["showCoalitionControl"])
            self.assertEqual(restored["overlaySizes"], {"detail": [540, 360]})


if __name__ == "__main__":
    unittest.main()
