from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from user_state import UserStateStore


class MapViewStateTests(unittest.TestCase):
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
