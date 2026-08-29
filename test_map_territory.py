from __future__ import annotations

import unittest

from app import _map_dataset
from map_territory import (
    DEFAULT_TERRITORY_COLOR,
    TerritoryCell,
    build_territory_cells,
    build_territory_label_regions,
    normalize_positions_snapshot,
    normalize_territory_snapshot,
    territory_label_font_pixels,
)
from launcher import map_fit_bounds


class TerritorySnapshotTests(unittest.TestCase):
    def test_snapshot_normalisation_keeps_only_public_valid_fields(self) -> None:
        territory = normalize_territory_snapshot(
            {
                " Alpha ": {
                    "coalition_id": "7",
                    "coalition_name": " Azure Union ",
                    "color": "12ABef",
                    "private": "discard me",
                },
                "Invalid": {"coalition_id": 0, "coalition_name": "No"},
                "Broken": "not a row",
                " alpha ": {
                    "coalition_id": 99,
                    "coalition_name": "Duplicate",
                    "color": "#ffffff",
                },
            }
        )
        positions = normalize_positions_snapshot(
            {
                " Alpha ": {"coord_x": "1.5", "coord_y": 2, "secret": True},
                "Bad": {"coord_x": "nan", "coord_y": 3},
            }
        )

        self.assertEqual(
            {
                "Alpha": {
                    "coalition_id": 7,
                    "coalition_name": "Azure Union",
                    "color": "#12abef",
                }
            },
            territory,
        )
        self.assertEqual({"Alpha": {"coord_x": 1.5, "coord_y": 2.0}}, positions)

    def test_map_dataset_retains_authoritative_territory_and_all_positions(self) -> None:
        snapshots = {
            "galaxyStatic": {
                "observedAt": "2026-08-29T10:00:00Z",
                "data": {
                    "positions": {
                        "Alpha": {"coord_x": 0, "coord_y": 0},
                        "Beta": {"coord_x": 1, "coord_y": 0},
                        "Hidden clip site": {"coord_x": 0.5, "coord_y": 1},
                    },
                    "territory": {
                        "Alpha": {
                            "coalition_id": 4,
                            "coalition_name": "The Blazing Phoenix",
                            "color": "#ff4500",
                        }
                    },
                },
            },
            "exploredSystems": {
                "data": [
                    {
                        "name": "Alpha",
                        "system_id": 1,
                        "warp_gates": [{"target_system": "Beta"}],
                    }
                ]
            },
        }

        galaxy = _map_dataset(snapshots, [])

        self.assertEqual(1, galaxy["territoryCount"])
        self.assertEqual("The Blazing Phoenix", galaxy["territory"]["Alpha"]["coalition_name"])
        self.assertEqual(3, len(galaxy["territoryPositions"]))
        self.assertIn("Hidden clip site", galaxy["territoryPositions"])


class TerritoryGeometryTests(unittest.TestCase):
    @staticmethod
    def _path_length(points) -> float:
        return sum(
            ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            for a, b in zip(points, points[1:])
        )

    @staticmethod
    def _inside(point, polygon) -> bool:
        x, y = point
        inside = False
        previous_x, previous_y = polygon[-1]
        for current_x, current_y in polygon:
            if (current_y > y) != (previous_y > y):
                crossing_x = (
                    (previous_x - current_x) * (y - current_y)
                    / (previous_y - current_y) + current_x
                )
                if x < crossing_x:
                    inside = not inside
            previous_x, previous_y = current_x, current_y
        return inside

    @staticmethod
    def _label_cell(name, polygon, neighbours=()) -> TerritoryCell:
        return TerritoryCell(
            system_name=name,
            coalition_id=7,
            coalition_name="Curved Union",
            color=(80, 180, 255),
            polygon=tuple(polygon),
            boundary_segments=(),
            same_coalition_neighbors=tuple(neighbours),
        )

    def test_cells_use_true_assignments_and_public_colours(self) -> None:
        positions = {
            "Alpha": {"coord_x": 0, "coord_y": 0},
            "Beta": {"coord_x": 2, "coord_y": 0},
            "Unowned": {"coord_x": 4, "coord_y": 0},
        }
        territory = {
            "Alpha": {"coalition_id": 1, "coalition_name": "Azure", "color": "#123456"},
            "Beta": {"coalition_id": 1, "coalition_name": "Azure", "color": "#123456"},
        }

        cells = build_territory_cells(positions, territory)
        regions = build_territory_label_regions(cells)

        self.assertEqual({"Alpha", "Beta"}, {cell.system_name for cell in cells})
        self.assertEqual({(18, 52, 86)}, {cell.color for cell in cells})
        self.assertEqual(1, len(regions))
        self.assertEqual("Azure", regions[0].coalition_name)
        self.assertEqual(("Alpha", "Beta"), regions[0].system_names)

    def test_unowned_position_clips_claim_and_missing_colour_uses_fallback(self) -> None:
        positions = {
            "Claimed": {"coord_x": 0, "coord_y": 0},
            "Unowned": {"coord_x": 2, "coord_y": 0},
        }
        territory = {
            "Claimed": {"coalition_id": 2, "coalition_name": "Neutral", "color": None}
        }

        (cell,) = build_territory_cells(positions, territory)

        self.assertEqual(DEFAULT_TERRITORY_COLOR, cell.color)
        self.assertLessEqual(max(point[0] for point in cell.polygon), 1.0 + 1.0e-7)

    def test_same_coalition_suppresses_shared_frontier_but_different_coalitions_keep_it(self) -> None:
        positions = {
            "Alpha": {"coord_x": 0, "coord_y": 0},
            "Beta": {"coord_x": 2, "coord_y": 0},
        }
        same = build_territory_cells(
            positions,
            {
                "Alpha": {"coalition_id": 1, "coalition_name": "One"},
                "Beta": {"coalition_id": 1, "coalition_name": "One"},
            },
        )
        divided = build_territory_cells(
            positions,
            {
                "Alpha": {"coalition_id": 1, "coalition_name": "One"},
                "Beta": {"coalition_id": 2, "coalition_name": "Two"},
            },
        )

        same_by_name = {cell.system_name: cell for cell in same}
        self.assertIn("Beta", same_by_name["Alpha"].same_coalition_neighbors)
        self.assertFalse(
            any(abs(a[0] - 1.0) < 1.0e-7 and abs(b[0] - 1.0) < 1.0e-7
                for a, b in same_by_name["Alpha"].boundary_segments)
        )
        self.assertTrue(
            any(abs(a[0] - 1.0) < 1.0e-7 and abs(b[0] - 1.0) < 1.0e-7
                for cell in divided for a, b in cell.boundary_segments)
        )

    def test_sparse_edge_claim_is_capped_to_local_spacing(self) -> None:
        (cell,) = build_territory_cells(
            {
                "Claimed": {"coord_x": 0, "coord_y": 0},
                "Distant": {"coord_x": 10, "coord_y": 0},
            },
            {"Claimed": {"coalition_id": 3, "coalition_name": "Cap"}},
        )

        self.assertGreaterEqual(min(point[0] for point in cell.polygon), -9.000001)
        self.assertLessEqual(max(point[0] for point in cell.polygon), 5.000001)

    def test_fit_bounds_ignore_territory_and_label_size_tracks_area(self) -> None:
        positioned = [
            {"name": "Alpha", "x": 0, "y": 0},
            {"name": "Beta", "x": 2, "y": 3},
        ]

        self.assertEqual((0.0, 0.0, 2.0, 3.0), map_fit_bounds(positioned))
        self.assertEqual(10, territory_label_font_pixels(0.0, 1.0))
        self.assertGreater(
            territory_label_font_pixels(10000.0, 2.0),
            territory_label_font_pixels(100.0, 2.0),
        )

    def test_label_path_follows_bend_but_stays_centred_and_inside(self) -> None:
        cells = (
            self._label_cell("Left", ((0, 0), (2, 0), (2, 2), (0, 2)), ("Middle",)),
            self._label_cell("Middle", ((2, 1), (4, 1), (4, 3), (2, 3)), ("Left", "Right")),
            self._label_cell("Right", ((4, 0), (6, 0), (6, 2), (4, 2)), ("Middle",)),
        )

        (region,) = build_territory_label_regions(cells)
        path = region.label_path
        anchor_index = min(
            range(len(path)),
            key=lambda index: (
                (path[index][0] - region.anchor[0]) ** 2
                + (path[index][1] - region.anchor[1]) ** 2
            ),
        )

        self.assertGreaterEqual(len(path), 7)
        self.assertAlmostEqual(region.anchor[0], path[anchor_index][0], places=7)
        self.assertAlmostEqual(region.anchor[1], path[anchor_index][1], places=7)
        self.assertAlmostEqual(
            self._path_length(path[:anchor_index + 1]),
            self._path_length(path[anchor_index:]),
            places=7,
        )
        self.assertGreater(max(point[1] for point in path) - min(point[1] for point in path), 0.25)
        for first, second in zip(path, path[1:]):
            for amount in (0.0, 0.5, 1.0):
                point = (
                    first[0] + (second[0] - first[0]) * amount,
                    first[1] + (second[1] - first[1]) * amount,
                )
                self.assertTrue(any(self._inside(point, cell.polygon) for cell in cells))

    def test_label_path_is_deterministic_when_cells_are_reordered(self) -> None:
        cells = (
            self._label_cell("Left", ((0, 0), (2, 0), (2, 2), (0, 2)), ("Right",)),
            self._label_cell("Right", ((2, 0.5), (5, 0.5), (5, 2.5), (2, 2.5)), ("Left",)),
        )

        first = build_territory_label_regions(cells)[0].label_path
        second = build_territory_label_regions(tuple(reversed(cells)))[0].label_path

        self.assertEqual(
            [tuple(round(value, 9) for value in point) for point in first],
            [tuple(round(value, 9) for value in point) for point in second],
        )


if __name__ == "__main__":
    unittest.main()
