from __future__ import annotations

import math
from types import SimpleNamespace
import unittest

from PIL import Image, ImageChops, ImageDraw

from launcher import (
    StarEmpireDesktop,
    _territory_label_raster,
    centered_curved_label_path,
    connected_map_systems,
    curved_label_glyph_layout,
    kerning_label_advances,
    paint_straight_map_label,
    smooth_label_path,
    territory_label_fallback_angle,
    territory_map_geometry,
)


def system(name: str, x: float, y: float, ownership: str = "") -> dict:
    return {"name": name, "x": x, "y": y, "hasPosition": True, "ownership": ownership}


class MapGeometryTests(unittest.TestCase):
    def test_whole_label_raster_is_reused_between_map_redraws(self) -> None:
        _territory_label_raster.cache_clear()

        first = _territory_label_raster("Europa Stellaris", 14, -420)
        second = _territory_label_raster("Europa Stellaris", 14, -420)

        self.assertIs(first, second)
        self.assertEqual(1, _territory_label_raster.cache_info().hits)

    def test_kerning_advances_preserve_the_fonts_complete_text_width(self) -> None:
        class KerningFont:
            widths = {"A": 10.0, "AV": 18.0, "AVA": 27.0}

            def getlength(self, text: str) -> float:
                return self.widths.get(text, 9.0)

        advances = kerning_label_advances("AVA", KerningFont())

        self.assertEqual((10.0, 8.0, 9.0), advances)
        self.assertEqual(27.0, sum(advances))

    def test_smoothed_label_path_retains_midpoint_and_softens_hinges(self) -> None:
        source = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (100.0, 50.0)]
        smoothed = smooth_label_path(source)

        def midpoint(points: list[tuple[float, float]] | tuple[tuple[float, float], ...]):
            lengths = [0.0]
            for start, end in zip(points, points[1:]):
                lengths.append(lengths[-1] + math.dist(start, end))
            target = lengths[-1] / 2.0
            for index in range(1, len(points)):
                if target <= lengths[index]:
                    ratio = (target - lengths[index - 1]) / (lengths[index] - lengths[index - 1])
                    return (
                        points[index - 1][0] + (points[index][0] - points[index - 1][0]) * ratio,
                        points[index - 1][1] + (points[index][1] - points[index - 1][1]) * ratio,
                    )
            return points[-1]

        def maximum_turn(points: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> float:
            angles = [
                math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
                for start, end in zip(points, points[1:])
            ]
            return max(
                abs((second - first + 180.0) % 360.0 - 180.0)
                for first, second in zip(angles, angles[1:])
            )

        self.assertAlmostEqual(midpoint(source)[0], midpoint(smoothed)[0], places=6)
        self.assertAlmostEqual(midpoint(source)[1], midpoint(smoothed)[1], places=6)
        self.assertLess(maximum_turn(smoothed), maximum_turn(source))

    def test_curved_label_angles_form_a_smooth_non_rigid_bend(self) -> None:
        placements = curved_label_glyph_layout(
            [(0.0, 0.0), (30.0, -15.0), (60.0, 0.0), (90.0, 15.0), (120.0, 0.0)],
            [8.0] * 11,
            tracking=0.0,
            max_angle_degrees=24.0,
        )
        angles = [angle for _x, _y, angle in placements]

        self.assertGreater(max(angles) - min(angles), 4.0)
        self.assertLess(max(abs(right - left) for left, right in zip(angles, angles[1:])), 9.0)

    def test_straight_label_is_horizontal_centred_and_inside_territory(self) -> None:
        image = Image.new("RGBA", (180, 90), (0, 0, 0, 0))
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).polygon(
            [(18, 18), (162, 18), (162, 72), (18, 72)],
            fill=255,
        )

        painted = paint_straight_map_label(
            image,
            "Normal Union",
            (90.0, 45.0),
            14,
            mask,
            preferred_angle_degrees=45.0,
        )

        self.assertTrue(painted)
        alpha_bbox = image.getchannel("A").getbbox()
        self.assertIsNotNone(alpha_bbox)
        self.assertGreater(alpha_bbox[2] - alpha_bbox[0], alpha_bbox[3] - alpha_bbox[1])
        self.assertAlmostEqual(90.0, (alpha_bbox[0] + alpha_bbox[2]) / 2.0, delta=1.0)
        self.assertAlmostEqual(45.0, (alpha_bbox[1] + alpha_bbox[3]) / 2.0, delta=1.0)
        self.assertIsNone(
            ImageChops.subtract(image.getchannel("A"), mask).getbbox()
        )

    def test_small_label_angles_as_one_line_to_gain_readable_size(self) -> None:
        image = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).polygon(
            [(20, 100), (100, 20), (130, 50), (50, 130)],
            fill=255,
        )

        painted = paint_straight_map_label(
            image,
            "ANGLE NAME",
            (80.0, 80.0),
            18,
            mask,
            minimum_font_pixels=6,
            preferred_angle_degrees=-45.0,
            minimum_readable_font_pixels=9,
        )

        self.assertTrue(painted)
        alpha_bbox = image.getchannel("A").getbbox()
        self.assertIsNotNone(alpha_bbox)
        self.assertGreater(alpha_bbox[2] - alpha_bbox[0], 50)
        self.assertGreater(alpha_bbox[3] - alpha_bbox[1], 50)
        self.assertIsNone(
            ImageChops.subtract(image.getchannel("A"), mask).getbbox()
        )

    def test_fallback_angle_uses_central_direction_and_stays_readable(self) -> None:
        path = [(0.0, 0.0), (0.0, 50.0), (20.0, 70.0), (40.0, 90.0), (40.0, 140.0)]

        self.assertAlmostEqual(45.0, territory_label_fallback_angle(path), places=6)
        self.assertAlmostEqual(
            45.0,
            territory_label_fallback_angle(list(reversed(path))),
            places=6,
        )
        self.assertAlmostEqual(
            30.0,
            territory_label_fallback_angle(path, max_angle_degrees=30.0),
            places=6,
        )

    def test_zoom_events_coalesce_before_one_expensive_redraw(self) -> None:
        class ScheduledRoot:
            def __init__(self) -> None:
                self.calls = []

            def after(self, delay, callback):
                self.calls.append((delay, callback))
                return f"after-{len(self.calls)}"

        root = ScheduledRoot()
        labels = []
        redraws = []
        desktop = object.__new__(StarEmpireDesktop)
        desktop.root = root
        desktop.map_canvas = SimpleNamespace(
            winfo_width=lambda: 800,
            winfo_height=lambda: 600,
        )
        desktop.map_zoom_label = SimpleNamespace(
            configure=lambda **values: labels.append(values["text"])
        )
        desktop.map_zoom = 1.0
        desktop.map_pan_x = 0.0
        desktop.map_pan_y = 0.0
        desktop.map_zoom_redraw_after = None
        desktop._draw_map = lambda: redraws.append(True)

        desktop._zoom_map_at(1.18, 400.0, 300.0)
        desktop._zoom_map_at(1.18, 400.0, 300.0)

        self.assertEqual(1, len(root.calls))
        self.assertEqual(16, root.calls[0][0])
        self.assertEqual([], redraws)
        self.assertEqual("139%", labels[-1])

        root.calls[0][1]()

        self.assertEqual([True], redraws)
        self.assertIsNone(desktop.map_zoom_redraw_after)

    def test_pan_moves_existing_items_then_redraws_once_on_release(self) -> None:
        moves = []
        redraws = []
        desktop = object.__new__(StarEmpireDesktop)
        desktop.map_canvas = SimpleNamespace(
            move=lambda *values: moves.append(values)
        )
        desktop.map_drag_origin = (20, 30)
        desktop.map_pan_x = 5.0
        desktop.map_pan_y = -2.0
        desktop._draw_map = lambda: redraws.append(True)

        desktop._map_pan_move(SimpleNamespace(x=28, y=41))

        self.assertEqual(13.0, desktop.map_pan_x)
        self.assertEqual(9.0, desktop.map_pan_y)
        self.assertEqual([("map-world", 8, 11)], moves)
        self.assertEqual([], redraws)

        desktop._map_pan_end()

        self.assertEqual([True], redraws)
        self.assertIsNone(desktop.map_drag_origin)

    def test_straight_label_remains_renderable_at_overview_and_detail_scale(self) -> None:
        for scale in (1, 4):
            with self.subTest(scale=scale):
                image = Image.new("RGBA", (180 * scale, 90 * scale), (0, 0, 0, 0))
                mask = Image.new("L", image.size, 0)
                ImageDraw.Draw(mask).polygon(
                    [
                        (18 * scale, 18 * scale),
                        (162 * scale, 18 * scale),
                        (162 * scale, 72 * scale),
                        (18 * scale, 72 * scale),
                    ],
                    fill=255,
                )

                painted = paint_straight_map_label(
                    image,
                    "Normal Union",
                    (90.0 * scale, 45.0 * scale),
                    14 * scale,
                    mask,
                )

                self.assertTrue(painted)
                self.assertIsNone(
                    ImageChops.subtract(image.getchannel("A"), mask).getbbox()
                )

    def test_straight_label_is_omitted_when_full_name_cannot_fit_inside(self) -> None:
        image = Image.new("RGBA", (120, 70), (0, 0, 0, 0))
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).polygon(
            [(48, 25), (72, 25), (72, 45), (48, 45)],
            fill=255,
        )

        painted = paint_straight_map_label(
            image,
            "The Black Company",
            (60.0, 35.0),
            12,
            mask,
            minimum_font_pixels=6,
        )

        self.assertFalse(painted)
        self.assertIsNone(image.getchannel("A").getbbox())

    def test_short_curved_label_path_extends_equally_without_losing_bend(self) -> None:
        source = [(0.0, 0.0), (25.0, 8.0), (50.0, 0.0)]
        extended = centered_curved_label_path(source, 110.0)

        length = sum(
            math.dist(start, end)
            for start, end in zip(extended, extended[1:])
        )
        self.assertGreaterEqual(length, 110.0)
        self.assertAlmostEqual(25.0, extended[1][0], places=6)
        self.assertAlmostEqual(8.0, extended[1][1], places=6)
        self.assertAlmostEqual(
            extended[1][0] * 2.0,
            extended[0][0] + extended[-1][0],
            places=6,
        )
        self.assertGreater(extended[1][1], extended[0][1])

    def test_centered_curved_path_rotates_steep_shapes_into_readable_chord(self) -> None:
        readable = centered_curved_label_path(
            [(10.0, 0.0), (14.0, 50.0), (10.0, 100.0)],
            120.0,
        )

        chord_angle = abs(
            math.degrees(
                math.atan2(
                    readable[-1][1] - readable[0][1],
                    readable[-1][0] - readable[0][0],
                )
            )
        )
        self.assertLessEqual(chord_angle, 22.000001)
        self.assertAlmostEqual(14.0, readable[1][0], places=6)
        self.assertAlmostEqual(50.0, readable[1][1], places=6)

    def test_asymmetric_curve_extension_retains_original_arc_midpoint(self) -> None:
        source = [(0.0, 0.0), (50.0, 0.0), (50.0, 10.0), (60.0, 10.0)]
        anchor = (35.0, 0.0)
        extended = centered_curved_label_path(source, 300.0)
        anchor_index = next(
            index
            for index, point in enumerate(extended)
            if math.dist(point, anchor) <= 1e-6
        )
        left_length = sum(
            math.dist(start, end)
            for start, end in zip(extended[:anchor_index + 1], extended[1:anchor_index + 1])
        )
        right_length = sum(
            math.dist(start, end)
            for start, end in zip(extended[anchor_index:], extended[anchor_index + 1:])
        )

        self.assertAlmostEqual(left_length, right_length, places=5)
        self.assertGreaterEqual(left_length + right_length, 300.0)

    def test_curved_label_layout_keeps_text_on_path_midpoint(self) -> None:
        path = [(0.0, 0.0), (50.0, 18.0), (100.0, 0.0)]
        placements = curved_label_glyph_layout(
            path,
            [10.0, 10.0, 10.0, 10.0],
            tracking=2.0,
        )

        self.assertEqual(4, len(placements))
        self.assertAlmostEqual(100.0, placements[0][0] + placements[-1][0], places=6)
        self.assertAlmostEqual(placements[0][1], placements[-1][1], places=6)

    def test_curved_label_layout_normalises_reading_direction(self) -> None:
        path = [(0.0, 0.0), (40.0, 12.0), (80.0, 4.0)]
        forward = curved_label_glyph_layout(path, [9.0, 11.0, 10.0])
        reversed_path = curved_label_glyph_layout(list(reversed(path)), [9.0, 11.0, 10.0])

        self.assertEqual(forward, reversed_path)
        self.assertLess(forward[0][0], forward[-1][0])

    def test_curved_label_layout_rejects_overflow_and_caps_angles(self) -> None:
        vertical = curved_label_glyph_layout(
            [(10.0, 0.0), (10.0, 100.0)],
            [8.0, 8.0, 8.0],
            max_angle_degrees=40.0,
        )

        self.assertTrue(vertical)
        self.assertTrue(all(abs(angle) <= 40.0 for _x, _y, angle in vertical))
        self.assertEqual(
            (),
            curved_label_glyph_layout([(0.0, 0.0), (20.0, 0.0)], [12.0, 12.0]),
        )

    def test_connected_map_hides_unlinked_systems_without_mutating_input(self) -> None:
        galaxy = {
            "systems": [system("Alpha", 0, 0), system("Beta", 1, 0), system("Lone", 9, 9)],
            "edges": [{"source": "Alpha", "target": "Beta"}],
        }

        visible, edges = connected_map_systems(galaxy)

        self.assertEqual(["Alpha", "Beta"], [row["name"] for row in visible])
        self.assertEqual(galaxy["edges"], edges)
        self.assertEqual("Lone", galaxy["systems"][2]["name"])

    def test_territory_uses_hidden_systems_without_rendering_their_nodes(self) -> None:
        galaxy = {
            "systems": [
                system("Alpha", 0, 0),
                system("Beta", 2, 0),
                system("Hidden claim", 1, 2),
            ],
            "edges": [{"source": "Alpha", "target": "Beta"}],
            "territoryPositions": {
                "Alpha": {"coord_x": 0, "coord_y": 0},
                "Beta": {"coord_x": 2, "coord_y": 0},
                "Hidden claim": {"coord_x": 1, "coord_y": 2},
            },
            "territory": {
                "Hidden claim": {
                    "coalition_id": 9,
                    "coalition_name": "Real Coalition",
                    "color": "#abcdef",
                }
            },
        }

        visible, _edges = connected_map_systems(galaxy)
        cells, regions = territory_map_geometry(galaxy)

        self.assertEqual(["Alpha", "Beta"], [row["name"] for row in visible])
        self.assertEqual(["Hidden claim"], [cell.system_name for cell in cells])
        self.assertEqual("Real Coalition", regions[0].coalition_name)


if __name__ == "__main__":
    unittest.main()
