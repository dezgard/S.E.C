from __future__ import annotations

import ctypes
import csv
import datetime
from functools import lru_cache
import hashlib
import math
import os
import queue
import re
import shlex
import sys
import textwrap
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageTk

import app
import fitting
import game_link
import sharing
import updater
from map_territory import (
    TerritoryCell,
    TerritoryLabelRegion,
    build_territory_cells,
    build_territory_label_regions,
    territory_label_font_pixels,
    territory_rgb,
)
from user_state import UserStateStore, scan_annotation_key


BG = "#050b14"
PANEL = "#091625"
PANEL_2 = "#0d2034"
PANEL_3 = "#102a42"
LINE = "#1b3b54"
LINE_BRIGHT = "#2d6f91"
TEXT = "#d7e9fb"
MUTED = "#7893ad"
CYAN = "#35d8ff"
MINT = "#6ff4bd"
AMBER = "#ffc857"
RED = "#ff7384"
BLUE = "#3794ff"

FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
MONO = ("Cascadia Mono", 9)
MONO_SMALL = ("Cascadia Mono", 8)

# Galaxy map, styled to follow the in-game map.  Discs scale with zoom, so the
# radius bounds and the label threshold below decide how it reads at each end
# of the zoom range.
MAP_ZOOM_MIN = 0.35
MAP_ZOOM_MAX = 64.0
MAP_NODE_MIN_RADIUS = 1.4
MAP_NODE_MAX_RADIUS = 26.0
# Fraction of the typical gap between systems a disc may occupy.  Below 0.5
# discs cannot touch even where systems sit closer than average.
MAP_NODE_GAP_FRACTION = 0.34
# A system name is roughly 70px wide, so names only appear once systems are at
# least that far apart on screen.  The budget is a safety net for dense views.
# Keep labels available at exactly twice the previous overview distance.  The
# grid prevents the higher budget from turning a dense sector into one block
# of text.
MAP_LABEL_GAP = 35.0
MAP_LABEL_BUDGET = 520
MAP_LABEL_CELL = 40.0
MAP_GRID = "#0b1b2b"
MAP_EDGE = "#1d4368"
MAP_EDGE_LIT = "#2770a4"
MAP_SYSTEM_FILL = "#23405f"
MAP_SYSTEM_RIM = "#6f9fc8"
MAP_STATION_FILL = "#1f6b66"
MAP_STATION_RIM = "#cfe9e6"
MAP_UNKNOWN_FILL = "#16283c"
MAP_UNKNOWN_RIM = "#24384f"
MAP_UNKNOWN_LABEL = "#4f6f8f"
MAP_SELECTED_FILL = "#2f7fd0"
MAP_SELECTED_RIM = "#ffd66b"
MAP_HIGHLIGHT_FILL = "#8a6a1f"
MAP_HIGHLIGHT_RIM = "#ffd9a0"
MAP_LABEL = "#e6f0f8"
MAP_TAG_FILL = "#0d1a26"
MAP_HAZARD_FILL = "#1a1005"
MAP_HAZARD_RIM = "#c8752a"
MAP_HAZARD_TEXT = "#e8a054"
MAP_CANVAS_BG = "#050912"
MAP_CANVAS_BG_RGB = (5, 9, 18)
MAP_TERRITORY_LEGEND = "#7e8da8"
MAP_TERRITORY_LABEL = "#e4edfa"
MAP_TERRITORY_LABEL_SHADOW = "#02050c"


def _label_path_point(
    points: tuple[tuple[float, float], ...],
    distances: tuple[float, ...],
    distance: float,
) -> tuple[float, float]:
    """Return the interpolated point at an arc distance along a polyline."""
    target = max(0.0, min(float(distance), distances[-1]))
    for index in range(1, len(points)):
        if target > distances[index]:
            continue
        span = distances[index] - distances[index - 1]
        if span <= 1e-9:
            return points[index]
        ratio = (target - distances[index - 1]) / span
        start = points[index - 1]
        end = points[index]
        return (
            start[0] + (end[0] - start[0]) * ratio,
            start[1] + (end[1] - start[1]) * ratio,
        )
    return points[-1]


def smooth_label_path(
    path: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    passes: int = 2,
) -> tuple[tuple[float, float], ...]:
    """Return a direction-normalised, gently rounded label centreline.

    Corner cutting removes visible hinges between territory path segments.
    Translating the result back onto the original arc midpoint keeps the full
    name centred on the region anchor instead of letting smoothing introduce
    drift.
    """
    clean_points: list[tuple[float, float]] = []
    for raw_x, raw_y in path:
        point = (float(raw_x), float(raw_y))
        if not all(math.isfinite(value) for value in point):
            continue
        if clean_points and math.dist(clean_points[-1], point) <= 1e-9:
            continue
        clean_points.append(point)
    if len(clean_points) < 2:
        return tuple(clean_points)

    delta_x = clean_points[-1][0] - clean_points[0][0]
    delta_y = clean_points[-1][1] - clean_points[0][1]
    if delta_x < -1e-9 or (abs(delta_x) <= 1e-9 and delta_y < 0.0):
        clean_points.reverse()

    original_distances = [0.0]
    for start, end in zip(clean_points, clean_points[1:]):
        original_distances.append(original_distances[-1] + math.dist(start, end))
    original_midpoint = _label_path_point(
        tuple(clean_points),
        tuple(original_distances),
        original_distances[-1] / 2.0,
    )

    smoothed = clean_points
    for _pass in range(max(0, min(4, int(passes)))):
        if len(smoothed) < 3:
            break
        rounded = [smoothed[0]]
        for start, end in zip(smoothed, smoothed[1:]):
            rounded.extend(
                (
                    (
                        start[0] * 0.75 + end[0] * 0.25,
                        start[1] * 0.75 + end[1] * 0.25,
                    ),
                    (
                        start[0] * 0.25 + end[0] * 0.75,
                        start[1] * 0.25 + end[1] * 0.75,
                    ),
                )
            )
        rounded.append(smoothed[-1])
        smoothed = rounded

    smooth_distances = [0.0]
    for start, end in zip(smoothed, smoothed[1:]):
        smooth_distances.append(smooth_distances[-1] + math.dist(start, end))
    smooth_midpoint = _label_path_point(
        tuple(smoothed),
        tuple(smooth_distances),
        smooth_distances[-1] / 2.0,
    )
    shift_x = original_midpoint[0] - smooth_midpoint[0]
    shift_y = original_midpoint[1] - smooth_midpoint[1]
    return tuple((x + shift_x, y + shift_y) for x, y in smoothed)


def curved_label_glyph_layout(
    path: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    advances: list[float] | tuple[float, ...],
    tracking: float = 1.0,
    max_angle_degrees: float = 40.0,
    edge_margin: float = 0.05,
) -> tuple[tuple[float, float, float], ...]:
    """Place glyph centres on a readable curve centred on the path midpoint.

    The path is normalised into reading direction, while the full text advance
    is centred by arc length.  Returning an empty tuple tells the caller to
    retry with a smaller font rather than truncate a coalition name.
    """
    points = smooth_label_path(path)
    if len(points) < 2 or not advances:
        return ()

    cumulative = [0.0]
    for start, end in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(start, end))
    distances = tuple(cumulative)
    path_length = distances[-1]
    clean_advances = tuple(max(0.0, float(value)) for value in advances)
    if path_length <= 1e-9 or not all(math.isfinite(value) for value in clean_advances):
        return ()

    gap = max(0.0, float(tracking))
    text_length = sum(clean_advances) + gap * max(0, len(clean_advances) - 1)
    margin = max(0.0, min(0.45, float(edge_margin)))
    if text_length <= 0.0 or text_length > path_length * (1.0 - margin * 2.0):
        return ()

    cursor = (path_length - text_length) / 2.0
    angle_limit = max(0.0, min(89.0, float(max_angle_degrees)))
    positions: list[tuple[float, float]] = []
    raw_angles: list[float] = []
    for advance in clean_advances:
        station = cursor + advance / 2.0
        x, y = _label_path_point(points, distances, station)
        tangent_window = max(
            1.0,
            min(
                path_length * 0.14,
                max(2.0, advance * 1.2, text_length * 0.08),
            ),
        )
        before = _label_path_point(points, distances, station - tangent_window)
        after = _label_path_point(points, distances, station + tangent_window)
        tangent_x = after[0] - before[0]
        tangent_y = after[1] - before[1]
        positions.append((x, y))
        raw_angles.append(math.degrees(math.atan2(tangent_y, tangent_x)))
        cursor += advance + gap

    # Blend neighbouring tangents as unit vectors so the baseline rotates as
    # one continuous ribbon rather than hinging at each letter or word.
    smooth_angles: list[float] = []
    for index in range(len(raw_angles)):
        vector_x = 0.0
        vector_y = 0.0
        for neighbour in range(max(0, index - 2), min(len(raw_angles), index + 3)):
            weight = 3.0 - abs(index - neighbour)
            radians = math.radians(raw_angles[neighbour])
            vector_x += math.cos(radians) * weight
            vector_y += math.sin(radians) * weight
        angle = math.degrees(math.atan2(vector_y, vector_x))
        smooth_angles.append(max(-angle_limit, min(angle_limit, angle)))
    return tuple(
        (x, y, angle)
        for (x, y), angle in zip(positions, smooth_angles)
    )


def kerning_label_advances(label: str, font: ImageFont.ImageFont) -> tuple[float, ...]:
    """Return character advances whose sum is the font's full kerned width."""
    text = str(label)
    previous = 0.0
    advances: list[float] = []
    for index in range(1, len(text) + 1):
        current = float(font.getlength(text[:index]))
        advance = current - previous
        if not math.isfinite(advance) or advance <= 0.0:
            advance = max(0.5, float(font.getlength(text[index - 1])))
        advances.append(advance)
        previous = current
    return tuple(advances)


def centered_curved_label_path(
    path: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    minimum_length: float,
    max_chord_degrees: float = 22.0,
) -> tuple[tuple[float, float], ...]:
    """Extend a short centreline equally around its midpoint for full labels.

    Small in-game territories can be narrower than their coalition name.  The
    old flat labels already extended beyond those blobs.  Stretching only the
    path's long-axis component retains its characteristic bend without moving
    the name away from the territory centre.
    """
    points: list[tuple[float, float]] = []
    for raw_x, raw_y in path:
        point = (float(raw_x), float(raw_y))
        if not all(math.isfinite(value) for value in point):
            continue
        if points and math.dist(points[-1], point) <= 1e-9:
            continue
        points.append(point)
    if len(points) < 2:
        return tuple(points)

    delta_x = points[-1][0] - points[0][0]
    delta_y = points[-1][1] - points[0][1]
    if delta_x < -1e-9 or (abs(delta_x) <= 1e-9 and delta_y < 0.0):
        points.reverse()

    cumulative = [0.0]
    for start, end in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(start, end))
    current_length = cumulative[-1]
    target_length = max(0.0, float(minimum_length))
    if current_length <= 1e-9:
        return tuple(points)

    anchor = _label_path_point(tuple(points), tuple(cumulative), current_length / 2.0)
    midpoint_distance = current_length / 2.0
    left_arm: list[tuple[float, float]] = []
    right_arm: list[tuple[float, float]] = []
    for index in range(1, len(points)):
        if midpoint_distance > cumulative[index] + 1e-9:
            continue
        if abs(midpoint_distance - cumulative[index]) <= 1e-9:
            left_arm = points[:index + 1]
            right_arm = points[index:]
        else:
            left_arm = [*points[:index], anchor]
            right_arm = [anchor, *points[index:]]
        break
    if not left_arm or not right_arm:
        return tuple(points)
    points = [*left_arm[:-1], *right_arm]
    anchor_index = len(left_arm) - 1

    axis_x = points[-1][0] - points[0][0]
    axis_y = points[-1][1] - points[0][1]
    axis_length = math.hypot(axis_x, axis_y)
    if axis_length <= 1e-9:
        return tuple(points)
    chord_angle = math.degrees(math.atan2(axis_y, axis_x))
    chord_limit = max(0.0, min(45.0, float(max_chord_degrees)))
    readable_angle = max(-chord_limit, min(chord_limit, chord_angle))
    correction = math.radians(readable_angle - chord_angle)
    if abs(correction) > 1e-9:
        cosine = math.cos(correction)
        sine = math.sin(correction)
        points = [
            (
                anchor[0] + (x - anchor[0]) * cosine - (y - anchor[1]) * sine,
                anchor[1] + (x - anchor[0]) * sine + (y - anchor[1]) * cosine,
            )
            for x, y in points
        ]
        axis_x = points[-1][0] - points[0][0]
        axis_y = points[-1][1] - points[0][1]
        axis_length = math.hypot(axis_x, axis_y)
    axis_x /= axis_length
    axis_y /= axis_length
    normal_x, normal_y = -axis_y, axis_x

    def stretched(factor: float) -> tuple[tuple[float, float], ...]:
        # Preserve most of the original bend; only let it grow slightly on a
        # very large extension so narrow regions do not produce a flat ruler.
        bend_factor = min(1.35, 1.0 + max(0.0, factor - 1.0) * 0.08)
        result: list[tuple[float, float]] = []
        for x, y in points:
            offset_x, offset_y = x - anchor[0], y - anchor[1]
            along = offset_x * axis_x + offset_y * axis_y
            across = offset_x * normal_x + offset_y * normal_y
            result.append(
                (
                    anchor[0] + along * factor * axis_x + across * bend_factor * normal_x,
                    anchor[1] + along * factor * axis_y + across * bend_factor * normal_y,
                )
            )
        return tuple(result)

    def arc_length(points_to_measure: tuple[tuple[float, float], ...]) -> float:
        return sum(
            math.dist(start, end)
            for start, end in zip(points_to_measure, points_to_measure[1:])
        )

    def arm_lengths(
        points_to_measure: tuple[tuple[float, float], ...],
    ) -> tuple[float, float]:
        return (
            arc_length(points_to_measure[:anchor_index + 1]),
            arc_length(points_to_measure[anchor_index:]),
        )

    def trim_from_anchor(
        outward_arm: tuple[tuple[float, float], ...],
        keep_length: float,
    ) -> tuple[tuple[float, float], ...]:
        distances = [0.0]
        for start, end in zip(outward_arm, outward_arm[1:]):
            distances.append(distances[-1] + math.dist(start, end))
        if distances[-1] <= keep_length + 1e-7:
            return outward_arm
        endpoint = _label_path_point(
            outward_arm,
            tuple(distances),
            keep_length,
        )
        retained = [outward_arm[0]]
        for point, distance in zip(outward_arm[1:], distances[1:]):
            if distance >= keep_length - 1e-7:
                break
            retained.append(point)
        if math.dist(retained[-1], endpoint) > 1e-7:
            retained.append(endpoint)
        return tuple(retained)

    def balanced(
        points_to_balance: tuple[tuple[float, float], ...],
    ) -> tuple[tuple[float, float], ...]:
        left_length, right_length = arm_lengths(points_to_balance)
        keep_length = min(left_length, right_length)
        left_outward = tuple(reversed(points_to_balance[:anchor_index + 1]))
        right_outward = points_to_balance[anchor_index:]
        left_retained = trim_from_anchor(left_outward, keep_length)
        right_retained = trim_from_anchor(right_outward, keep_length)
        return (*reversed(left_retained), *right_retained[1:])

    current_points = tuple(points)
    target_arm_length = target_length / 2.0
    if min(arm_lengths(current_points)) >= target_arm_length:
        return balanced(current_points)
    low = 1.0
    high = max(2.0, target_length / current_length * 1.5)
    while min(arm_lengths(stretched(high))) < target_arm_length:
        high *= 2.0
    for _iteration in range(28):
        middle = (low + high) / 2.0
        if min(arm_lengths(stretched(middle))) < target_arm_length:
            low = middle
        else:
            high = middle
    return balanced(stretched(high))


@lru_cache(maxsize=64)
def _territory_label_font(size: int) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/bahnschrift.ttf"),
        Path("C:/Windows/Fonts/arialnb.ttf"),
        Path("C:/Windows/Fonts/seguisb.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    )
    for path in candidates:
        try:
            font = ImageFont.truetype(str(path), size=max(1, int(size)))
            if path.name.casefold() == "bahnschrift.ttf":
                try:
                    font.set_variation_by_name(b"Bold Condensed")
                except (AttributeError, OSError, ValueError):
                    pass
            return font
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=512)
def _territory_label_raster(
    label: str,
    font_pixels: int,
    angle_tenths: int,
) -> Image.Image:
    """Render one reusable whole-label raster for local mask fitting."""
    font = _territory_label_font(max(1, int(font_pixels)))
    stroke_width = max(1, min(2, int(round(font_pixels * 0.1))))
    text_bbox = font.getbbox(label, stroke_width=stroke_width)
    padding = stroke_width + 3
    text_width = max(1, text_bbox[2] - text_bbox[0])
    text_height = max(1, text_bbox[3] - text_bbox[1])
    raster = Image.new(
        "RGBA",
        (text_width + padding * 2, text_height + padding * 2),
        (0, 0, 0, 0),
    )
    ImageDraw.Draw(raster, "RGBA").text(
        (padding - text_bbox[0], padding - text_bbox[1]),
        label,
        font=font,
        fill=MAP_TERRITORY_LABEL,
        stroke_width=stroke_width,
        stroke_fill=MAP_TERRITORY_LABEL_SHADOW,
    )
    angle_degrees = angle_tenths / 10.0
    if abs(angle_degrees) > 0.05:
        raster = raster.rotate(
            -angle_degrees,
            resample=Image.Resampling.BICUBIC,
            expand=True,
        )
    return raster


def paint_curved_map_label(
    image: Image.Image,
    text: str,
    path: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    requested_font_pixels: int,
    containment_mask: Image.Image,
    minimum_font_pixels: int = 5,
) -> bool:
    """Paint one smoothly bending, kerned name wholly inside its territory."""
    label = str(text).strip()
    if (
        not label
        or len(path) < 2
        or containment_mask.mode != "L"
        or containment_mask.size != image.size
    ):
        return False

    largest = max(1, int(requested_font_pixels))
    smallest = max(1, min(largest, int(minimum_font_pixels)))
    for font_pixels in range(largest, smallest - 1, -1):
        font = _territory_label_font(font_pixels)
        advances = kerning_label_advances(label, font)
        layout = curved_label_glyph_layout(
            path,
            advances,
            # The full font kerning is retained and the shared layout smooths
            # adjacent tangents, so letters form one continuous curved name.
            tracking=0.0,
            max_angle_degrees=24.0,
            edge_margin=0.08,
        )
        if not layout or len(layout) != len(label):
            continue

        candidate = Image.new("RGBA", image.size, (0, 0, 0, 0))
        stroke_width = max(1, min(2, int(round(font_pixels * 0.1))))
        line_bbox = font.getbbox(label, stroke_width=stroke_width)
        line_top, line_bottom = line_bbox[1], line_bbox[3]
        padding = stroke_width + 3
        complete = True
        for character, advance, (x, y, angle) in zip(label, advances, layout):
            if character.isspace():
                continue
            ink_bbox = font.getbbox(character)
            ink_offset = (ink_bbox[0] + ink_bbox[2]) / 2.0 - advance / 2.0
            radians = math.radians(angle)
            x += math.cos(radians) * ink_offset
            y += math.sin(radians) * ink_offset
            glyph_bbox = font.getbbox(character, stroke_width=stroke_width)
            glyph_width = max(1, glyph_bbox[2] - glyph_bbox[0])
            line_height = max(1, line_bottom - line_top)
            glyph = Image.new(
                "RGBA",
                (glyph_width + padding * 2, line_height + padding * 2),
                (0, 0, 0, 0),
            )
            glyph_draw = ImageDraw.Draw(glyph, "RGBA")
            glyph_draw.text(
                (padding - glyph_bbox[0], padding - line_top),
                character,
                font=font,
                fill=MAP_TERRITORY_LABEL,
                stroke_width=stroke_width,
                stroke_fill=MAP_TERRITORY_LABEL_SHADOW,
            )
            rotated = glyph.rotate(
                -angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
            )
            destination = (
                int(round(x - rotated.width / 2.0)),
                int(round(y - rotated.height / 2.0)),
            )
            if (
                destination[0] < 0
                or destination[1] < 0
                or destination[0] + rotated.width > image.width
                or destination[1] + rotated.height > image.height
            ):
                complete = False
                break
            candidate.paste(rotated, destination, rotated)
        if not complete:
            continue

        outside_alpha = ImageChops.subtract(
            candidate.getchannel("A"),
            containment_mask,
        )
        if outside_alpha.getbbox() is not None:
            continue
        image.alpha_composite(candidate)
        return True
    return False


def paint_straight_map_label(
    image: Image.Image,
    text: str,
    anchor: tuple[float, float],
    requested_font_pixels: int,
    containment_mask: Image.Image,
    minimum_font_pixels: int = 5,
    preferred_angle_degrees: float = 0.0,
    minimum_readable_font_pixels: int = 11,
) -> bool:
    """Paint one contained name, angling the whole line only when necessary."""
    label = str(text).strip()
    anchor_x, anchor_y = float(anchor[0]), float(anchor[1])
    if (
        not label
        or not math.isfinite(anchor_x)
        or not math.isfinite(anchor_y)
        or containment_mask.mode != "L"
        or containment_mask.size != image.size
    ):
        return False

    largest = max(1, int(requested_font_pixels))
    smallest = max(1, min(largest, int(minimum_font_pixels)))
    readable_floor = max(
        smallest,
        min(largest, int(minimum_readable_font_pixels)),
    )

    def contained_candidate(
        font_pixels: int,
        angle_degrees: float,
    ) -> tuple[Image.Image, tuple[int, int]] | None:
        raster = _territory_label_raster(
            label,
            font_pixels,
            int(round(float(angle_degrees) * 10.0)),
        )
        destination = (
            int(round(anchor_x - raster.width / 2.0)),
            int(round(anchor_y - raster.height / 2.0)),
        )
        if (
            destination[0] < 0
            or destination[1] < 0
            or destination[0] + raster.width > image.width
            or destination[1] + raster.height > image.height
        ):
            return None

        destination_box = (
            destination[0],
            destination[1],
            destination[0] + raster.width,
            destination[1] + raster.height,
        )
        outside_alpha = ImageChops.subtract(
            raster.getchannel("A"),
            containment_mask.crop(destination_box),
        )
        if outside_alpha.getbbox() is not None:
            return None
        return raster, destination

    def largest_fit(
        angle_degrees: float,
    ) -> tuple[int, tuple[Image.Image, tuple[int, int]]] | None:
        for font_pixels in range(largest, smallest - 1, -1):
            candidate = contained_candidate(font_pixels, angle_degrees)
            if candidate is not None:
                return font_pixels, candidate
        return None

    def composite_fit(
        fit: tuple[int, tuple[Image.Image, tuple[int, int]]],
    ) -> None:
        raster, destination = fit[1]
        image.alpha_composite(raster, dest=destination)

    horizontal = largest_fit(0.0)
    if horizontal is not None and horizontal[0] >= readable_floor:
        composite_fit(horizontal)
        return True

    preferred_angle = float(preferred_angle_degrees)
    angled = None
    if math.isfinite(preferred_angle) and abs(preferred_angle) > 1.0:
        angled = largest_fit(preferred_angle)
    if angled is not None and (horizontal is None or angled[0] > horizontal[0]):
        composite_fit(angled)
        return True
    if horizontal is not None:
        composite_fit(horizontal)
        return True
    return False


def territory_label_fallback_angle(
    path: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    max_angle_degrees: float = 55.0,
) -> float:
    """Return a readable whole-label angle from the centre of a territory path."""
    clean_points: list[tuple[float, float]] = []
    for raw_x, raw_y in path:
        point = (float(raw_x), float(raw_y))
        if not all(math.isfinite(value) for value in point):
            continue
        if clean_points and math.dist(clean_points[-1], point) <= 1e-9:
            continue
        clean_points.append(point)
    if len(clean_points) < 2:
        return 0.0

    distances = [0.0]
    for start, end in zip(clean_points, clean_points[1:]):
        distances.append(distances[-1] + math.dist(start, end))
    total_length = distances[-1]
    if total_length <= 1e-9:
        return 0.0

    points = tuple(clean_points)
    arc_distances = tuple(distances)
    start = _label_path_point(points, arc_distances, total_length * 0.4)
    end = _label_path_point(points, arc_distances, total_length * 0.6)
    if math.dist(start, end) <= 1e-9:
        return 0.0
    angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
    while angle > 90.0:
        angle -= 180.0
    while angle < -90.0:
        angle += 180.0
    limit = max(0.0, min(89.0, abs(float(max_angle_degrees))))
    return max(-limit, min(limit, angle))


def _map_system_name(system: dict[str, Any]) -> str:
    return str(system.get("name") or "").strip().casefold()


def connected_map_systems(galaxy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return positioned systems with at least one recorded jump connection.

    This filters the rendered map only. It deliberately leaves unlinked map
    observations in the archive and searchable tables.
    """
    positioned = [
        system for system in galaxy.get("systems", [])
        if isinstance(system, dict) and system.get("hasPosition")
        and isinstance(system.get("x"), (int, float))
        and isinstance(system.get("y"), (int, float))
        and _map_system_name(system)
    ]
    available = {_map_system_name(system) for system in positioned}
    connected: set[str] = set()
    usable_edges: list[dict[str, Any]] = []
    for edge in galaxy.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "").strip().casefold()
        target = str(edge.get("target") or "").strip().casefold()
        if not source or not target or source == target or source not in available or target not in available:
            continue
        connected.update((source, target))
        usable_edges.append(edge)
    return [system for system in positioned if _map_system_name(system) in connected], usable_edges


def map_fit_bounds(
    positioned: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    """Return fit bounds from visible connected nodes only."""
    return (
        min(float(system["x"]) for system in positioned),
        min(float(system["y"]) for system in positioned),
        max(float(system["x"]) for system in positioned),
        max(float(system["y"]) for system in positioned),
    )


def territory_map_geometry(
    galaxy: dict[str, Any],
) -> tuple[tuple[TerritoryCell, ...], tuple[TerritoryLabelRegion, ...]]:
    """Build authoritative territory independently of the visible node filter."""
    positions = galaxy.get("territoryPositions")
    territory = galaxy.get("territory")
    if not isinstance(positions, dict) or not isinstance(territory, dict):
        return (), ()
    cells = build_territory_cells(positions, territory)
    return cells, build_territory_label_regions(cells)


def _rgb_hex(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def game_link_backup_paths(result: game_link.PatchResult) -> list[str]:
    """Return the backups created by a passive Game Link repair."""
    return [
        str(path)
        for path in (
            result.backup,
            result.source_backup,
        )
        if path is not None
    ]


def format_number(value: Any, empty: str = "-") -> str:
    if value is None or value == "":
        return empty
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def compact_number(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return "-"
    for divisor, suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,.0f}"


def positive_prices(item: dict[str, Any], key: str) -> list[float]:
    values = []
    for market in item.get("markets", []):
        value = market.get(key)
        if isinstance(value, (int, float)) and value > 0:
            values.append(float(value))
    return values


PERSONAL_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "favorite": {"label": "FAV", "width": 54, "minwidth": 48, "anchor": "center", "kind": "number", "first_desc": True},
    "watchlist": {"label": "WATCH", "width": 62, "minwidth": 54, "anchor": "center", "kind": "number", "first_desc": True},
    "personal_category": {"label": "MY CATEGORY", "width": 126, "minwidth": 90, "anchor": "w", "kind": "text"},
    "tags": {"label": "MY TAGS", "width": 170, "minwidth": 105, "anchor": "w", "kind": "text"},
    "note": {"label": "MY NOTE", "width": 220, "minwidth": 130, "anchor": "w", "kind": "text"},
}


def personal_column_raw_value(annotation: dict[str, Any] | None, column: str) -> Any:
    annotation = annotation if isinstance(annotation, dict) else {}
    if column == "favorite":
        return 1 if annotation.get("favorite") else 0
    if column == "watchlist":
        return 1 if annotation.get("watchlist") else 0
    if column == "personal_category":
        return annotation.get("category")
    if column == "tags":
        return ", ".join(str(value) for value in annotation.get("tags", []))
    if column == "note":
        return annotation.get("note")
    return None


def personal_column_display_value(annotation: dict[str, Any] | None, column: str) -> str:
    value = personal_column_raw_value(annotation, column)
    if column == "favorite":
        return "★" if value else "—"
    if column == "watchlist":
        return "YES" if value else "—"
    if column == "note" and value:
        text = " ".join(str(value).split())
        return text[:72] + ("…" if len(text) > 72 else "")
    return format_number(value)


def personal_search_text(annotation: dict[str, Any] | None) -> str:
    annotation = annotation if isinstance(annotation, dict) else {}
    return " ".join(
        (
            "favorite" if annotation.get("favorite") else "",
            "watchlist watched" if annotation.get("watchlist") else "",
            str(annotation.get("category") or ""),
            " ".join(str(value) for value in annotation.get("tags", [])),
            str(annotation.get("note") or ""),
        )
    )


ITEM_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "category": {"label": "CATEGORY", "width": 112, "minwidth": 85, "anchor": "w", "kind": "text"},
    "tech": {"label": "TECH", "width": 52, "minwidth": 45, "anchor": "center", "kind": "number", "first_desc": True},
    "size": {"label": "SIZE", "width": 52, "minwidth": 45, "anchor": "center", "kind": "number"},
    "buy": {"label": "BEST BUY", "width": 92, "minwidth": 75, "anchor": "e", "kind": "number"},
    "sell": {"label": "BEST SELL", "width": 92, "minwidth": 75, "anchor": "e", "kind": "number", "first_desc": True},
    "art": {"label": "IMAGE", "width": 72, "minwidth": 65, "anchor": "center", "kind": "text"},
    "rarity": {"label": "RARITY", "width": 82, "minwidth": 70, "anchor": "w", "kind": "text"},
    "classification": {"label": "CLASS", "width": 115, "minwidth": 85, "anchor": "w", "kind": "text", "stats": ("Classification", "Class")},
    "mass": {"label": "MASS", "width": 88, "minwidth": 68, "anchor": "e", "kind": "number", "stats": ("Mass",), "first_desc": True},
    "damage": {"label": "DAMAGE", "width": 82, "minwidth": 64, "anchor": "e", "kind": "number", "stats": ("Damage",), "first_desc": True},
    "damage_type": {"label": "DAMAGE TYPE", "width": 98, "minwidth": 78, "anchor": "w", "kind": "text", "stats": ("Damage Type",)},
    "fire_rate": {"label": "FIRE RATE", "width": 122, "minwidth": 88, "anchor": "e", "kind": "number", "stats": ("Fire Rate",), "first_desc": True},
    "range": {"label": "RANGE", "width": 82, "minwidth": 65, "anchor": "e", "kind": "number", "stats": ("Range", "Max Range"), "first_desc": True},
    "tracking": {"label": "TRACKING", "width": 92, "minwidth": 72, "anchor": "e", "kind": "number", "stats": ("Tracking", "Beam Tracking"), "first_desc": True},
    "projectile_speed": {"label": "PROJ SPEED", "width": 96, "minwidth": 76, "anchor": "e", "kind": "number", "stats": ("Proj Speed", "Projectile Speed"), "first_desc": True},
    "energy_cost": {"label": "ENERGY COST", "width": 104, "minwidth": 82, "anchor": "e", "kind": "number", "stats": ("Energy Cost", "Energy/sec"), "first_desc": False},
    "shield_bank": {"label": "SHIELD BANK", "width": 102, "minwidth": 82, "anchor": "e", "kind": "number", "stats": ("Shield Bank",), "first_desc": True},
    "shield_regen": {"label": "SHIELD REGEN", "width": 116, "minwidth": 88, "anchor": "e", "kind": "number", "stats": ("Recharge Rate", "Shield Regen"), "first_desc": True},
    "regen_cost": {"label": "REGEN COST", "width": 152, "minwidth": 100, "anchor": "e", "kind": "number", "stats": ("Regen Energy Cost", "Energy Cost"), "first_desc": False},
    "energy_bank": {"label": "ENERGY BANK", "width": 105, "minwidth": 82, "anchor": "e", "kind": "number", "stats": ("Capacity", "Energy Bank"), "first_desc": True},
    "energy_output": {"label": "ENERGY OUTPUT", "width": 115, "minwidth": 88, "anchor": "e", "kind": "number", "stats": ("Output", "Energy Output", "Energy Regen"), "first_desc": True},
    "thrust": {"label": "THRUST", "width": 94, "minwidth": 72, "anchor": "e", "kind": "number", "stats": ("Thrust",), "first_desc": True},
    "turning": {"label": "TURNING", "width": 94, "minwidth": 72, "anchor": "e", "kind": "number", "stats": ("Turning",), "first_desc": True},
    "speed": {"label": "SPEED", "width": 76, "minwidth": 62, "anchor": "e", "kind": "number", "stats": ("Speed", "Max Speed", "Ship Speed"), "first_desc": True},
    "cargo_capacity": {"label": "CARGO CAP", "width": 96, "minwidth": 76, "anchor": "e", "kind": "number", "stats": ("Cargo Cap", "Cargo Capacity", "Hull Capacity"), "first_desc": True},
    "shield_bonus": {"label": "SHIELD BONUS", "width": 108, "minwidth": 86, "anchor": "e", "kind": "number", "stats": ("Shield Bonus",), "first_desc": True},
    "resistance": {"label": "RESIST", "width": 76, "minwidth": 62, "anchor": "e", "kind": "number", "stats": ("Kinetic Resist", "Resistance"), "first_desc": True},
}
ITEM_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})

ITEM_DEFAULT_COLUMNS = ("category", "tech", "size", "buy", "sell", "art")
ITEM_COLUMN_PRESETS = {
    "Default": ITEM_DEFAULT_COLUMNS,
    "Combat": ("category", "tech", "damage", "damage_type", "fire_rate", "range", "tracking", "projectile_speed", "energy_cost", "mass", "buy"),
    "Ships": ("category", "tech", "classification", "speed", "cargo_capacity", "shield_bonus", "resistance", "mass", "buy"),
    "Power & defence": ("category", "tech", "shield_bank", "shield_regen", "regen_cost", "energy_bank", "energy_output", "mass", "size", "buy"),
    "Engines": ("category", "tech", "thrust", "turning", "mass", "size", "buy"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "category", "tech", "buy"),
}


SCAN_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "system": {"label": "SYSTEM", "width": 165, "minwidth": 120, "anchor": "w", "kind": "text"},
    "type": {"label": "TYPE", "width": 92, "minwidth": 70, "anchor": "w", "kind": "text"},
    "colony": {"label": "COLONY RATING", "width": 128, "minwidth": 100, "anchor": "w", "kind": "text"},
    "score": {"label": "SCORE", "width": 62, "minwidth": 52, "anchor": "e", "kind": "number", "first_desc": True},
    "atmosphere": {"label": "ATMOSPHERE", "width": 126, "minwidth": 95, "anchor": "w", "kind": "text"},
    "temperature": {"label": "TEMPERATURE", "width": 116, "minwidth": 90, "anchor": "w", "kind": "text"},
    "gravity": {"label": "GRAVITY", "width": 112, "minwidth": 85, "anchor": "w", "kind": "text"},
    "geology": {"label": "GEOLOGY", "width": 112, "minwidth": 85, "anchor": "w", "kind": "text"},
    "ecology": {"label": "ECOLOGY", "width": 132, "minwidth": 96, "anchor": "w", "kind": "text"},
    "resources": {"label": "BEST RESOURCES", "width": 220, "minwidth": 150, "anchor": "w", "kind": "text"},
    "resource_count": {"label": "YIELDS", "width": 62, "minwidth": 52, "anchor": "e", "kind": "number", "first_desc": True},
    "top_yield": {"label": "TOP YIELD", "width": 82, "minwidth": 65, "anchor": "e", "kind": "number", "first_desc": True},
    "extractor": {"label": "BEST EXTRACTOR", "width": 152, "minwidth": 110, "anchor": "w", "kind": "text"},
    "base": {"label": "BASE", "width": 58, "minwidth": 50, "anchor": "center", "kind": "number", "first_desc": True},
    "count": {"label": "BASES", "width": 60, "minwidth": 52, "anchor": "e", "kind": "number", "first_desc": True},
    "range": {"label": "SCAN RANGE", "width": 92, "minwidth": 72, "anchor": "e", "kind": "number", "first_desc": True},
    "observed": {"label": "LAST SCAN", "width": 142, "minwidth": 120, "anchor": "w", "kind": "text", "first_desc": True},
    "id": {"label": "PLANET ID", "width": 86, "minwidth": 68, "anchor": "e", "kind": "number"},
}
SCAN_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})

# One sortable column per resource, so the planet list can be ordered by the
# yield of a specific commodity rather than only by the best overall.  These
# reuse the existing column picker, sorting, and layout persistence.
SCAN_RESOURCE_KEYS = (
    "metal_ore", "silicon", "copper", "gold", "oil",
    "promethium", "space_corn", "titanium", "wood",
)
SCAN_RESOURCE_COLUMN_PREFIX = "res_"
SCAN_COLUMN_SPECS.update({
    f"{SCAN_RESOURCE_COLUMN_PREFIX}{name}": {
        "label": name.replace("_", " ").upper(),
        "width": 86, "minwidth": 66, "anchor": "e",
        "kind": "number", "first_desc": True,
    }
    for name in SCAN_RESOURCE_KEYS
})

SCAN_DEFAULT_COLUMNS = ("system", "type", "colony", "resources", "base", "count", "observed")
SCAN_COLUMN_PRESETS = {
    "Overview": SCAN_DEFAULT_COLUMNS,
    "Colony": ("system", "type", "colony", "score", "atmosphere", "temperature", "gravity", "geology", "ecology", "base", "count"),
    "Resources": ("system", "type", "resources", "resource_count", "top_yield", "extractor", "base", "count"),
    "Yield by resource": ("system", "type") + tuple(
        f"{SCAN_RESOURCE_COLUMN_PREFIX}{name}" for name in SCAN_RESOURCE_KEYS),
    "Survey": ("system", "type", "range", "observed", "id"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "system", "type", "colony", "base", "count"),
}


def scan_quality(scan: dict[str, Any]) -> tuple[str, float | None]:
    try:
        score = float(scan.get("colonization_index"))
    except (TypeError, ValueError):
        return "Unknown", None
    if score >= 90:
        return "Exceptional", score
    if score >= 75:
        return "Strong", score
    if score >= 50:
        return "Viable", score
    if score >= 25:
        return "Difficult", score
    return "Hostile", score


def scan_environment_value(scan: dict[str, Any], category: str) -> str:
    for row in scan.get("colonization", []) if isinstance(scan.get("colonization"), list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("category_label") or "").casefold() == category.casefold():
            setting = str(row.get("setting_label") or "Unknown")
            penalty = row.get("penalty_pct")
            return f"{setting} ({format_number(penalty, '0')}%)" if penalty not in (None, "") else setting
    return "Unknown"


def scan_positive_resources(scan: dict[str, Any], key: str = "resources") -> list[tuple[float, str]]:
    values = scan.get(key) if isinstance(scan.get(key), dict) else {}
    ranked: list[tuple[float, str]] = []
    for name, value in values.items():
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            ranked.append((amount, str(name).replace("_", " ").title()))
    ranked.sort(key=lambda row: (-row[0], row[1].casefold()))
    return ranked


def scan_yield_amounts(scan: dict[str, Any]) -> dict[str, float]:
    """Extractor yield for a body, falling back to old raw-resource scans."""
    values = scan.get("extractors")
    if not isinstance(values, dict) or not values:
        values = scan.get("resources") if isinstance(scan.get("resources"), dict) else {}
    amounts: dict[str, float] = {}
    for resource in SCAN_RESOURCE_KEYS:
        try:
            amount = float(values.get(resource, 0) or 0)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            amounts[resource] = amount
    return amounts


MAX_EXTRACTION_BASES_PER_PLANET = 3
MAX_EXTRACTION_BASES_PER_MOON = 1


def scan_is_moon(scan: dict[str, Any]) -> bool:
    """Whether this scan is one of the game-marked moon bodies."""
    return str(scan.get("planet_type") or "").strip().casefold() == "moon"


def scan_extraction_base_limit(scan: dict[str, Any]) -> int:
    """The number of extraction bases the recorded body can host."""
    return MAX_EXTRACTION_BASES_PER_MOON if scan_is_moon(scan) else MAX_EXTRACTION_BASES_PER_PLANET


def system_extraction_base_summary(scans: list[dict[str, Any]] | None) -> dict[str, int]:
    """Count scanned planet/moon bodies and their valid maximum base count."""
    summary = {"bodies": 0, "planetBodies": 0, "moonBodies": 0, "maxBases": 0}
    for scan in scans or []:
        if not isinstance(scan, dict):
            continue
        summary["bodies"] += 1
        if scan_is_moon(scan):
            summary["moonBodies"] += 1
        else:
            summary["planetBodies"] += 1
        summary["maxBases"] += scan_extraction_base_limit(scan)
    return summary


def system_extractor_slot_capacities(
    scans: list[dict[str, Any]] | None,
) -> dict[str, float]:
    """Maximum extractor slots for the selected system's scanned bodies.

    A recorded resource yield represents one base. Planets can host three
    bases while game-marked moons host one. Only extraction capacity is scaled;
    general per-system yield totals remain unscaled.
    """
    capacities: dict[str, float] = {}
    for scan in scans or []:
        if not isinstance(scan, dict):
            continue
        multiplier = scan_extraction_base_limit(scan)
        for resource, amount in scan_yield_amounts(scan).items():
            capacities[resource] = capacities.get(resource, 0.0) + amount * multiplier
    return {
        resource: amount
        for resource, amount in capacities.items()
        if amount > 0
    }


def resource_yield_entries(amounts: dict[str, Any]) -> list[tuple[str, float]]:
    """Resource label/value pairs, largest yield first for compact display."""
    entries = []
    for resource in SCAN_RESOURCE_KEYS:
        try:
            amount = float(amounts.get(resource, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if amount > 0:
            entries.append((resource.replace("_", " ").title(), amount))
    return sorted(entries, key=lambda entry: (-entry[1], entry[0].casefold()))


def extractor_slot_entries(
    possible_slots: dict[str, Any],
    observed_slots: dict[str, Any] | None,
) -> list[tuple[str, int | None, float]]:
    """Display rows for locally observed extractor use versus scanned capacity."""
    entries: list[tuple[str, int | None, float]] = []
    for resource in SCAN_RESOURCE_KEYS:
        try:
            possible = float(possible_slots.get(resource, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if possible <= 0:
            continue
        used: int | None = None
        if observed_slots is not None:
            try:
                used = max(0, int(observed_slots.get(resource, 0) or 0))
            except (AttributeError, TypeError, ValueError):
                used = 0
        entries.append((resource.replace("_", " ").title(), used, possible))
    return sorted(entries, key=lambda entry: (-entry[2], entry[0].casefold()))


def extractor_tier_summary(tiers: dict[Any, Any] | None) -> str:
    """Format one private extractor tier mix without guessing legacy data."""
    entries: list[tuple[int, int]] = []
    for raw_tier, raw_quantity in (tiers or {}).items():
        try:
            tier = int(raw_tier)
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if tier > 0 and quantity > 0:
            entries.append((tier, quantity))
    entries.sort()
    return "  ·  ".join(
        f"{format_number(quantity)}× T{tier}"
        for tier, quantity in entries
    )


def system_extraction_capacity_entries(
    max_slots: dict[str, Any],
    observed_slots: dict[str, Any] | None,
    observed_tiers: dict[str, dict[int, int]] | None,
) -> list[dict[str, Any]]:
    """Detailed rows without claiming slots from unobserved bases are free."""
    entries: list[dict[str, Any]] = []
    for resource in SCAN_RESOURCE_KEYS:
        try:
            maximum = float(max_slots.get(resource, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if maximum <= 0:
            continue
        used: int | None = None
        if observed_slots is not None:
            try:
                used = max(0, int(observed_slots.get(resource, 0) or 0))
            except (AttributeError, TypeError, ValueError):
                used = 0
        remaining = maximum - used if used is not None else None
        entries.append({
            "resource": resource.replace("_", " ").title(),
            "maximum": maximum,
            "used": used,
            "remaining": remaining,
            "usedPercent": (used / maximum * 100.0) if used is not None else None,
            "tierSummary": extractor_tier_summary((observed_tiers or {}).get(resource)),
        })
    return sorted(entries, key=lambda entry: (-float(entry["maximum"]), str(entry["resource"]).casefold()))


COVERAGE_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "hops": {"label": "HOPS", "width": 70, "minwidth": 55, "anchor": "e",
             "numeric": True},
    "hazard": {"label": "DANGER", "width": 80, "minwidth": 65, "anchor": "e",
               "numeric": True},
    "unseenShops": {"label": "SHOPS", "width": 80, "minwidth": 65, "anchor": "e",
                    "numeric": True, "first_desc": True},
    "npcStations": {"label": "STATIONS", "width": 95, "minwidth": 75,
                    "anchor": "e", "numeric": True, "first_desc": True},
    "status": {"label": "STATUS", "width": 170, "minwidth": 120, "anchor": "w"},
}
COVERAGE_COLUMNS = tuple(COVERAGE_COLUMN_SPECS)

SYSTEM_YIELD_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "planets": {"label": "BODIES", "width": 68, "minwidth": 56, "anchor": "e",
                "kind": "number", "first_desc": True},
    "total": {"label": "TOTAL (MAX)", "width": 106, "minwidth": 84, "anchor": "e",
              "kind": "number", "first_desc": True},
}
# One column per resource, each sortable highest-first, so a system can be
# ranked by whichever commodity matters right now.
SYSTEM_YIELD_COLUMN_SPECS.update({
    f"{SCAN_RESOURCE_COLUMN_PREFIX}{name}": {
        "label": name.replace("_", " ").upper(),
        "width": 94, "minwidth": 72, "anchor": "e",
        "kind": "number", "first_desc": True,
    }
    for name in SCAN_RESOURCE_KEYS
})
SYSTEM_YIELD_COLUMNS = tuple(SYSTEM_YIELD_COLUMN_SPECS)


def system_yield_summary(row: dict[str, Any], limit: int = 0) -> str:
    """Readable "Silicon 743, Metal Ore 737, ..." for a whole system."""
    ranked = resource_yield_entries({
        name: row.get(f"{SCAN_RESOURCE_COLUMN_PREFIX}{name}")
        for name in SCAN_RESOURCE_KEYS
    })
    if limit:
        ranked = ranked[:limit]
    return ", ".join(f"{label} {format_number(amount)}"
                     for label, amount in ranked) or "No useful yield"


def system_yield_display_value(row: dict[str, Any], column: str) -> str:
    """Show one-base resource yield followed by its moon-aware system maximum."""
    if column == "planets":
        return format_number(row.get(column))
    try:
        amount = float(row.get(column) or 0)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    maximum_key = "maxTotal" if column == "total" else f"max_{column}"
    try:
        maximum = float(row.get(maximum_key) or 0)
    except (TypeError, ValueError):
        maximum = 0.0
    return f"{format_number(amount)} ({format_number(maximum or amount)})"


def system_resource_totals(scans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum each resource across every scanned body in a system.

    Two planets yielding 40 and 60 of the same resource make that system worth
    100 of it, which is the figure that actually decides where to settle.
    Extractor counts are preferred, falling back to the raw resource figure for
    scans taken before extractors were recorded.
    """
    systems: dict[str, dict[str, Any]] = {}
    for scan in scans or []:
        if not isinstance(scan, dict):
            continue
        name = str(scan.get("system_name") or "").strip()
        if not name:
            continue
        system_key = name.casefold()
        row = systems.setdefault(
            system_key,
            {"system": name, "planets": 0, "moonBodies": 0, "maxBases": 0,
             "total": 0.0, "maxTotal": 0.0,
             **{f"{SCAN_RESOURCE_COLUMN_PREFIX}{res}": 0.0
                for res in SCAN_RESOURCE_KEYS},
             **{f"max_{SCAN_RESOURCE_COLUMN_PREFIX}{res}": 0.0
                for res in SCAN_RESOURCE_KEYS}},
        )
        row["planets"] += 1
        multiplier = scan_extraction_base_limit(scan)
        if scan_is_moon(scan):
            row["moonBodies"] += 1
        row["maxBases"] += multiplier
        for resource, amount in scan_yield_amounts(scan).items():
            row[f"{SCAN_RESOURCE_COLUMN_PREFIX}{resource}"] += amount
            row["total"] += amount
            row[f"max_{SCAN_RESOURCE_COLUMN_PREFIX}{resource}"] += amount * multiplier
            row["maxTotal"] += amount * multiplier
    return sorted(systems.values(),
                  key=lambda row: (-row["total"], row["system"].casefold()))


def scan_best_resources(scan: dict[str, Any], limit: int = 3) -> str:
    ranked = scan_positive_resources(scan)
    return ", ".join(f"{name} {format_number(amount)}" for amount, name in ranked[:limit]) or "No useful yield"


def scan_yield_summary(scan: dict[str, Any], limit: int = 0) -> str:
    """Recorded extractor yield for one scanned body, with legacy fallback."""
    ranked = resource_yield_entries(scan_yield_amounts(scan))
    if limit:
        ranked = ranked[:limit]
    return ", ".join(f"{name} {format_number(amount)}" for name, amount in ranked) or "No useful yield"


def scans_for_system(scans: list[dict[str, Any]] | None, system_name: str) -> list[dict[str, Any]]:
    """Captured body scans for a map system, sorted for a stable detail panel."""
    target = str(system_name or "").strip().casefold()
    if not target:
        return []
    matches = [
        scan for scan in scans or []
        if isinstance(scan, dict)
        and str(scan.get("system_name") or "").strip().casefold() == target
    ]
    return sorted(
        matches,
        key=lambda scan: (
            str(scan.get("planet_name") or "").casefold(),
            str(scan.get("planet_id") or ""),
        ),
    )


def scan_column_raw_value(
    scan: dict[str, Any],
    column: str,
    annotation: dict[str, Any] | None = None,
    personal: dict[str, Any] | None = None,
) -> Any:
    annotation = annotation if isinstance(annotation, dict) else {}
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(personal, column)
    quality_label, quality_score = scan_quality(scan)
    if column == "name":
        return scan.get("planet_name")
    if column == "system":
        return annotation.get("systemName") or scan.get("system_name")
    if column == "type":
        return scan.get("planet_type")
    if column == "colony":
        return quality_label
    if column == "score":
        return quality_score
    if column in {"atmosphere", "temperature", "gravity", "geology", "ecology"}:
        return scan_environment_value(scan, column)
    if column == "resources":
        return scan_best_resources(scan)
    if column == "resource_count":
        return len(scan_positive_resources(scan))
    if column == "top_yield":
        ranked = scan_positive_resources(scan)
        return ranked[0][0] if ranked else None
    if column.startswith(SCAN_RESOURCE_COLUMN_PREFIX):
        # Yield of one specific resource, so the list can be sorted by it.
        # Extractors are the useful figure and fall back to the raw resource
        # count when a scan predates that field.
        name = column[len(SCAN_RESOURCE_COLUMN_PREFIX):]
        for source in ("extractors", "resources"):
            values = scan.get(source)
            if isinstance(values, dict) and name in values:
                try:
                    amount = float(values[name])
                except (TypeError, ValueError):
                    continue
                return amount if amount > 0 else None
        return None
    if column == "extractor":
        ranked = scan_positive_resources(scan, "extractors")
        return f"{ranked[0][1]} {format_number(ranked[0][0])}" if ranked else None
    if column == "base":
        return 1 if annotation.get("hasBase") else 0
    if column == "count":
        return int(annotation.get("baseCount") or 0)
    if column == "range":
        return scan.get("scan_range")
    if column == "observed":
        return scan.get("observedAt")
    if column == "id":
        return scan.get("planet_id")
    return None


def scan_column_display_value(
    scan: dict[str, Any],
    column: str,
    annotation: dict[str, Any] | None = None,
    personal: dict[str, Any] | None = None,
) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(personal, column)
    value = scan_column_raw_value(scan, column, annotation, personal)
    if column == "colony":
        _label, score = scan_quality(scan)
        return f"{value} · {score:.0f}" if score is not None else str(value or "Unknown")
    if column == "base":
        return "YES" if value else "—"
    if column == "range":
        return f"{format_number(value)} u" if value not in (None, "") else "-"
    return format_number(value)


def scan_column_sort_value(
    scan: dict[str, Any],
    column: str,
    annotation: dict[str, Any] | None = None,
    personal: dict[str, Any] | None = None,
) -> Any:
    value = scan_column_raw_value(scan, column, annotation, personal)
    if value is None or value == "":
        return None
    if SCAN_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        return fitting.number(value)
    return str(value).casefold()


def _numeric_query_matches(actual: float | None, operator: str, expected: float) -> bool:
    if actual is None:
        return False
    return {
        ">": actual > expected,
        ">=": actual >= expected,
        "<": actual < expected,
        "<=": actual <= expected,
        "=": actual == expected,
    }[operator]


def scan_matches_query(
    scan: dict[str, Any],
    annotation: dict[str, Any],
    query: str,
    personal: dict[str, Any] | None = None,
) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    environments = {
        key: scan_environment_value(scan, key)
        for key in ("atmosphere", "temperature", "gravity", "geology", "ecology")
    }
    resources = " ".join(
        f"{name.replace('_', ' ')} {value}"
        for name, value in (scan.get("resources") or {}).items()
    )
    extractors = " ".join(
        f"{name.replace('_', ' ')} {value}"
        for name, value in (scan.get("extractors") or {}).items()
    )
    quality_label, quality_score = scan_quality(scan)
    base_count = int(annotation.get("baseCount") or 0)
    base_text = f"{'has base yes' if annotation.get('hasBase') else 'no base'} {base_count}"
    fields = {
        "planet": str(scan.get("planet_name") or ""),
        "name": str(scan.get("planet_name") or ""),
        "id": str(scan.get("planet_id") or ""),
        "system": str(annotation.get("systemName") or scan.get("system_name") or ""),
        "type": str(scan.get("planet_type") or ""),
        "quality": quality_label,
        "rating": f"{quality_label} {quality_score if quality_score is not None else ''}",
        "score": str(quality_score if quality_score is not None else ""),
        "resource": resources,
        "resources": resources,
        "extractor": extractors,
        "environment": " ".join(environments.values()),
        "env": " ".join(environments.values()),
        "base": base_text,
        "bases": base_text,
        "range": str(scan.get("scan_range") or ""),
        "date": str(scan.get("observedAt") or ""),
        "scanned": str(scan.get("observedAt") or ""),
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "category": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
        **environments,
        "temp": environments["temperature"],
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        "score": quality_score,
        "yield": scan_positive_resources(scan)[0][0] if scan_positive_resources(scan) else None,
        "bases": float(base_count),
        "range": fitting.number(scan.get("scan_range")) if scan.get("scan_range") not in (None, "") else None,
    }

    for token in tokens:
        numeric = re.fullmatch(r"(score|yield|bases|range)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            if not _numeric_query_matches(numeric_values[key.casefold()], operator, float(expected)):
                return False
            continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


STATION_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "system": {"label": "SYSTEM", "width": 175, "minwidth": 125, "anchor": "w", "kind": "text"},
    "kind": {"label": "KIND", "width": 72, "minwidth": 60, "anchor": "center", "kind": "text"},
    "sources": {"label": "SOURCE", "width": 170, "minwidth": 115, "anchor": "w", "kind": "text"},
    "items": {"label": "ITEMS", "width": 64, "minwidth": 54, "anchor": "e", "kind": "number", "first_desc": True},
    "priced": {"label": "PRICED", "width": 68, "minwidth": 56, "anchor": "e", "kind": "number", "first_desc": True},
    "coverage": {"label": "PRICE COVERAGE", "width": 112, "minwidth": 88, "anchor": "e", "kind": "number", "first_desc": True},
    "last": {"label": "LAST VISIT / SIGHTING", "width": 150, "minwidth": 125, "anchor": "w", "kind": "text", "first_desc": True},
}
STATION_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})

STATION_DEFAULT_COLUMNS = ("system", "kind", "sources", "items", "last")
STATION_COLUMN_PRESETS = {
    "Overview": STATION_DEFAULT_COLUMNS,
    "Trade coverage": ("system", "kind", "items", "priced", "coverage", "last"),
    "Provenance": ("system", "kind", "sources", "last"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "system", "kind", "items", "last"),
}

STATION_ITEM_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    key: dict(spec)
    for key, spec in ITEM_COLUMN_SPECS.items()
    if key != "art"
}
STATION_ITEM_COLUMN_SPECS.update(
    {
        "stock": {"label": "STOCK", "width": 68, "minwidth": 56, "anchor": "e", "kind": "number", "first_desc": True},
        "observed": {"label": "OBSERVED", "width": 142, "minwidth": 120, "anchor": "w", "kind": "text", "first_desc": True},
    }
)
STATION_ITEM_DEFAULT_COLUMNS = ("category", "tech", "stock", "buy", "sell")
STATION_ITEM_COLUMN_PRESETS = {
    "Market": STATION_ITEM_DEFAULT_COLUMNS,
    "Combat": ("category", "tech", "damage", "damage_type", "fire_rate", "range", "energy_cost", "stock", "buy", "sell"),
    "Fitting": ("category", "tech", "size", "mass", "thrust", "turning", "shield_bank", "energy_output", "stock", "buy"),
    "Observation": ("category", "tech", "stock", "buy", "sell", "observed"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "category", "tech", "stock", "buy", "sell"),
}


def station_column_raw_value(
    station: dict[str, Any],
    column: str,
    system_name: str = "",
    kind: str = "",
    personal: dict[str, Any] | None = None,
) -> Any:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(personal, column)
    if column == "name":
        return station.get("name")
    if column == "system":
        return system_name or station.get("systemName")
    if column == "kind":
        return kind
    if column == "sources":
        return ", ".join(str(value) for value in station.get("sources", []))
    if column == "items":
        return station.get("itemCount")
    if column == "priced":
        return station.get("pricedItemCount")
    if column == "coverage":
        total = fitting.number(station.get("itemCount"))
        priced = fitting.number(station.get("pricedItemCount"))
        return (priced / total * 100.0) if total > 0 else None
    if column == "last":
        return station.get("lastSeen")
    return None


def station_column_display_value(
    station: dict[str, Any],
    column: str,
    system_name: str = "",
    kind: str = "",
    personal: dict[str, Any] | None = None,
) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(personal, column)
    value = station_column_raw_value(station, column, system_name, kind, personal)
    if column == "coverage":
        return f"{value:.0f}%" if isinstance(value, (int, float)) else "-"
    return format_number(value)


def station_column_sort_value(
    station: dict[str, Any],
    column: str,
    system_name: str = "",
    kind: str = "",
    personal: dict[str, Any] | None = None,
) -> Any:
    value = station_column_raw_value(station, column, system_name, kind, personal)
    if value is None or value == "":
        return None
    if STATION_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        return fitting.number(value)
    return str(value).casefold()


def station_matches_query(
    station: dict[str, Any],
    catalog_items: list[dict[str, Any]],
    system_name: str,
    kind: str,
    query: str,
    personal: dict[str, Any] | None = None,
) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    station_id = station.get("id")
    offered = [
        item for item in catalog_items
        if any(market.get("stationId") == station_id for market in item.get("markets", []))
    ]
    item_names = " ".join(str(item.get("name") or "") for item in offered)
    categories = " ".join(str(item.get("categoryLabel") or item.get("category") or "") for item in offered)
    fields = {
        "station": str(station.get("name") or ""),
        "name": str(station.get("name") or ""),
        "system": system_name,
        "kind": kind,
        "source": " ".join(str(value) for value in station.get("sources", [])),
        "item": item_names,
        "items": item_names,
        "category": categories,
        "date": str(station.get("lastSeen") or ""),
        "seen": str(station.get("lastSeen") or ""),
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        "items": fitting.number(station.get("itemCount")),
        "priced": fitting.number(station.get("pricedItemCount")),
        "coverage": station_column_raw_value(station, "coverage", system_name, kind),
    }
    for token in tokens:
        numeric = re.fullmatch(r"(items|priced|coverage)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            if not _numeric_query_matches(numeric_values[key.casefold()], operator, float(expected)):
                return False
            continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


def station_item_column_raw_value(
    item: dict[str, Any],
    markets: list[dict[str, Any]],
    column: str,
    personal: dict[str, Any] | None = None,
) -> Any:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(personal, column)
    if column == "stock":
        values = [market.get("stock") for market in markets if isinstance(market.get("stock"), (int, float)) and market.get("stock") >= 0]
        return max(values) if values else None
    if column == "buy":
        values = [market.get("buyPrice") for market in markets if isinstance(market.get("buyPrice"), (int, float)) and market.get("buyPrice") > 0]
        return min(values) if values else None
    if column == "sell":
        values = [market.get("sellPrice") for market in markets if isinstance(market.get("sellPrice"), (int, float)) and market.get("sellPrice") > 0]
        return max(values) if values else None
    if column == "observed":
        values = [str(market.get("observedAt") or "") for market in markets if market.get("observedAt")]
        return max(values) if values else None
    return item_column_raw_value(item, column, personal)


def station_item_column_display_value(
    item: dict[str, Any],
    markets: list[dict[str, Any]],
    column: str,
    personal: dict[str, Any] | None = None,
) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(personal, column)
    value = station_item_column_raw_value(item, markets, column, personal)
    if column in {"buy", "sell"}:
        return compact_number(value)
    return format_number(value)


def station_item_column_sort_value(
    item: dict[str, Any],
    markets: list[dict[str, Any]],
    column: str,
    personal: dict[str, Any] | None = None,
) -> Any:
    value = station_item_column_raw_value(item, markets, column, personal)
    if value is None or value == "":
        return None
    if STATION_ITEM_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        if column == "damage":
            multiple = re.match(r"\s*([-+]?\d+(?:\.\d+)?)\s*[x×]\s*(\d+)", str(value), re.IGNORECASE)
            if multiple:
                return float(multiple.group(1)) * int(multiple.group(2))
        return fitting.number(value)
    return str(value).casefold()


def station_item_matches_query(
    item: dict[str, Any],
    markets: list[dict[str, Any]],
    query: str,
    personal: dict[str, Any] | None = None,
) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    fields = {
        "item": str(item.get("name") or ""),
        "name": str(item.get("name") or ""),
        "category": str(item.get("categoryLabel") or item.get("category") or ""),
        "rarity": str(item.get("rarity") or ""),
        "stats": " ".join(f"{key} {value}" for key, value in (item.get("stats") or {}).items()),
        "tech": str(item.get("tech") or ""),
        "size": str(item.get("cargoSize") or ""),
        "stock": str(station_item_column_raw_value(item, markets, "stock") or ""),
        "buy": str(station_item_column_raw_value(item, markets, "buy") or ""),
        "sell": str(station_item_column_raw_value(item, markets, "sell") or ""),
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        key: station_item_column_sort_value(item, markets, key)
        for key, spec in STATION_ITEM_COLUMN_SPECS.items()
        if spec.get("kind") == "number"
    }
    for token in tokens:
        numeric = re.fullmatch(r"([a-z][a-z0-9_]*)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            key = key.casefold()
            if key in numeric_values:
                if not _numeric_query_matches(numeric_values[key], operator, float(expected)):
                    return False
                continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


TRAINING_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "station": {"label": "NPC TRAINER", "width": 195, "minwidth": 140, "anchor": "w", "kind": "text"},
    "system": {"label": "SYSTEM", "width": 155, "minwidth": 115, "anchor": "w", "kind": "text"},
    "level": {"label": "LEVEL / CAP", "width": 92, "minwidth": 78, "anchor": "center", "kind": "number", "first_desc": True},
    "cap": {"label": "TRAINER CAP", "width": 88, "minwidth": 74, "anchor": "e", "kind": "number", "first_desc": True},
    "global_cap": {"label": "GLOBAL CAP", "width": 86, "minwidth": 72, "anchor": "e", "kind": "number", "first_desc": True},
    "sp": {"label": "SP", "width": 58, "minwidth": 50, "anchor": "e", "kind": "number"},
    "credits": {"label": "CREDITS", "width": 86, "minwidth": 68, "anchor": "e", "kind": "number"},
    "item": {"label": "ITEM REQUIREMENT", "width": 180, "minwidth": 120, "anchor": "w", "kind": "text"},
    "status": {"label": "STATUS", "width": 112, "minwidth": 92, "anchor": "center", "kind": "text"},
    "observed": {"label": "OBSERVED", "width": 142, "minwidth": 120, "anchor": "w", "kind": "text", "first_desc": True},
}
TRAINING_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})
TRAINING_DEFAULT_COLUMNS = ("station", "system", "level", "sp", "credits", "item", "status")
TRAINING_COLUMN_PRESETS = {
    "Training": TRAINING_DEFAULT_COLUMNS,
    "Caps": ("station", "system", "level", "cap", "global_cap", "status", "observed"),
    "Costs": ("station", "system", "sp", "credits", "item", "status"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "station", "system", "level", "status"),
}


def training_offer_status(offer: dict[str, Any]) -> str:
    if offer.get("atStationCap"):
        return "At station cap"
    if offer.get("canTrainNow"):
        return "Ready now"
    if not offer.get("canAffordItem", True):
        return "Needs items"
    if not offer.get("canAffordSp", True) or not offer.get("canAffordCredits", True):
        return "Needs SP / credits"
    return "Unavailable"


def training_item_cost_text(offer: dict[str, Any]) -> str:
    needed = int(fitting.number(offer.get("itemCostNeeded")))
    if needed <= 0:
        return "-"
    display = str(offer.get("itemCostDisplay") or offer.get("itemCostType") or "Item").replace("_", " ").title()
    return f"{needed:,} × {display}"


def training_column_raw_value(offer: dict[str, Any], column: str, personal: dict[str, Any] | None = None) -> Any:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(personal, column)
    if column == "name":
        return offer.get("displayName") or offer.get("skillId")
    if column == "station":
        return offer.get("stationName")
    if column == "system":
        return offer.get("systemName")
    if column == "level":
        return offer.get("currentLevel")
    if column == "cap":
        return offer.get("offeredMax")
    if column == "global_cap":
        return offer.get("globalMax")
    if column == "sp":
        return offer.get("nextSpCost")
    if column == "credits":
        return offer.get("nextCreditCost")
    if column == "item":
        return training_item_cost_text(offer)
    if column == "status":
        return training_offer_status(offer)
    if column == "observed":
        return offer.get("observedAt")
    return None


def training_column_display_value(offer: dict[str, Any], column: str, personal: dict[str, Any] | None = None) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(personal, column)
    value = training_column_raw_value(offer, column, personal)
    if column == "level":
        return f"{int(fitting.number(offer.get('currentLevel')))}/{int(fitting.number(offer.get('offeredMax')))}"
    if column == "credits":
        return f"{format_number(value, '0')} cr" if fitting.number(value) > 0 else "-"
    if column == "status":
        return str(value or "").upper()
    return format_number(value)


def training_column_sort_value(offer: dict[str, Any], column: str, personal: dict[str, Any] | None = None) -> Any:
    value = training_column_raw_value(offer, column, personal)
    if value is None or value == "":
        return None
    if TRAINING_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        return fitting.number(value)
    return str(value).casefold()


def training_matches_query(offer: dict[str, Any], query: str, personal: dict[str, Any] | None = None) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    fields = {
        "skill": str(offer.get("displayName") or offer.get("skillId") or ""),
        "name": str(offer.get("displayName") or offer.get("skillId") or ""),
        "station": str(offer.get("stationName") or ""),
        "system": str(offer.get("systemName") or ""),
        "item": training_item_cost_text(offer),
        "status": training_offer_status(offer),
        "bonus": " ".join((*map(str, (offer.get("statBonus") or {}).keys()), *map(str, (offer.get("pctBonus") or {}).keys()))),
        "description": str(offer.get("description") or ""),
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        "level": fitting.number(offer.get("currentLevel")),
        "cap": fitting.number(offer.get("offeredMax")),
        "sp": fitting.number(offer.get("nextSpCost")),
        "credits": fitting.number(offer.get("nextCreditCost")),
    }
    for token in tokens:
        numeric = re.fullmatch(r"(level|cap|sp|credits)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            if not _numeric_query_matches(numeric_values[key.casefold()], operator, float(expected)):
                return False
            continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


PLAYER_SKILL_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "rank": {"label": "RANK", "width": 78, "minwidth": 68, "anchor": "center", "kind": "number", "first_desc": True},
    "maximum": {"label": "MAX", "width": 58, "minwidth": 50, "anchor": "e", "kind": "number", "first_desc": True},
    "cost": {"label": "SPENT", "width": 66, "minwidth": 56, "anchor": "e", "kind": "number", "first_desc": True},
    "next_sp": {"label": "NEXT SP", "width": 72, "minwidth": 60, "anchor": "e", "kind": "number"},
    "next_credits": {"label": "NEXT CREDITS", "width": 98, "minwidth": 78, "anchor": "e", "kind": "number"},
    "bonus": {"label": "EFFECT AT CURRENT RANK", "width": 420, "minwidth": 240, "anchor": "w", "kind": "text"},
}
PLAYER_SKILL_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})
PLAYER_SKILL_DEFAULT_COLUMNS = ("rank", "cost", "bonus")
PLAYER_SKILL_COLUMN_PRESETS = {
    "Overview": PLAYER_SKILL_DEFAULT_COLUMNS,
    "Training costs": ("rank", "maximum", "next_sp", "next_credits", "bonus"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "rank", "maximum", "bonus"),
}


def player_skill_column_raw_value(skill: dict[str, Any], column: str, personal: dict[str, Any] | None = None) -> Any:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(personal, column)
    if column == "name":
        return skill.get("display_name") or skill.get("name") or skill.get("skill_id")
    return {
        "rank": skill.get("level"),
        "maximum": skill.get("max_level"),
        "cost": skill.get("cost_paid"),
        "next_sp": skill.get("next_cost"),
        "next_credits": skill.get("next_credit_cost"),
    }.get(column)


def player_skill_column_sort_value(skill: dict[str, Any], column: str, personal: dict[str, Any] | None = None) -> Any:
    value = player_skill_column_raw_value(skill, column, personal)
    if column == "bonus":
        value = " ".join((*map(str, (skill.get("stat_bonus") or {}).keys()), *map(str, (skill.get("pct_bonus") or {}).keys())))
    if value is None or value == "":
        return None
    if PLAYER_SKILL_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        return fitting.number(value)
    return str(value).casefold()


def player_skill_matches_query(skill: dict[str, Any], query: str, personal: dict[str, Any] | None = None) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    bonus = " ".join((*map(str, (skill.get("stat_bonus") or {}).keys()), *map(str, (skill.get("pct_bonus") or {}).keys())))
    fields = {
        "skill": str(skill.get("display_name") or skill.get("name") or skill.get("skill_id") or ""),
        "name": str(skill.get("display_name") or skill.get("name") or skill.get("skill_id") or ""),
        "bonus": bonus,
        "description": str(skill.get("description") or ""),
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        "rank": fitting.number(skill.get("level")),
        "max": fitting.number(skill.get("max_level")),
        "spent": fitting.number(skill.get("cost_paid")),
        "nextsp": fitting.number(skill.get("next_cost")),
        "credits": fitting.number(skill.get("next_credit_cost")),
    }
    for token in tokens:
        numeric = re.fullmatch(r"(rank|max|spent|nextsp|credits)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            if not _numeric_query_matches(numeric_values[key.casefold()], operator, float(expected)):
                return False
            continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


MAP_RESULT_COLUMN_SPECS: dict[str, dict[str, Any]] = {
    "kind": {"label": "TYPE", "width": 76, "minwidth": 64, "anchor": "w", "kind": "text"},
    "location": {"label": "LOCATION", "width": 145, "minwidth": 100, "anchor": "w", "kind": "text"},
    "details": {"label": "SUMMARY", "width": 190, "minwidth": 120, "anchor": "w", "kind": "text"},
    "observed": {"label": "OBSERVED", "width": 138, "minwidth": 115, "anchor": "w", "kind": "text", "first_desc": True},
}


def system_matches_query(system: dict[str, Any], query: str, personal: dict[str, Any] | None = None) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    planet_types = " ".join(f"{name} {count}" for name, count in (system.get("planetTypes") or {}).items())
    fields = {
        "system": str(system.get("name") or ""),
        "name": str(system.get("name") or ""),
        "ownership": str(system.get("ownership") or ""),
        "planet": planet_types,
        "planets": planet_types,
        "explored": "yes explored" if system.get("explored") else "no unexplored",
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    station_counts = system.get("stationCounts") if isinstance(system.get("stationCounts"), dict) else {}
    numeric_values = {
        "hazard": fitting.number(system.get("hazard")),
        "stations": fitting.number(system.get("npcStationCount")) + sum(fitting.number(value) for value in station_counts.values()),
        "planets": sum(fitting.number(value) for value in (system.get("planetTypes") or {}).values()) + fitting.number(system.get("moonCount")),
    }
    for token in tokens:
        numeric = re.fullmatch(r"(hazard|stations|planets)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            if not _numeric_query_matches(numeric_values[key.casefold()], operator, float(expected)):
                return False
            continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True
MAP_RESULT_COLUMN_SPECS.update({key: dict(spec) for key, spec in PERSONAL_COLUMN_SPECS.items()})
MAP_RESULT_DEFAULT_COLUMNS = ("kind", "location", "details")
MAP_RESULT_COLUMN_PRESETS = {
    "Overview": MAP_RESULT_DEFAULT_COLUMNS,
    "Freshness": ("kind", "location", "details", "observed"),
    "My intel": ("favorite", "watchlist", "personal_category", "tags", "note", "kind", "location", "details"),
}


def map_result_column_raw_value(result: dict[str, Any], column: str) -> Any:
    if column == "name":
        return result.get("name")
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(result.get("personal"), column)
    return result.get(column)


def map_result_column_display_value(result: dict[str, Any], column: str) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(result.get("personal"), column)
    value = map_result_column_raw_value(result, column)
    return format_number(value)


def map_result_column_sort_value(result: dict[str, Any], column: str) -> Any:
    value = map_result_column_raw_value(result, column)
    if value is None or value == "":
        return None
    if MAP_RESULT_COLUMN_SPECS.get(column, {"kind": "text"}).get("kind") == "number":
        return fitting.number(value)
    return str(value).casefold()


def item_stat_value(item: dict[str, Any], labels: tuple[str, ...]) -> Any:
    values = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    folded = {str(label).casefold(): value for label, value in values.items()}
    for label in labels:
        if label.casefold() in folded:
            return folded[label.casefold()]
    return None


def item_column_raw_value(item: dict[str, Any], column: str, annotation: dict[str, Any] | None = None) -> Any:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_raw_value(annotation, column)
    if column == "name":
        return item.get("name")
    if column == "category":
        return item.get("categoryLabel") or item.get("category")
    if column == "tech":
        return item.get("tech")
    if column == "size":
        return item.get("cargoSize")
    if column == "buy":
        prices = positive_prices(item, "buyPrice")
        return min(prices) if prices else None
    if column == "sell":
        prices = positive_prices(item, "sellPrice")
        return max(prices) if prices else None
    if column == "art":
        return "OFFICIAL" if item.get("art") else "INTEL"
    if column == "rarity":
        return item.get("rarity")
    spec = ITEM_COLUMN_SPECS.get(column, {})
    labels = spec.get("stats")
    return item_stat_value(item, labels) if isinstance(labels, tuple) else None


def item_column_display_value(item: dict[str, Any], column: str, annotation: dict[str, Any] | None = None) -> str:
    if column in PERSONAL_COLUMN_SPECS:
        return personal_column_display_value(annotation, column)
    value = item_column_raw_value(item, column, annotation)
    if column in {"buy", "sell"}:
        return compact_number(value)
    if column in {"tech", "size"}:
        return format_number(value)
    return format_number(value)


def item_column_sort_value(item: dict[str, Any], column: str, annotation: dict[str, Any] | None = None) -> Any:
    value = item_column_raw_value(item, column, annotation)
    if value is None or value == "" or value == "-":
        return None
    spec = ITEM_COLUMN_SPECS.get(column, {"kind": "text"})
    if spec.get("kind") == "number":
        text = str(value).replace(",", "")
        if column == "damage":
            multiple = re.match(r"\s*([-+]?\d+(?:\.\d+)?)\s*[x×]\s*(\d+)", text, re.IGNORECASE)
            if multiple:
                return float(multiple.group(1)) * int(multiple.group(2))
        return fitting.number(value)
    return str(value).casefold()


def item_matches_query(item: dict[str, Any], query: str, personal: dict[str, Any] | None = None) -> bool:
    query = str(query or "").strip()
    if not query:
        return True
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    stats = " ".join(f"{key} {value}" for key, value in (item.get("stats") or {}).items())
    stations = " ".join(str(market.get("stationName") or "") for market in item.get("markets", []))
    fields = {
        "item": str(item.get("name") or ""),
        "name": str(item.get("name") or ""),
        "type": str(item.get("type") or ""),
        "category": str(item.get("categoryLabel") or item.get("category") or ""),
        "description": str(item.get("description") or ""),
        "rarity": str(item.get("rarity") or ""),
        "stats": stats,
        "station": stations,
        "favorite": "yes favorite" if (personal or {}).get("favorite") else "no",
        "watch": "yes watchlist watched" if (personal or {}).get("watchlist") else "no",
        "mycategory": str((personal or {}).get("category") or ""),
        "tag": " ".join(str(value) for value in (personal or {}).get("tags", [])),
        "note": str((personal or {}).get("note") or ""),
    }
    all_text = " ".join(fields.values()).casefold()
    numeric_values = {
        key: item_column_sort_value(item, key, personal)
        for key, spec in ITEM_COLUMN_SPECS.items()
        if spec.get("kind") == "number"
    }
    for token in tokens:
        numeric = re.fullmatch(r"([a-z][a-z0-9_]*)(<=|>=|=|<|>)(-?\d+(?:\.\d+)?)", token, re.IGNORECASE)
        if numeric:
            key, operator, expected = numeric.groups()
            key = key.casefold()
            if key in numeric_values:
                if not _numeric_query_matches(numeric_values[key], operator, float(expected)):
                    return False
                continue
        if ":" in token:
            key, wanted = token.split(":", 1)
            key = key.casefold()
            if key in fields:
                if wanted.casefold() not in fields[key].casefold():
                    return False
                continue
        if token.casefold() not in all_text:
            return False
    return True


class StarEmpireDesktop:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.data: dict[str, Any] = {}
        self.items: list[dict[str, Any]] = []
        self.filtered_items: list[dict[str, Any]] = []
        self.item_by_id: dict[str, dict[str, Any]] = {}
        self.station_by_label: dict[str, str] = {}
        self.current_station_id: str | None = None
        self.current_page = "items"
        self.current_photo: ImageTk.PhotoImage | None = None
        self.current_scan_photo: ImageTk.PhotoImage | None = None
        self.current_ship_photo: ImageTk.PhotoImage | None = None
        self.item_icons: dict[str, ImageTk.PhotoImage] = {}
        self.patch_inspection: game_link.PatchInspection | None = None
        self.player_skills: list[dict[str, Any]] = []
        self.current_fit_state: dict[str, Any] = {}
        self.fit_state: dict[str, Any] = {}
        self.fit_row_targets: dict[str, tuple[str, int | None]] = {}
        self.fit_apply_skills_var = tk.BooleanVar(value=True)
        self.saved_fit_var = tk.StringVar(value="Unsaved fit")
        self.saved_fit_by_label: dict[str, str] = {}
        self.current_saved_fit_id: str | None = None
        self.map_result_targets: dict[str, dict[str, Any]] = {}
        self.map_highlight_systems: set[str] = set()
        self.map_selected_system: str | None = None
        self.map_zoom = 1.0
        self.map_pan_x = 0.0
        self.map_pan_y = 0.0
        self.map_drag_origin: tuple[int, int] | None = None
        self.map_resize_after: str | None = None
        self.map_zoom_redraw_after: str | None = None
        self._map_territory_cache: tuple[Any, ...] | None = None
        self.map_territory_photo: ImageTk.PhotoImage | None = None
        self.map_territory_label_photo: ImageTk.PhotoImage | None = None
        self.search_after: str | None = None
        self.loading = False
        self.application_update_queue: queue.Queue[tuple[Any | None, Exception | None]] = queue.Queue(maxsize=1)
        self.application_update_active = False
        self.global_search_dialog: tk.Toplevel | None = None
        self.global_search_results: dict[str, dict[str, Any]] = {}
        self.global_saved_search_by_label: dict[str, str] = {}

        self.search_var = tk.StringVar()
        self.station_var = tk.StringVar(value="All stations")
        self.sort_var = tk.StringVar(value="Name")
        self.item_sort_column = "name"
        self.item_sort_desc = False
        self.item_column_vars = {
            key: tk.BooleanVar(value=key in ITEM_DEFAULT_COLUMNS)
            for key in ITEM_COLUMN_SPECS
        }
        self.item_display_order = list(ITEM_DEFAULT_COLUMNS)
        self.item_column_menu: tk.Menu | None = None
        self.item_pending_xview: float | None = None
        self.official_only_var = tk.BooleanVar(value=False)
        self.priced_only_var = tk.BooleanVar(value=False)
        self.map_search_var = tk.StringVar()
        self.map_mode_var = tk.StringVar(value="Everything")
        self.map_show_names_var = tk.BooleanVar(value=True)
        self.map_show_coalition_var = tk.BooleanVar(value=True)
        self.map_result_var = tk.StringVar(value="Awaiting galaxy data")
        self.map_result_sort_column = "name"
        self.map_result_sort_desc = False
        self.map_result_column_vars = {
            key: tk.BooleanVar(value=key in MAP_RESULT_DEFAULT_COLUMNS)
            for key in MAP_RESULT_COLUMN_SPECS
        }
        self.map_result_display_order = list(MAP_RESULT_DEFAULT_COLUMNS)
        self.map_result_column_menu: tk.Menu | None = None
        self.map_result_pending_xview: float | None = None
        self.station_search_var = tk.StringVar()
        self.station_kind_var = tk.StringVar(value="All kinds")
        self.station_location_var = tk.StringVar(value="All locations")
        self.station_sort_column = "name"
        self.station_sort_desc = False
        self.station_column_vars = {
            key: tk.BooleanVar(value=key in STATION_DEFAULT_COLUMNS)
            for key in STATION_COLUMN_SPECS
        }
        self.station_display_order = list(STATION_DEFAULT_COLUMNS)
        self.station_column_menu: tk.Menu | None = None
        self.station_pending_xview: float | None = None
        self.station_item_search_var = tk.StringVar()
        self.station_item_result_var = tk.StringVar()
        self.station_item_sort_column = "name"
        self.station_item_sort_desc = False
        self.station_item_column_vars = {
            key: tk.BooleanVar(value=key in STATION_ITEM_DEFAULT_COLUMNS)
            for key in STATION_ITEM_COLUMN_SPECS
        }
        self.station_item_display_order = list(STATION_ITEM_DEFAULT_COLUMNS)
        self.station_item_column_menu: tk.Menu | None = None
        self.station_item_pending_xview: float | None = None
        self.training_search_var = tk.StringVar()
        self.training_system_var = tk.StringVar(value="All systems")
        self.training_station_var = tk.StringVar(value="All stations")
        self.training_status_var = tk.StringVar(value="All offers")
        self.training_offer_targets: dict[str, dict[str, Any]] = {}
        self.training_sort_column = "name"
        self.training_sort_desc = False
        self.training_column_vars = {
            key: tk.BooleanVar(value=key in TRAINING_DEFAULT_COLUMNS)
            for key in TRAINING_COLUMN_SPECS
        }
        self.training_display_order = list(TRAINING_DEFAULT_COLUMNS)
        self.training_column_menu: tk.Menu | None = None
        self.training_pending_xview: float | None = None
        self.scan_search_var = tk.StringVar()
        self.scan_system_filter_var = tk.StringVar(value="All systems")
        self.scan_type_filter_var = tk.StringVar(value="All types")
        self.scan_quality_filter_var = tk.StringVar(value="All colony ratings")
        self.scan_base_filter_var = tk.StringVar(value="All base records")
        self.coverage_search_var = tk.StringVar(value="")
        self.coverage_origin_var = tk.StringVar(value="")
        self.coverage_scope_var = tk.StringVar(value="Reachable only")
        self.coverage_result_var = tk.StringVar(value="")
        self.coverage_sort_column = "hops"
        self.coverage_sort_desc = False
        self.system_yield_search_var = tk.StringVar(value="")
        self.system_yield_result_var = tk.StringVar(value="")
        self.system_yield_order_var = tk.StringVar(value="Total yield")
        self.system_yield_sort_column = "total"
        self.system_yield_sort_desc = True
        self.scan_sort_column = "name"
        self.scan_sort_desc = False
        self.scan_column_vars = {
            key: tk.BooleanVar(value=key in SCAN_DEFAULT_COLUMNS)
            for key in SCAN_COLUMN_SPECS
        }
        self.scan_display_order = list(SCAN_DEFAULT_COLUMNS)
        self.scan_column_menu: tk.Menu | None = None
        self.scan_pending_xview: float | None = None
        self.scan_system_var = tk.StringVar()
        self.scan_has_base_var = tk.BooleanVar(value=False)
        self.scan_base_count_var = tk.StringVar(value="0")
        self.player_skill_search_var = tk.StringVar()
        self.player_skill_result_var = tk.StringVar()
        self.player_skill_sort_column = "name"
        self.player_skill_sort_desc = False
        self.player_skill_column_vars = {
            key: tk.BooleanVar(value=key in PLAYER_SKILL_DEFAULT_COLUMNS)
            for key in PLAYER_SKILL_COLUMN_SPECS
        }
        self.player_skill_display_order = list(PLAYER_SKILL_DEFAULT_COLUMNS)
        self.player_skill_column_menu: tk.Menu | None = None
        self.player_skill_pending_xview: float | None = None
        self.user_state = UserStateStore()
        saved_game_directory = self.user_state.game_directory()
        if saved_game_directory:
            app.configure_game_root(saved_game_directory, require_valid=False)
        self.game_directory_var = tk.StringVar(value=saved_game_directory or str(app.GAME_ROOT))
        self.status_var = tk.StringVar(value="Loading local archive...")
        self.result_var = tk.StringVar(value="")

        self._configure_window()
        self._configure_styles()
        self._build_shell()
        self._build_items_page()
        self._build_stations_page()
        self._build_skill_finder_page()
        self._build_map_page()
        self._build_scans_page()
        self._build_system_yields_page()
        self._build_coverage_page()
        self._build_player_page()
        self._build_ship_page()
        self.root.bind_all("<Control-k>", self._open_global_search)
        self.root.bind_all("<Control-K>", self._open_global_search)
        self.root.bind_all("<Control-f>", self._focus_current_search)
        self.root.bind_all("<Control-F>", self._focus_current_search)
        self.root.bind_all("<Control-r>", self._keyboard_refresh)
        self.root.bind_all("<Control-R>", self._keyboard_refresh)
        self.show_page("items")
        self.root.after(40, self.update_logger_status)
        self.root.after(80, self.refresh_data)

    def _configure_window(self) -> None:
        self.root.title("Star Empire Companion")
        self.root.configure(bg=BG)
        self.root.minsize(1080, 680)
        width = min(1520, max(1180, self.root.winfo_screenwidth() - 120))
        height = min(920, max(720, self.root.winfo_screenheight() - 110))
        screen_x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        screen_y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{screen_x}+{screen_y}")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Archive.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            bordercolor=LINE,
            borderwidth=0,
            rowheight=36,
            font=FONT,
        )
        style.map(
            "Archive.Treeview",
            background=[("selected", PANEL_3)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Archive.Treeview.Heading",
            background=PANEL_2,
            foreground=MUTED,
            bordercolor=LINE,
            relief="flat",
            font=("Cascadia Mono", 8, "bold"),
            padding=(8, 9),
        )
        style.map("Archive.Treeview.Heading", background=[("active", PANEL_3)])
        style.configure(
            "Archive.TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=TEXT,
            arrowcolor=CYAN,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
            padding=6,
        )
        style.map(
            "Archive.TCombobox",
            fieldbackground=[("readonly", PANEL_2)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL_2)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Archive.Vertical.TScrollbar",
            background=PANEL_2,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=MUTED,
        )
        style.configure(
            "Archive.Horizontal.TScrollbar",
            background=PANEL_2,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=MUTED,
        )

    def _build_shell(self) -> None:
        self.root.grid_rowconfigure(3, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.root, bg=BG, padx=22, pady=14)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        logo = tk.Canvas(header, width=48, height=48, bg=BG, highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 13))
        logo.create_oval(5, 5, 43, 43, outline=CYAN, width=1)
        logo.create_oval(0, 17, 48, 31, outline="#2e7899", width=1)
        logo.create_polygon(24, 4, 29, 24, 24, 44, 19, 24, fill="#78ddff", outline="")
        logo.create_oval(34, 9, 39, 14, fill=MINT, outline="")

        tk.Label(
            header,
            text="STAR EMPIRE  //  LOCAL INTEL ARCHIVE",
            bg=BG,
            fg=TEXT,
            font=("Cascadia Mono", 14, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="sw")
        self.source_label = tk.Label(
            header,
            text="Read-only local data",
            bg=BG,
            fg=MUTED,
            font=MONO_SMALL,
            anchor="w",
        )
        self.source_label.grid(row=1, column=1, sticky="nw", pady=(3, 0))

        self.metric_labels: dict[str, tk.Label] = {}
        metric_frame = tk.Frame(header, bg=BG)
        metric_frame.grid(row=0, column=2, rowspan=2, padx=(20, 14))
        for index, (key, caption) in enumerate((("items", "ITEMS"), ("stations", "STATIONS"), ("systems", "SYSTEMS"), ("scans", "SCANS"))):
            box = tk.Frame(metric_frame, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=11, pady=6)
            box.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0))
            value = tk.Label(box, text="-", bg=PANEL, fg=TEXT, font=("Cascadia Mono", 13, "bold"))
            value.pack()
            tk.Label(box, text=caption, bg=PANEL, fg=MUTED, font=("Cascadia Mono", 7)).pack()
            self.metric_labels[key] = value

        self._button(header, "SEARCH  CTRL+K", self._open_global_search, PANEL_2, CYAN).grid(row=0, column=3, rowspan=2, sticky="e", padx=(0, 8))
        self.patch_button = self._button(header, "CHECK GAME LINK", self.check_or_repair_logger, PANEL_2, MINT)
        self.patch_button.grid(row=0, column=4, rowspan=2, sticky="e", padx=(0, 8))
        self.update_button = self._button(header, "CHECK UPDATE", self.check_for_application_update, PANEL_2, AMBER)
        self.update_button.grid(row=0, column=5, rowspan=2, sticky="e", padx=(0, 8))
        self._button(header, "SHARE INTEL", self.open_share_intel, PANEL_2, MINT).grid(
            row=0, column=6, rowspan=2, sticky="e", padx=(0, 8))
        self.refresh_button = self._button(header, "REFRESH DATA", self.refresh_data, CYAN, "#03131b")
        self.refresh_button.grid(row=0, column=7, rowspan=2, sticky="e")

        game_bar = tk.Frame(self.root, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=22, pady=8)
        game_bar.grid(row=1, column=0, sticky="ew")
        game_bar.grid_columnconfigure(1, weight=1)
        tk.Label(game_bar, text="GAME DIRECTORY", bg=PANEL_2, fg=MUTED, font=("Cascadia Mono", 8, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.game_directory_entry = tk.Entry(
            game_bar,
            textvariable=self.game_directory_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            selectbackground=PANEL_3,
            relief="flat",
            bd=0,
            highlightbackground=LINE_BRIGHT,
            highlightcolor=CYAN,
            highlightthickness=1,
            font=MONO,
        )
        self.game_directory_entry.grid(row=0, column=1, sticky="ew", ipady=6)
        self.game_directory_entry.bind("<Return>", lambda _event: self.use_game_directory())
        self._button(game_bar, "BROWSE", self.browse_game_directory, PANEL, CYAN).grid(row=0, column=2, padx=(8, 6))
        self._button(game_bar, "USE FOLDER", self.use_game_directory, CYAN, "#03131b").grid(row=0, column=3)

        nav = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=22)
        nav.grid(row=2, column=0, sticky="ew")
        self.nav_buttons: dict[str, tk.Button] = {}
        for page, label in (
            ("items", "ITEM CATALOG"),
            ("map", "GALAXY MAP"),
            ("stations", "STATION ARCHIVE"),
            ("training", "SKILL FINDER"),
            ("scans", "PLANET ARCHIVE"),
            ("system_yields", "SYSTEM YIELDS"),
            ("coverage", "COVERAGE"),
            ("player", "PLAYER"),
            ("ship", "SHIP FITTING"),
        ):
            button = tk.Button(
                nav,
                text=label,
                command=lambda selected=page: self.show_page(selected),
                bg=PANEL,
                fg=MUTED,
                activebackground=PANEL_2,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                padx=18,
                pady=12,
                cursor="hand2",
                font=("Cascadia Mono", 9, "bold"),
            )
            button.pack(side="left")
            self.nav_buttons[page] = button

        self._button(nav, "?  QUICK HELP", self._open_quick_help, PANEL, MINT).pack(side="right", pady=5)

        self.page_host = tk.Frame(self.root, bg=BG)
        self.page_host.grid(row=3, column=0, sticky="nsew")
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

        status = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=14, pady=7)
        status.grid(row=4, column=0, sticky="ew")
        self.status_dot = tk.Label(status, text="●", bg=PANEL, fg=AMBER, font=("Segoe UI", 10))
        self.status_dot.pack(side="left")
        tk.Label(status, textvariable=self.status_var, bg=PANEL, fg=MUTED, font=MONO_SMALL).pack(side="left", padx=(6, 0))
        tk.Label(
            status,
            text="No game, save, account, or server data is changed",
            bg=PANEL,
            fg="#53708b",
            font=MONO_SMALL,
        ).pack(side="right")

    def _build_items_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)
        self.pages = {"items": page}

        sidebar = tk.Frame(page, bg=PANEL, width=220, highlightbackground=LINE, highlightthickness=1)
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text="CATEGORIES", bg=PANEL, fg=CYAN, font=("Cascadia Mono", 9, "bold"), padx=14, pady=13, anchor="w").pack(fill="x")
        self.category_list = tk.Listbox(
            sidebar,
            bg=PANEL,
            fg=TEXT,
            selectbackground=PANEL_3,
            selectforeground="#ffffff",
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=FONT,
            exportselection=False,
        )
        self.category_list.pack(fill="both", expand=True, padx=5)
        self.category_list.bind("<<ListboxSelect>>", lambda _event: self.apply_filters())

        filters = tk.Frame(sidebar, bg=PANEL_2, padx=12, pady=10)
        filters.pack(fill="x", side="bottom")
        self._checkbutton(filters, "Priced items only", self.priced_only_var, self.apply_filters).pack(anchor="w", pady=2)
        self._checkbutton(filters, "Official artwork only", self.official_only_var, self.apply_filters).pack(anchor="w", pady=2)

        center = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        center.grid_rowconfigure(1, weight=1)
        center.grid_columnconfigure(0, weight=1)

        tools = tk.Frame(center, bg=PANEL_2, padx=10, pady=9)
        tools.grid(row=0, column=0, sticky="ew")
        tools.grid_columnconfigure(0, weight=1)
        search_wrap = tk.Frame(tools, bg=BG, highlightbackground=LINE_BRIGHT, highlightthickness=1)
        search_wrap.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tk.Label(search_wrap, text="⌕", bg=BG, fg=CYAN, font=("Segoe UI Symbol", 14), padx=8).pack(side="left")
        search = tk.Entry(
            search_wrap,
            textvariable=self.search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            selectbackground=PANEL_3,
            relief="flat",
            bd=0,
            font=FONT,
        )
        search.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)
        search.bind("<KeyRelease>", self._schedule_filter)
        self.item_search_entry = search

        self.station_combo = ttk.Combobox(tools, textvariable=self.station_var, state="readonly", width=22, style="Archive.TCombobox")
        self.station_combo.grid(row=0, column=1, padx=4)
        self.station_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_filters())
        self.sort_combo = ttk.Combobox(
            tools,
            textvariable=self.sort_var,
            state="readonly",
            width=15,
            values=("Name", "Category", "Tech high", "Buy low", "Sell high"),
            style="Archive.TCombobox",
        )
        self.sort_combo.grid(row=0, column=2, padx=(4, 0))
        self.sort_combo.bind("<<ComboboxSelected>>", self._item_sort_combo_selected)
        self._button(tools, "RESET", self._reset_item_filters, PANEL_3, CYAN).grid(row=0, column=3, padx=(8, 0))

        table_wrap = tk.Frame(center, bg=PANEL)
        table_wrap.grid(row=1, column=0, sticky="nsew")
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        columns = tuple(ITEM_COLUMN_SPECS)
        self.item_tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.item_tree.column("#0", width=235, minwidth=160, stretch=False)
        for key, spec in ITEM_COLUMN_SPECS.items():
            self.item_tree.column(
                key,
                width=spec["width"],
                minwidth=spec["minwidth"],
                anchor=spec["anchor"],
                stretch=False,
            )
        self.item_tree.grid(row=0, column=0, sticky="nsew")
        item_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.item_tree.yview, style="Archive.Vertical.TScrollbar")
        item_scroll.grid(row=0, column=1, sticky="ns")
        item_xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.item_tree.xview, style="Archive.Horizontal.TScrollbar")
        item_xscroll.grid(row=1, column=0, sticky="ew")
        self.item_tree.configure(yscrollcommand=item_scroll.set, xscrollcommand=item_xscroll.set)
        self.item_tree.bind("<<TreeviewSelect>>", self._show_selected_item)
        self.item_tree.bind("<Button-3>", self._show_item_column_menu)
        self._restore_item_table_layout()

        result_bar = tk.Frame(center, bg=PANEL_2, padx=10, pady=7)
        result_bar.grid(row=2, column=0, sticky="ew")
        tk.Label(result_bar, textvariable=self.result_var, bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        tk.Label(result_bar, text="Try item:Wasp  category:weapon  damage>=100  station:Dezgard  tag:priority  •  Right-click for columns/actions", bg=PANEL_2, fg="#53708b", font=MONO_SMALL).pack(side="right")

        self.item_detail = tk.Frame(page, bg=PANEL, width=390, highlightbackground=LINE, highlightthickness=1)
        self.item_detail.grid(row=0, column=2, sticky="nse")
        self.item_detail.grid_propagate(False)
        self._build_item_detail()

    def _build_detail_pane(self, parent, *, empty_art: str,
                           empty_title: str, tabbar_text: str,
                           buttons=()) -> dict:
        """The archive's detail pane: art, title, meta, description, body.

        One builder keeps every archive detail panel visually consistent.
        """
        art_wrap = tk.Frame(parent, bg=BG, height=220)
        art_wrap.pack(fill="x")
        art_wrap.pack_propagate(False)
        art = tk.Label(art_wrap, bg=BG, fg=MUTED, text=empty_art, font=("Cascadia Mono", 10, "bold"))
        art.pack(fill="both", expand=True)

        title_wrap = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        title_wrap.pack(fill="x")
        name = tk.Label(title_wrap, text=empty_title, bg=PANEL, fg=TEXT, font=("Segoe UI", 16, "bold"), anchor="w", wraplength=350, justify="left")
        name.pack(fill="x")
        meta = tk.Label(title_wrap, text="", bg=PANEL, fg=CYAN, font=MONO_SMALL, anchor="w")
        meta.pack(fill="x", pady=(4, 0))
        description = tk.Label(title_wrap, text="", bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left", wraplength=350)
        description.pack(fill="x", pady=(8, 0))

        tabbar = tk.Frame(parent, bg=PANEL_2)
        tabbar.pack(fill="x")
        tk.Label(tabbar, text=tabbar_text, bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 8, "bold"), padx=14, pady=9).pack(side="left")
        for label, command, colour in buttons:
            self._button(tabbar, label, command, PANEL_3, colour).pack(
                side="right", padx=7, pady=5)

        text_wrap = tk.Frame(parent, bg=PANEL)
        text_wrap.pack(fill="both", expand=True)
        text = tk.Text(
            text_wrap,
            bg=PANEL,
            fg=TEXT,
            insertbackground=CYAN,
            selectbackground=PANEL_3,
            relief="flat",
            bd=0,
            padx=14,
            pady=10,
            wrap="word",
            font=MONO,
            state="disabled",
        )
        text.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=text.yview, style="Archive.Vertical.TScrollbar")
        detail_scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=detail_scroll.set)
        text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 8, "bold"), spacing1=9, spacing3=4)
        text.tag_configure("label", foreground=MUTED)
        text.tag_configure("value", foreground=TEXT)
        text.tag_configure("price", foreground=AMBER)
        text.tag_configure("station", foreground=MINT)

        return {"art": art, "name": name, "meta": meta,
                "description": description, "text": text}

    def _build_item_detail(self) -> None:
        widgets = self._build_detail_pane(
            self.item_detail,
            empty_art="SELECT AN ITEM", empty_title="Item details",
            tabbar_text="STAT SHEET  /  MARKET OBSERVATIONS",
            buttons=(("ORGANIZE", self._organize_selected_item, MINT),
                     ("SELLERS ON MAP", self._show_selected_item_sellers, CYAN)))
        self.item_art_label = widgets["art"]
        self.item_name_label = widgets["name"]
        self.item_meta_label = widgets["meta"]
        self.item_description_label = widgets["description"]
        self.item_text = widgets["text"]

    def _build_stations_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["stations"] = page

        heading = self._page_heading(
            page,
            "VISITED / OBSERVED STATION ARCHIVE",
            "Search every visited market — try system:Bestla, kind:npc, item:Wasp, priced>=20, tag:trade",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tools = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=12, pady=9)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tk.Label(tools, text="SEARCH", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        station_search = tk.Entry(
            tools,
            textvariable=self.station_search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            width=31,
            font=MONO,
        )
        station_search.pack(side="left", padx=(8, 14), ipady=6)
        station_search.bind("<KeyRelease>", lambda _event: self._populate_stations())
        self.station_search_entry = station_search
        for label, variable, values, width in (
            ("KIND", self.station_kind_var, ("All kinds", "NPC", "Player"), 13),
            ("LOCATION", self.station_location_var, ("All locations", "Mapped", "Unmapped"), 15),
        ):
            tk.Label(tools, text=label, bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left", padx=(0, 6))
            combo = ttk.Combobox(tools, textvariable=variable, values=values, state="readonly", width=width, style="Archive.TCombobox")
            combo.pack(side="left", padx=(0, 14))
            combo.bind("<<ComboboxSelected>>", lambda _event: self._populate_stations())
        self._button(tools, "RESET", self._reset_station_filters, PANEL_3, CYAN).pack(side="left")
        self.station_result_label = tk.Label(tools, text="", bg=PANEL_2, fg=MINT, font=MONO_SMALL)
        self.station_result_label.pack(side="right")

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)

        table_wrap = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        table_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        columns = tuple(STATION_COLUMN_SPECS)
        self.station_tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.station_tree.column("#0", width=235, minwidth=170, stretch=False)
        for key, spec in STATION_COLUMN_SPECS.items():
            self.station_tree.column(
                key,
                width=spec["width"],
                minwidth=spec["minwidth"],
                anchor=spec["anchor"],
                stretch=False,
            )
        self.station_tree.grid(row=0, column=0, sticky="nsew")
        station_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.station_tree.yview, style="Archive.Vertical.TScrollbar")
        station_scroll.grid(row=0, column=1, sticky="ns")
        station_xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.station_tree.xview, style="Archive.Horizontal.TScrollbar")
        station_xscroll.grid(row=1, column=0, sticky="ew")
        self.station_tree.configure(yscrollcommand=station_scroll.set, xscrollcommand=station_xscroll.set)
        self.station_tree.bind("<<TreeviewSelect>>", self._show_selected_station)
        self.station_tree.bind("<Button-3>", self._show_station_context_menu)
        self.station_tree.bind("<Double-1>", lambda _event: self._show_station_on_map())
        self._restore_station_table_layout()

        station_detail = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        station_detail.grid(row=0, column=1, sticky="nsew")
        self.station_name_label = tk.Label(station_detail, text="Select a station", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold"), anchor="w", padx=16, pady=14)
        self.station_name_label.pack(fill="x")
        self.station_summary_label = tk.Label(station_detail, text="", bg=PANEL_2, fg=MUTED, font=MONO_SMALL, anchor="w", padx=16, pady=9)
        self.station_summary_label.pack(fill="x")
        station_item_tools = tk.Frame(station_detail, bg=PANEL_2, padx=12, pady=8)
        station_item_tools.pack(fill="x")
        tk.Label(station_item_tools, text="FIND ITEM", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        station_item_search = tk.Entry(
            station_item_tools,
            textvariable=self.station_item_search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            width=22,
            font=MONO,
        )
        station_item_search.pack(side="left", fill="x", expand=True, padx=(7, 8), ipady=5)
        station_item_search.bind("<KeyRelease>", lambda _event: self._show_selected_station())
        self.station_item_search_entry = station_item_search
        tk.Label(station_item_tools, textvariable=self.station_item_result_var, bg=PANEL_2, fg=MINT, font=MONO_SMALL).pack(side="right")
        station_items_wrap = tk.Frame(station_detail, bg=PANEL)
        station_items_wrap.pack(fill="both", expand=True)
        station_items_wrap.grid_rowconfigure(0, weight=1)
        station_items_wrap.grid_columnconfigure(0, weight=1)
        self.station_item_tree = ttk.Treeview(
            station_items_wrap,
            columns=tuple(STATION_ITEM_COLUMN_SPECS),
            show="tree headings",
            style="Archive.Treeview",
        )
        self.station_item_tree.column("#0", width=190, minwidth=130, stretch=False)
        for key, spec in STATION_ITEM_COLUMN_SPECS.items():
            self.station_item_tree.column(
                key,
                width=spec["width"],
                minwidth=spec["minwidth"],
                anchor=spec["anchor"],
                stretch=False,
            )
        self.station_item_tree.grid(row=0, column=0, sticky="nsew")
        station_item_scroll = ttk.Scrollbar(station_items_wrap, orient="vertical", command=self.station_item_tree.yview, style="Archive.Vertical.TScrollbar")
        station_item_scroll.grid(row=0, column=1, sticky="ns")
        station_item_xscroll = ttk.Scrollbar(station_items_wrap, orient="horizontal", command=self.station_item_tree.xview, style="Archive.Horizontal.TScrollbar")
        station_item_xscroll.grid(row=1, column=0, sticky="ew")
        self.station_item_tree.configure(yscrollcommand=station_item_scroll.set, xscrollcommand=station_item_xscroll.set)
        self.station_item_tree.bind("<Double-1>", self._open_station_item)
        self.station_item_tree.bind("<Button-3>", self._show_station_item_context_menu)
        self._restore_station_item_table_layout()

        footer = tk.Frame(station_detail, bg=PANEL_2, padx=14, pady=9)
        footer.pack(fill="x")
        self.station_map_button = self._button(footer, "SHOW ON MAP", self._show_station_on_map, PANEL_3, CYAN)
        self.station_map_button.pack(side="left")
        self._button(footer, "ORGANIZE", self._organize_selected_station, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        self._button(footer, "COPY INVENTORY", self._copy_station_inventory, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        tk.Label(footer, text="Double-click an item for its full stat sheet", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="right")

    def _build_skill_finder_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["training"] = page

        heading = self._page_heading(
            page,
            "NPC SKILL FINDER",
            "Find trainers and costs — try skill:Skirmisher, system:Bestla, status:ready, sp<=500, tag:next",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tools = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=11, pady=8)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tk.Label(tools, text="SEARCH", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        search = tk.Entry(
            tools,
            textvariable=self.training_search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            width=25,
            font=MONO,
        )
        search.pack(side="left", padx=(7, 10), ipady=6)
        search.bind("<KeyRelease>", lambda _event: self._populate_skill_finder())
        self.training_search_entry = search
        for variable, width in (
            (self.training_system_var, 18),
            (self.training_station_var, 22),
            (self.training_status_var, 17),
        ):
            combo = ttk.Combobox(tools, textvariable=variable, state="readonly", width=width, style="Archive.TCombobox")
            combo.pack(side="left", padx=(0, 7))
            combo.bind("<<ComboboxSelected>>", lambda _event: self._populate_skill_finder())
            if variable is self.training_system_var:
                self.training_system_combo = combo
            elif variable is self.training_station_var:
                self.training_station_combo = combo
            else:
                self.training_status_combo = combo
        self._button(tools, "RESET", self._reset_training_filters, PANEL_3, CYAN).pack(side="left")
        self.training_result_label = tk.Label(tools, text="", bg=PANEL_2, fg=MINT, font=MONO_SMALL)
        self.training_result_label.pack(side="right")

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)

        table_wrap = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        table_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        columns = tuple(TRAINING_COLUMN_SPECS)
        self.training_tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.training_tree.column("#0", width=165, minwidth=120, stretch=False)
        for key, spec in TRAINING_COLUMN_SPECS.items():
            self.training_tree.column(key, width=spec["width"], minwidth=spec["minwidth"], anchor=spec["anchor"], stretch=False)
        self.training_tree.grid(row=0, column=0, sticky="nsew")
        training_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.training_tree.yview, style="Archive.Vertical.TScrollbar")
        training_scroll.grid(row=0, column=1, sticky="ns")
        training_xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.training_tree.xview, style="Archive.Horizontal.TScrollbar")
        training_xscroll.grid(row=1, column=0, sticky="ew")
        self.training_tree.configure(yscrollcommand=training_scroll.set, xscrollcommand=training_xscroll.set)
        self.training_tree.bind("<<TreeviewSelect>>", self._show_selected_training_offer)
        self.training_tree.bind("<Button-3>", self._show_training_context_menu)
        self._restore_training_table_layout()

        detail = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        detail.grid(row=0, column=1, sticky="nsew")
        self.training_name_label = tk.Label(detail, text="Select a training offer", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold"), anchor="w", padx=16, pady=14)
        self.training_name_label.pack(fill="x")
        self.training_summary_label = tk.Label(detail, text="", bg=PANEL_2, fg=MUTED, font=MONO_SMALL, anchor="w", padx=16, pady=9)
        self.training_summary_label.pack(fill="x")
        text_wrap = tk.Frame(detail, bg=PANEL)
        text_wrap.pack(fill="both", expand=True)
        self.training_text = tk.Text(text_wrap, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=14, pady=11, wrap="word", font=MONO, state="disabled")
        self.training_text.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.training_text.yview, style="Archive.Vertical.TScrollbar")
        detail_scroll.pack(side="right", fill="y")
        self.training_text.configure(yscrollcommand=detail_scroll.set)
        self.training_text.tag_configure("label", foreground=MUTED)
        self.training_text.tag_configure("value", foreground=TEXT)
        self.training_text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 8, "bold"), spacing1=9, spacing3=4)
        self.training_text.tag_configure("good", foreground=MINT)
        self.training_text.tag_configure("warning", foreground=AMBER)
        self.training_text.tag_configure("bad", foreground=RED)

        footer = tk.Frame(detail, bg=PANEL_2, padx=14, pady=9)
        footer.pack(fill="x")
        self.training_map_button = self._button(footer, "SHOW TRAINER ON MAP", self._show_training_on_map, PANEL_3, MINT)
        self.training_map_button.pack(side="left")
        self._button(footer, "OPEN STATION", self._open_training_station, PANEL_3, CYAN).pack(side="left", padx=(8, 0))
        self.training_item_button = self._button(footer, "FIND REQUIRED ITEM", self._open_training_required_item, PANEL_3, AMBER)
        self.training_item_button.pack(side="left", padx=(8, 0))
        self._button(footer, "ORGANIZE SKILL", self._organize_selected_training_skill, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        tk.Label(footer, text="Archive only — train inside the game", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="right")

    def _build_map_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["map"] = page

        self.map_stage = tk.Frame(page, bg=MAP_CANVAS_BG)
        self.map_stage.grid(row=0, column=0, sticky="nsew")
        self.map_stage.grid_rowconfigure(0, weight=1)
        self.map_stage.grid_columnconfigure(0, weight=1)

        self.map_canvas = tk.Canvas(self.map_stage, bg=MAP_CANVAS_BG, highlightthickness=0, bd=0, cursor="fleur")
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        self.map_canvas.bind("<Configure>", self._schedule_map_redraw)
        self.map_canvas.bind("<MouseWheel>", self._map_mousewheel)
        self.map_canvas.bind("<ButtonPress-1>", self._map_pan_start)
        self.map_canvas.bind("<B1-Motion>", self._map_pan_move)
        self.map_canvas.bind("<ButtonRelease-1>", self._map_pan_end)

        controls = tk.Frame(self.map_stage, bg=PANEL, highlightbackground=LINE_BRIGHT, highlightthickness=1)
        controls_header = tk.Frame(controls, bg=PANEL_2, padx=9, pady=6)
        controls_header.pack(fill="x")
        controls_handle = tk.Label(controls_header, text="✦ GALAXY INTELLIGENCE MAP", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 8, "bold"), cursor="fleur")
        controls_handle.pack(side="left", padx=(0, 12))
        for caption, command in (("−", lambda: self._change_map_zoom(0.8)), ("+", lambda: self._change_map_zoom(1.25)), ("FIT MAP", self._fit_map_view)):
            tk.Button(controls_header, text=caption, command=command, bg=PANEL, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, relief="flat", bd=0, padx=9, pady=4, cursor="hand2", font=("Cascadia Mono", 7, "bold")).pack(side="left", padx=(0, 4))
        self._checkbutton(controls_header, "SHOW SYSTEM NAMES", self.map_show_names_var, self._draw_map).pack(side="left", padx=(7, 0))
        self._checkbutton(controls_header, "SHOW COALITION CONTROL", self.map_show_coalition_var, self._draw_map).pack(side="left", padx=(7, 0))
        tk.Button(controls_header, text="RESET PANELS", command=self._reset_map_overlays, bg=PANEL, fg=MUTED, activebackground=PANEL_3, activeforeground=CYAN, relief="flat", bd=0, padx=9, pady=4, cursor="hand2", font=("Cascadia Mono", 7, "bold")).pack(side="left", padx=(4, 0))
        self.map_zoom_label = tk.Label(controls_header, text="100%", bg=PANEL_2, fg=MUTED, font=("Cascadia Mono", 7, "bold"))
        self.map_zoom_label.pack(side="right", padx=7)

        metrics = tk.Frame(controls, bg=PANEL, padx=7, pady=6)
        metrics.pack(fill="x")
        self.map_metric_labels: dict[str, tk.Label] = {}
        for key, caption in (("systems", "SYSTEMS"), ("edges", "JUMPS"), ("mapped", "SHOPS"), ("unmapped", "UNKNOWN")):
            card = tk.Frame(metrics, bg=BG, highlightbackground=LINE, highlightthickness=1, padx=9, pady=4)
            card.pack(side="left", fill="x", expand=True, padx=(0, 5))
            value = tk.Label(card, text="-", bg=BG, fg=TEXT, font=("Cascadia Mono", 9, "bold"))
            value.pack(side="left")
            tk.Label(card, text=caption, bg=BG, fg=MUTED, font=("Cascadia Mono", 6, "bold")).pack(side="left", padx=(5, 0))
            self.map_metric_labels[key] = value

        search_panel = tk.Frame(self.map_stage, bg=PANEL, highlightbackground=LINE_BRIGHT, highlightthickness=1)
        search_panel.grid_rowconfigure(3, weight=1)
        search_panel.grid_columnconfigure(0, weight=1)
        search_handle = tk.Label(search_panel, text="☷ SEARCH GALAXY INTEL  •  DRAG", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 8, "bold"), padx=11, pady=9, anchor="w", cursor="fleur")
        search_handle.grid(row=0, column=0, columnspan=2, sticky="ew")
        search_wrap = tk.Frame(search_panel, bg=BG, highlightbackground=LINE_BRIGHT, highlightthickness=1)
        search_wrap.grid(row=1, column=0, columnspan=2, sticky="ew", padx=9, pady=(9, 6))
        tk.Label(search_wrap, text="⌕", bg=BG, fg=CYAN, font=("Segoe UI Symbol", 13), padx=7).pack(side="left")
        self.map_search_entry = tk.Entry(search_wrap, textvariable=self.map_search_var, bg=BG, fg=TEXT, insertbackground=CYAN, selectbackground=PANEL_3, relief="flat", bd=0, font=FONT)
        self.map_search_entry.pack(side="left", fill="both", expand=True, padx=(0, 7), pady=7)
        self.map_search_entry.bind("<KeyRelease>", self._apply_map_search)
        self.map_mode_combo = ttk.Combobox(
            search_panel,
            textvariable=self.map_mode_var,
            state="readonly",
            values=("Everything", "Systems", "Stations", "Shop items", "Planets", "My favourites"),
            style="Archive.TCombobox",
        )
        self.map_mode_combo.grid(row=2, column=0, columnspan=2, sticky="ew", padx=9, pady=(0, 7))
        self.map_mode_combo.bind("<<ComboboxSelected>>", self._apply_map_search)

        self.map_result_tree = ttk.Treeview(search_panel, columns=tuple(MAP_RESULT_COLUMN_SPECS), show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.map_result_tree.column("#0", width=180, minwidth=125, stretch=False)
        for key, spec in MAP_RESULT_COLUMN_SPECS.items():
            self.map_result_tree.column(key, width=spec["width"], minwidth=spec["minwidth"], anchor=spec["anchor"], stretch=False)
        self.map_result_tree.grid(row=3, column=0, sticky="nsew")
        result_scroll = ttk.Scrollbar(search_panel, orient="vertical", command=self.map_result_tree.yview, style="Archive.Vertical.TScrollbar")
        result_scroll.grid(row=3, column=1, sticky="ns")
        map_result_xscroll = ttk.Scrollbar(search_panel, orient="horizontal", command=self.map_result_tree.xview, style="Archive.Horizontal.TScrollbar")
        map_result_xscroll.grid(row=4, column=0, sticky="ew")
        self.map_result_tree.configure(yscrollcommand=result_scroll.set, xscrollcommand=map_result_xscroll.set)
        self.map_result_tree.bind("<<TreeviewSelect>>", self._show_selected_map_result)
        self.map_result_tree.bind("<Button-3>", self._show_map_result_context_menu)
        self._restore_map_result_layout()
        tk.Label(search_panel, textvariable=self.map_result_var, bg=PANEL_2, fg=MUTED, font=("Cascadia Mono", 7), padx=9, pady=7, anchor="w").grid(row=5, column=0, columnspan=2, sticky="ew")

        detail_panel = tk.Frame(self.map_stage, bg=PANEL, highlightbackground=LINE_BRIGHT, highlightthickness=1)
        detail_handle = tk.Label(detail_panel, text="☷ MAP INTELLIGENCE  •  DRAG", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 8, "bold"), padx=11, pady=9, anchor="w", cursor="fleur")
        detail_handle.pack(fill="x")
        map_detail_actions = tk.Frame(detail_panel, bg=PANEL_2, padx=8, pady=7)
        map_detail_actions.pack(side="bottom", fill="x")
        self._button(map_detail_actions, "OPEN RECORD", self._open_selected_map_record, PANEL_3, CYAN).pack(side="left")
        self._button(map_detail_actions, "ORGANIZE", self._organize_selected_map_record, PANEL_3, MINT).pack(side="left", padx=(7, 0))
        self._button(map_detail_actions, "COPY", self._copy_selected_map_result, PANEL_3, AMBER).pack(side="left", padx=(7, 0))
        detail_wrap = tk.Frame(detail_panel, bg=PANEL)
        detail_wrap.pack(fill="both", expand=True)
        self.map_detail_text = tk.Text(detail_wrap, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=12, pady=10, wrap="word", font=MONO_SMALL, state="disabled")
        self.map_detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_wrap, orient="vertical", command=self.map_detail_text.yview, style="Archive.Vertical.TScrollbar")
        detail_scroll.pack(side="right", fill="y")
        self.map_detail_text.configure(yscrollcommand=detail_scroll.set)
        self.map_detail_text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 8, "bold"), spacing1=8, spacing3=4)
        self.map_detail_text.tag_configure("label", foreground=MUTED)
        self.map_detail_text.tag_configure("value", foreground=TEXT)
        self.map_detail_text.tag_configure("good", foreground=MINT)
        self.map_detail_text.tag_configure("warning", foreground=AMBER)
        self.map_detail_text.tag_configure("bad", foreground=RED)
        detail_resize_grip = tk.Label(
            detail_panel,
            text="◢",
            bg=PANEL_2,
            fg=MUTED,
            font=("Cascadia Mono", 9, "bold"),
            cursor="size_nw_se",
        )
        detail_resize_grip.place(relx=1.0, rely=1.0, anchor="se", width=18, height=18)

        legend = tk.Frame(self.map_stage, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=8, pady=5)
        legend_handle = tk.Label(legend, text="☷", bg=PANEL_2, fg=MUTED, font=("Cascadia Mono", 7, "bold"), cursor="fleur")
        legend_handle.pack(side="left", padx=(0, 8))
        for colour, caption in ((CYAN, "SHOP / STATION"), (MAP_TERRITORY_LEGEND, "COALITION CONTROL"), (AMBER, "SEARCH MATCH"), (MINT, "SELECTED"), (RED, "HAZARD 10 / DANGER")):
            tk.Label(legend, text=f"● {caption}", bg=PANEL_2, fg=colour, font=("Cascadia Mono", 6, "bold")).pack(side="left", padx=(0, 10))

        self.map_overlay_frames = {"controls": controls, "search": search_panel, "detail": detail_panel, "legend": legend}
        self.map_overlay_positions: dict[str, tuple[int, int]] = {}
        self.map_overlay_sizes: dict[str, tuple[int, int]] = {}
        self.map_overlay_drag: tuple[str, int, int, int, int] | None = None
        self.map_overlay_resize: tuple[str, int, int, int, int, int, int] | None = None
        self._register_map_overlay("controls", controls_handle)
        self._register_map_overlay("search", search_handle)
        self._register_map_overlay("detail", detail_handle)
        self._register_map_overlay("legend", legend_handle)
        self._register_map_overlay_resize("detail", detail_resize_grip)
        controls.place(x=0, y=0)
        search_panel.place(x=0, y=0, width=330, height=400)
        detail_panel.place(x=0, y=0, width=330, height=400)
        legend.place(x=0, y=0)
        self.map_stage.bind("<Configure>", self._layout_map_overlays, add="+")
        self._restore_map_view()
        self.root.after_idle(self._layout_map_overlays)

    def _register_map_overlay(self, key: str, handle: tk.Widget) -> None:
        handle.bind("<ButtonPress-1>", lambda event, panel=key: self._map_overlay_drag_start(event, panel))
        handle.bind("<B1-Motion>", self._map_overlay_drag_move)
        handle.bind("<ButtonRelease-1>", self._map_overlay_drag_end)

    def _layout_map_overlays(self, _event=None) -> None:
        if not hasattr(self, "map_stage"):
            return
        width = self.map_stage.winfo_width()
        height = self.map_stage.winfo_height()
        if width < 300 or height < 240:
            return
        controls = self.map_overlay_frames["controls"]
        legend = self.map_overlay_frames["legend"]
        controls.update_idletasks()
        legend.update_idletasks()
        controls_width = min(width - 24, max(650, controls.winfo_reqwidth()))
        controls_height = controls.winfo_reqheight()
        panel_width = min(340, max(285, (width - 48) // 3))
        panel_height = max(300, min(480, height - controls_height - 42))
        saved_detail_width, saved_detail_height = self.map_overlay_sizes.get(
            "detail", (panel_width, panel_height)
        )
        detail_width = min(max(285, saved_detail_width), max(285, width - 12))
        detail_height = min(max(240, saved_detail_height), max(240, height - 12))
        legend_width = legend.winfo_reqwidth()
        legend_height = legend.winfo_reqheight()
        sizes = {
            "controls": (controls_width, controls_height),
            "search": (panel_width, panel_height),
            "detail": (detail_width, detail_height),
            "legend": (legend_width, legend_height),
        }
        panel_y = controls_height + 24
        defaults = {
            "controls": ((width - controls_width) // 2, 12),
            "search": (12, panel_y),
            "detail": (width - panel_width - 12, panel_y),
            "legend": ((width - legend_width) // 2, height - legend_height - 12),
        }
        for key, frame in self.map_overlay_frames.items():
            frame_width, frame_height = sizes[key]
            desired_x, desired_y = self.map_overlay_positions.get(key, defaults[key])
            x = min(max(6, desired_x), max(6, width - frame_width - 6))
            y = min(max(6, desired_y), max(6, height - frame_height - 6))
            frame.place_configure(x=x, y=y, width=frame_width, height=frame_height)
            frame.lift()

    def _reset_map_overlays(self) -> None:
        self.map_overlay_positions.clear()
        self.map_overlay_sizes.clear()
        self._layout_map_overlays()

    def _map_overlay_drag_start(self, event, key: str) -> str:
        frame = self.map_overlay_frames[key]
        frame.lift()
        self.map_overlay_drag = (key, event.x_root, event.y_root, frame.winfo_x(), frame.winfo_y())
        return "break"

    def _map_overlay_drag_move(self, event) -> str:
        if not self.map_overlay_drag:
            return "break"
        key, start_x, start_y, frame_x, frame_y = self.map_overlay_drag
        frame = self.map_overlay_frames[key]
        stage_width = self.map_stage.winfo_width()
        stage_height = self.map_stage.winfo_height()
        x = min(max(6, frame_x + event.x_root - start_x), max(6, stage_width - frame.winfo_width() - 6))
        y = min(max(6, frame_y + event.y_root - start_y), max(6, stage_height - frame.winfo_height() - 6))
        frame.place_configure(x=x, y=y)
        self.map_overlay_positions[key] = (x, y)
        return "break"

    def _map_overlay_drag_end(self, _event=None) -> str:
        self.map_overlay_drag = None
        return "break"

    def _register_map_overlay_resize(self, key: str, grip: tk.Widget) -> None:
        grip.bind("<ButtonPress-1>", lambda event, panel=key: self._map_overlay_resize_start(event, panel))
        grip.bind("<B1-Motion>", self._map_overlay_resize_move)
        grip.bind("<ButtonRelease-1>", self._map_overlay_resize_end)

    def _map_overlay_resize_start(self, event, key: str) -> str:
        frame = self.map_overlay_frames[key]
        frame.lift()
        self.map_overlay_resize = (
            key,
            event.x_root,
            event.y_root,
            frame.winfo_x(),
            frame.winfo_y(),
            frame.winfo_width(),
            frame.winfo_height(),
        )
        return "break"

    def _map_overlay_resize_move(self, event) -> str:
        if not self.map_overlay_resize:
            return "break"
        key, start_x, start_y, frame_x, frame_y, frame_width, frame_height = self.map_overlay_resize
        frame = self.map_overlay_frames[key]
        maximum_width = max(285, self.map_stage.winfo_width() - frame_x - 6)
        maximum_height = max(240, self.map_stage.winfo_height() - frame_y - 6)
        width = min(max(285, frame_width + event.x_root - start_x), maximum_width)
        height = min(max(240, frame_height + event.y_root - start_y), maximum_height)
        frame.place_configure(width=width, height=height)
        self.map_overlay_sizes[key] = (width, height)
        return "break"

    def _map_overlay_resize_end(self, _event=None) -> str:
        self.map_overlay_resize = None
        return "break"

    def _restore_map_view(self) -> None:
        view = self.user_state.map_view()
        self.map_zoom = min(MAP_ZOOM_MAX, max(MAP_ZOOM_MIN, float(view.get("zoom") or 1.0)))
        self.map_pan_x = float(view.get("panX") or 0.0)
        self.map_pan_y = float(view.get("panY") or 0.0)
        self.map_show_names_var.set(bool(view.get("showNames", True)))
        self.map_show_coalition_var.set(bool(view.get("showCoalitionControl", True)))
        self.map_selected_system = str(view.get("selectedSystem") or "") or None
        valid_modes = {"Everything", "Systems", "Stations", "Shop items", "Planets", "My favourites"}
        mode = str(view.get("mode") or "Everything")
        self.map_mode_var.set(mode if mode in valid_modes else "Everything")
        positions = view.get("overlayPositions") if isinstance(view.get("overlayPositions"), dict) else {}
        self.map_overlay_positions = {
            key: (int(pair[0]), int(pair[1]))
            for key, pair in positions.items()
            if key in self.map_overlay_frames and isinstance(pair, (list, tuple)) and len(pair) >= 2
        }
        sizes = view.get("overlaySizes") if isinstance(view.get("overlaySizes"), dict) else {}
        self.map_overlay_sizes = {
            "detail": (int(pair[0]), int(pair[1]))
            for key, pair in sizes.items()
            if key == "detail" and isinstance(pair, (list, tuple)) and len(pair) >= 2
        }

    def _save_map_view(self) -> None:
        self.user_state.set_map_view(
            zoom=self.map_zoom,
            pan_x=self.map_pan_x,
            pan_y=self.map_pan_y,
            show_names=self.map_show_names_var.get(),
            show_coalition_control=self.map_show_coalition_var.get(),
            selected_system=self.map_selected_system or "",
            mode=self.map_mode_var.get(),
            overlay_positions=self.map_overlay_positions,
            overlay_sizes=self.map_overlay_sizes,
        )

    def _restore_map_result_layout(self) -> None:
        saved = self.user_state.load().get("tableLayouts", {})
        has_layout = isinstance(saved, dict) and "map_results" in saved
        layout = self.user_state.table_layout("map_results")
        if has_layout:
            columns = [column for column in layout.get("columns", []) if column in MAP_RESULT_COLUMN_SPECS]
            if columns:
                self.map_result_display_order = columns
                selected = set(columns)
                for key, variable in self.map_result_column_vars.items():
                    variable.set(key in selected)
            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.map_result_tree.column("#0", width=widths["name"])
                for key in MAP_RESULT_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.map_result_tree.column(key, width=widths[key])
            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in MAP_RESULT_COLUMN_SPECS:
                self.map_result_sort_column = sort_column
                self.map_result_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.map_result_pending_xview = xview
        self._refresh_map_result_columns()
        self._refresh_map_result_headings()

    def _save_map_result_layout(self) -> None:
        if not hasattr(self, "map_result_tree") or not self.map_result_tree.winfo_exists():
            return
        displayed = list(self.map_result_tree.tk.splitlist(self.map_result_tree.cget("displaycolumns")))
        widths = {"name": int(self.map_result_tree.column("#0", "width"))}
        widths.update({key: int(self.map_result_tree.column(key, "width")) for key in MAP_RESULT_COLUMN_SPECS})
        xview = self.map_result_tree.xview()
        self.user_state.set_table_layout(
            "map_results",
            columns=displayed,
            widths=widths,
            sort_column=self.map_result_sort_column,
            sort_descending=self.map_result_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_map_result_columns(self) -> None:
        visible = [key for key in self.map_result_display_order if key in MAP_RESULT_COLUMN_SPECS and self.map_result_column_vars[key].get()]
        visible.extend(key for key in MAP_RESULT_COLUMN_SPECS if self.map_result_column_vars[key].get() and key not in visible)
        self.map_result_display_order = visible
        self.map_result_tree.configure(displaycolumns=visible)

    def _apply_map_result_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.map_result_display_order = [column for column in columns if column in MAP_RESULT_COLUMN_SPECS]
        for key, variable in self.map_result_column_vars.items():
            variable.set(key in selected)
        self._refresh_map_result_columns()

    def _refresh_map_result_headings(self) -> None:
        arrow = " ▼" if self.map_result_sort_desc else " ▲"
        self.map_result_tree.heading("#0", text="MATCH" + (arrow if self.map_result_sort_column == "name" else ""), anchor="w", command=lambda: self._sort_map_results_by("name"))
        for key, spec in MAP_RESULT_COLUMN_SPECS.items():
            self.map_result_tree.heading(key, text=spec["label"] + (arrow if self.map_result_sort_column == key else ""), anchor=spec["anchor"], command=lambda column=key: self._sort_map_results_by(column))

    def _sort_map_results_by(self, column: str) -> None:
        if self.map_result_sort_column == column:
            self.map_result_sort_desc = not self.map_result_sort_desc
        else:
            self.map_result_sort_column = column
            self.map_result_sort_desc = bool(MAP_RESULT_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_map_result_headings()
        self._apply_map_search()

    def _sort_map_result_rows(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sortable: list[tuple[Any, str, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for result in results:
            value = map_result_column_sort_value(result, self.map_result_sort_column)
            if value is None:
                missing.append(result)
            else:
                sortable.append((value, str(result.get("name") or "").casefold(), result))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.map_result_sort_desc)
        missing.sort(key=lambda result: str(result.get("name") or "").casefold())
        return [row[2] for row in sortable] + missing

    def _show_map_result_column_menu(self, event) -> str:
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, selectcolor=CYAN)
        for label, columns in MAP_RESULT_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_map_result_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_map_result_column_preset(tuple(MAP_RESULT_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in MAP_RESULT_COLUMN_SPECS.items():
            menu.add_checkbutton(label=spec["label"].title(), variable=self.map_result_column_vars[key], command=self._refresh_map_result_columns)
        self.map_result_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _build_coverage_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["coverage"] = page

        heading = self._page_heading(
            page,
            "EXPLORATION COVERAGE",
            "Systems known to hold shops whose contents have not yet been observed",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tools = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE,
                         highlightthickness=1, padx=11, pady=8)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tools.grid_columnconfigure(1, weight=1)
        tk.Label(tools, text="SEARCH", bg=PANEL_2, fg=MUTED,
                 font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        tk.Entry(tools, textvariable=self.coverage_search_var, bg=BG, fg=TEXT,
                 insertbackground=CYAN, relief="flat", bd=0, width=28,
                 font=MONO).grid(row=0, column=1, sticky="ew", padx=(7, 10), ipady=6)
        self.coverage_search_var.trace_add(
            "write", lambda *_a: self._populate_coverage())
        tk.Label(tools, text="FROM", bg=PANEL_2, fg=MUTED,
                 font=MONO_SMALL).grid(row=0, column=2, sticky="e", padx=(10, 0))
        # Hops are measured through the real warp graph, which only became
        # usable when a shared team map arrived.
        tk.Entry(tools, textvariable=self.coverage_origin_var, bg=BG, fg=TEXT,
                 insertbackground=CYAN, relief="flat", bd=0, width=22,
                 font=MONO).grid(row=0, column=3, sticky="e", padx=(7, 10), ipady=6)
        self.coverage_origin_var.trace_add(
            "write", lambda *_a: self._populate_coverage())
        ttk.Combobox(
            tools, textvariable=self.coverage_scope_var, state="readonly",
            values=["Reachable only", "Everything", "Shops"],
            style="Archive.TCombobox", width=16,
        ).grid(row=0, column=4, sticky="e", ipady=2)
        self.coverage_scope_var.trace_add(
            "write", lambda *_a: self._populate_coverage())
        tk.Label(tools, textvariable=self.coverage_result_var, bg=PANEL_2,
                 fg=MUTED, font=MONO_SMALL, anchor="e").grid(
            row=1, column=0, columnspan=5, sticky="e", pady=(7, 0))

        wrap = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.coverage_tree = ttk.Treeview(
            wrap, columns=COVERAGE_COLUMNS, show="tree headings",
            style="Archive.Treeview", selectmode="browse")
        self.coverage_tree.column("#0", width=210, minwidth=150, stretch=False)
        self.coverage_tree.heading("#0", text="SYSTEM",
                                   command=lambda: self._sort_coverage("system"))
        for key, spec in COVERAGE_COLUMN_SPECS.items():
            self.coverage_tree.column(
                key, width=spec["width"], minwidth=spec["minwidth"],
                anchor=spec["anchor"], stretch=False)
            self.coverage_tree.heading(
                key, text=spec["label"],
                command=lambda column=key: self._sort_coverage(column))
        self.coverage_tree.tag_configure("blocked", foreground=MUTED)
        self.coverage_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(wrap, orient="vertical",
                                command=self.coverage_tree.yview,
                                style="Archive.Vertical.TScrollbar")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient="horizontal",
                                command=self.coverage_tree.xview,
                                style="Archive.Horizontal.TScrollbar")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.coverage_tree.configure(yscrollcommand=yscroll.set,
                                     xscrollcommand=xscroll.set)
        self.coverage_tree.bind(
            "<Double-1>", lambda _e: self._show_coverage_on_map())

    def _sort_coverage(self, column: str) -> None:
        if self.coverage_sort_column == column:
            self.coverage_sort_desc = not self.coverage_sort_desc
        else:
            self.coverage_sort_column = column
            spec = COVERAGE_COLUMN_SPECS.get(column, {})
            self.coverage_sort_desc = bool(spec.get("first_desc"))
        self._populate_coverage()

    def _show_coverage_on_map(self) -> None:
        selection = self.coverage_tree.selection()
        if not selection:
            return
        name = self.coverage_tree.item(selection[0], "text")
        if name:
            self.show_page("map")
            self._focus_map_system(name)

    def _coverage_default_origin(self) -> str:
        """The system of the most recently seen station.

        A sensible starting point for hop counts without asking, and the player
        can type any other system over it.
        """
        best_seen, best_system = "", ""
        for station in self.data.get("stations") or []:
            seen = str(station.get("lastSeen") or "")
            system = str(station.get("systemName") or "")
            if system and seen > best_seen:
                best_seen, best_system = seen, system
        return best_system

    def _populate_coverage(self) -> None:
        tree = getattr(self, "coverage_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        origin = str(self.coverage_origin_var.get() or "").strip()
        rows = app.coverage_targets(
            self.data.get("map") or {}, self.data.get("stations"), origin=origin)

        scope = str(self.coverage_scope_var.get() or "Reachable only")
        if scope == "Reachable only":
            rows = [row for row in rows if row["reachable"]]
        elif scope == "Shops":
            rows = [row for row in rows if row["unseenShops"]]

        needle = str(self.coverage_search_var.get() or "").strip().casefold()
        if needle:
            rows = [row for row in rows if needle in row["system"].casefold()]

        column = self.coverage_sort_column
        spec = COVERAGE_COLUMN_SPECS.get(column, {})
        if column == "system":
            rows.sort(key=lambda row: row["system"].casefold(),
                      reverse=self.coverage_sort_desc)
        elif column == "status":
            rows.sort(key=lambda row: (row["reachable"], row["system"].casefold()),
                      reverse=self.coverage_sort_desc)
        elif spec.get("numeric"):
            # Unknown sorts last either way: an unreachable system is not the
            # closest one just because its distance is unknown.
            rows.sort(key=lambda row: (row.get(column) is None,
                                       row.get(column) or 0,
                                       row["system"].casefold()),
                      reverse=self.coverage_sort_desc)

        for row in rows:
            if not row["hazardKnown"]:
                status = "danger unknown"
            elif not row["reachable"]:
                status = f"over the {app.DEFAULT_HAZARD_LIMIT} limit"
            elif row["hops"] is None:
                status = "no known route"
            else:
                status = "explored" if row["explored"] else "never visited"
            tree.insert(
                "", "end", text=row["system"],
                values=(
                    "" if row["hops"] is None else row["hops"],
                    "" if row["hazard"] is None else row["hazard"],
                    row["unseenShops"] or "",
                    row["npcStations"] or "",
                    status,
                ),
                tags=() if row["reachable"] else ("blocked",))

        shops = sum(row["unseenShops"] for row in rows)
        self.coverage_result_var.set(
            f"{len(rows):,} systems  -  {shops:,} shops not yet observed"
            "  -  double-click for the map")

    def _build_system_yields_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["system_yields"] = page

        heading = self._page_heading(
            page,
            "SYSTEM RESOURCE YIELDS / EXTRACTION CAPACITY",
            "Each value is one-base yield (moon-aware maximum): planets ×3, moons ×1",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tools = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE,
                         highlightthickness=1, padx=11, pady=8)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tools.grid_columnconfigure(1, weight=1)
        tk.Label(tools, text="SEARCH", bg=PANEL_2, fg=MUTED,
                 font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        tk.Entry(
            tools, textvariable=self.system_yield_search_var, bg=BG, fg=TEXT,
            insertbackground=CYAN, relief="flat", bd=0, width=36, font=MONO,
        ).grid(row=0, column=1, sticky="ew", padx=(7, 10), ipady=6)
        self.system_yield_search_var.trace_add(
            "write", lambda *_a: self._populate_system_yields())
        tk.Label(tools, text="ORDER BY", bg=PANEL_2, fg=MUTED,
                 font=MONO_SMALL).grid(row=0, column=2, sticky="e", padx=(10, 0))
        order_values = ["Total yield"] + [
            name.replace("_", " ").title() for name in SCAN_RESOURCE_KEYS]
        ttk.Combobox(
            tools, textvariable=self.system_yield_order_var, state="readonly",
            values=order_values, style="Archive.TCombobox", width=16,
        ).grid(row=0, column=3, sticky="e", padx=(7, 0), ipady=2)
        self.system_yield_order_var.trace_add(
            "write", lambda *_a: self._populate_system_yields())
        tk.Label(tools, textvariable=self.system_yield_result_var, bg=PANEL_2,
                 fg=MUTED, font=MONO_SMALL, anchor="e").grid(
            row=1, column=0, columnspan=4, sticky="e", pady=(7, 0))

        wrap = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.system_yield_tree = ttk.Treeview(
            wrap, columns=SYSTEM_YIELD_COLUMNS, show="tree headings",
            style="Archive.Treeview", selectmode="browse")
        self.system_yield_tree.column("#0", width=190, minwidth=140, stretch=False)
        self.system_yield_tree.heading("#0", text="SYSTEM",
                                       command=lambda: self._sort_system_yields("system"))
        for key, spec in SYSTEM_YIELD_COLUMN_SPECS.items():
            self.system_yield_tree.column(
                key, width=spec["width"], minwidth=spec["minwidth"],
                anchor=spec["anchor"], stretch=False)
            self.system_yield_tree.heading(
                key, text=spec["label"],
                command=lambda column=key: self._sort_system_yields(column))
        self.system_yield_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(wrap, orient="vertical",
                                command=self.system_yield_tree.yview,
                                style="Archive.Vertical.TScrollbar")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient="horizontal",
                                command=self.system_yield_tree.xview,
                                style="Archive.Horizontal.TScrollbar")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.system_yield_tree.configure(yscrollcommand=yscroll.set,
                                         xscrollcommand=xscroll.set)
        self.system_yield_tree.bind(
            "<Double-1>", lambda _e: self._show_system_yield_on_map())

    def _sort_system_yields(self, column: str) -> None:
        if self.system_yield_sort_column == column:
            self.system_yield_sort_desc = not self.system_yield_sort_desc
        else:
            self.system_yield_sort_column = column
            spec = SYSTEM_YIELD_COLUMN_SPECS.get(column, {})
            self.system_yield_sort_desc = bool(spec.get("first_desc"))
        self._populate_system_yields()

    def _show_system_yield_on_map(self) -> None:
        selection = self.system_yield_tree.selection()
        if not selection:
            return
        name = self.system_yield_tree.item(selection[0], "text")
        if name:
            self.show_page("map")
            self._focus_map_system(name)

    def _populate_system_yields(self) -> None:
        tree = getattr(self, "system_yield_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        rows = system_resource_totals(self.data.get("scans") or [])
        needle = str(self.system_yield_search_var.get() or "").strip().casefold()
        if needle:
            rows = [row for row in rows if needle in row["system"].casefold()]

        # "ORDER BY" also hides systems with none of the chosen resource, which
        # a column sort alone cannot do.
        choice = str(self.system_yield_order_var.get() or "Total yield")
        order_key = "total"
        for name in SCAN_RESOURCE_KEYS:
            if choice.casefold() == name.replace("_", " ").title().casefold():
                order_key = f"{SCAN_RESOURCE_COLUMN_PREFIX}{name}"
                break
        if order_key != "total":
            rows = [row for row in rows if float(row.get(order_key) or 0.0) > 0]
            self.system_yield_sort_column = order_key
            self.system_yield_sort_desc = True

        column = self.system_yield_sort_column
        if column == "system":
            rows.sort(key=lambda row: row["system"].casefold(),
                      reverse=self.system_yield_sort_desc)
        else:
            maximum_column = (
                "maxTotal" if column == "total"
                else "planets" if column == "planets"
                else f"max_{column}"
            )
            rows.sort(key=lambda row: (float(row.get(maximum_column) or 0.0),
                                       row["system"].casefold()),
                      reverse=self.system_yield_sort_desc)

        for row in rows:
            values = [system_yield_display_value(row, key) for key in SYSTEM_YIELD_COLUMNS]
            tree.insert("", "end", text=row["system"], values=values)
        total_systems = len(rows)
        suffix = "" if order_key == "total" else f" yielding {choice}"
        self.system_yield_result_var.set(
            f"{total_systems:,} system{'' if total_systems == 1 else 's'}{suffix}"
            f" • click a column to sort • double-click a row for the map")

    def _build_scans_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["scans"] = page

        heading = self._page_heading(
            page,
            "PLANETARY SCAN ARCHIVE",
            "Compare colony quality and resources, track your bases, and jump directly to each system",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tools = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=11, pady=8)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tools.grid_columnconfigure(1, weight=1)
        tk.Label(tools, text="SEARCH", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        scan_search = tk.Entry(
            tools,
            textvariable=self.scan_search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            width=42,
            font=MONO,
        )
        scan_search.grid(row=0, column=1, columnspan=4, sticky="ew", padx=(7, 10), ipady=6)
        scan_search.bind("<KeyRelease>", lambda _event: self._populate_scans())
        self.scan_search_entry = scan_search
        self.scan_result_label = tk.Label(tools, text="", bg=PANEL_2, fg=MINT, font=MONO_SMALL)
        self.scan_result_label.grid(row=0, column=5, sticky="e", padx=(8, 0))
        for index, (variable, width) in enumerate((
            (self.scan_system_filter_var, 19),
            (self.scan_type_filter_var, 13),
            (self.scan_quality_filter_var, 18),
            (self.scan_base_filter_var, 17),
        )):
            combo = ttk.Combobox(tools, textvariable=variable, state="readonly", width=width, style="Archive.TCombobox")
            combo.grid(row=1, column=index, sticky="w", padx=(0, 7), pady=(8, 0))
            combo.bind("<<ComboboxSelected>>", lambda _event: self._populate_scans())
            if variable is self.scan_system_filter_var:
                self.scan_system_filter_combo = combo
            elif variable is self.scan_type_filter_var:
                self.scan_type_filter_combo = combo
            elif variable is self.scan_quality_filter_var:
                self.scan_quality_filter_combo = combo
            else:
                self.scan_base_filter_combo = combo
        self._button(tools, "RESET FILTERS", self._reset_scan_filters, PANEL_3, CYAN).grid(row=1, column=4, sticky="w", pady=(8, 0))
        tk.Label(
            tools,
            text='Examples: resource:gold  score>=75  atmosphere:breathable  base:yes',
            bg=PANEL_2,
            fg=MUTED,
            font=MONO_SMALL,
            anchor="e",
        ).grid(row=1, column=5, sticky="e", padx=(10, 0), pady=(8, 0))

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)

        table_wrap = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        table_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        scan_columns = tuple(SCAN_COLUMN_SPECS)
        self.scan_tree = ttk.Treeview(table_wrap, columns=scan_columns, show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.scan_tree.column("#0", width=155, minwidth=115, stretch=False)
        for key, spec in SCAN_COLUMN_SPECS.items():
            self.scan_tree.column(
                key,
                width=spec["width"],
                minwidth=spec["minwidth"],
                anchor=spec["anchor"],
                stretch=False,
            )
        self.scan_tree.grid(row=0, column=0, sticky="nsew")
        scan_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.scan_tree.yview, style="Archive.Vertical.TScrollbar")
        scan_scroll.grid(row=0, column=1, sticky="ns")
        scan_xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.scan_tree.xview, style="Archive.Horizontal.TScrollbar")
        scan_xscroll.grid(row=1, column=0, sticky="ew")
        self.scan_tree.configure(yscrollcommand=scan_scroll.set, xscrollcommand=scan_xscroll.set)
        self.scan_tree.bind("<<TreeviewSelect>>", self._show_selected_scan)
        self.scan_tree.bind("<Button-1>", self._scan_tree_click, add="+")
        self.scan_tree.bind("<Button-3>", self._show_scan_context_menu)
        self.scan_tree.bind("<Double-1>", lambda _event: self._show_scan_on_map())
        self._restore_scan_table_layout()

        detail = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        detail.grid(row=0, column=1, sticky="nsew")
        scan_art_wrap = tk.Frame(detail, bg=BG, height=205)
        scan_art_wrap.pack(fill="x")
        scan_art_wrap.pack_propagate(False)
        self.scan_art_label = tk.Label(scan_art_wrap, text="SELECT A PLANET", bg=BG, fg=MUTED, font=("Cascadia Mono", 10, "bold"))
        self.scan_art_label.pack(fill="both", expand=True)
        self.scan_name_label = tk.Label(detail, text="Planet scan details", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold"), anchor="w", padx=16, pady=13)
        self.scan_name_label.pack(fill="x")

        annotation = tk.Frame(detail, bg=PANEL_2, padx=13, pady=9)
        annotation.pack(fill="x")
        tk.Label(annotation, text="SYSTEM", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        self.scan_system_combo = ttk.Combobox(
            annotation,
            textvariable=self.scan_system_var,
            state="normal",
            width=24,
            style="Archive.TCombobox",
        )
        self.scan_system_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(3, 0))
        self._checkbutton(annotation, "HAS BASE", self.scan_has_base_var, self._sync_scan_base_controls).grid(row=1, column=1, sticky="w", padx=(0, 8))
        tk.Label(annotation, text="COUNT", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).grid(row=0, column=2, sticky="w")
        self.scan_base_count_spin = tk.Spinbox(
            annotation,
            from_=0,
            to=999,
            textvariable=self.scan_base_count_var,
            width=5,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            buttonbackground=PANEL_3,
            relief="flat",
            font=MONO,
        )
        self.scan_base_count_spin.grid(row=1, column=2, sticky="w", pady=(3, 0))
        annotation.grid_columnconfigure(0, weight=1)

        annotation_actions = tk.Frame(detail, bg=PANEL_2, padx=13, pady=9)
        annotation_actions.pack(fill="x")
        self._button(annotation_actions, "SAVE LOCATION / BASE", self._save_scan_annotation, CYAN, "#03131b").pack(side="left")
        self._button(annotation_actions, "SHOW ON MAP", self._show_scan_on_map, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        self._button(annotation_actions, "ORGANIZE", self._organize_selected_scan, PANEL_3, MINT).pack(side="left", padx=(8, 0))

        scan_text_wrap = tk.Frame(detail, bg=PANEL)
        scan_text_wrap.pack(fill="both", expand=True)
        self.scan_text = tk.Text(scan_text_wrap, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=14, pady=10, wrap="word", font=MONO, state="disabled")
        self.scan_text.pack(side="left", fill="both", expand=True)
        scan_detail_scroll = ttk.Scrollbar(scan_text_wrap, orient="vertical", command=self.scan_text.yview, style="Archive.Vertical.TScrollbar")
        scan_detail_scroll.pack(side="right", fill="y")
        self.scan_text.configure(yscrollcommand=scan_detail_scroll.set)
        self.scan_text.tag_configure("label", foreground=MUTED)
        self.scan_text.tag_configure("value", foreground=TEXT)
        self.scan_text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 8, "bold"), spacing1=8, spacing3=4)
        self.scan_text.tag_configure("good", foreground=MINT)
        self.scan_text.tag_configure("warning", foreground=AMBER)
        self.scan_text.tag_configure("bad", foreground=RED)

    def _build_player_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["player"] = page

        heading = self._page_heading(
            page,
            "PLAYER DOSSIER",
            "Latest observed progression — search skills with level>=5, bonus:damage, tag:priority, or plain text",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        metrics = tk.Frame(page, bg=BG)
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for column in range(6):
            metrics.grid_columnconfigure(column, weight=1)
        self.player_metric_labels: dict[str, tk.Label] = {}
        for column, (key, caption) in enumerate(
            (
                ("credits", "CREDITS"),
                ("level", "LEVEL"),
                ("xp", "XP PROGRESS"),
                ("points", "SKILL POINTS"),
                ("kills", "KILLS  E / P"),
                ("playtime", "PLAY TIME"),
            )
        ):
            card = tk.Frame(metrics, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=12, pady=9)
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
            value = tk.Label(card, text="-", bg=PANEL, fg=TEXT, font=("Cascadia Mono", 13, "bold"))
            value.pack(anchor="w")
            tk.Label(card, text=caption, bg=PANEL, fg=MUTED, font=("Cascadia Mono", 7, "bold")).pack(anchor="w", pady=(3, 0))
            self.player_metric_labels[key] = value

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)

        skills_wrap = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        skills_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        skills_wrap.grid_rowconfigure(3, weight=1)
        skills_wrap.grid_columnconfigure(0, weight=1)
        tk.Label(skills_wrap, text="SKILLS", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 9, "bold"), padx=13, pady=10, anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew")
        self.player_xp_canvas = tk.Canvas(skills_wrap, height=18, bg=BG, highlightthickness=0)
        self.player_xp_canvas.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=10)
        player_skill_tools = tk.Frame(skills_wrap, bg=PANEL_2, padx=10, pady=7)
        player_skill_tools.grid(row=2, column=0, columnspan=2, sticky="ew")
        tk.Label(player_skill_tools, text="FIND SKILL", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        player_skill_search = tk.Entry(
            player_skill_tools,
            textvariable=self.player_skill_search_var,
            bg=BG,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            font=MONO,
        )
        player_skill_search.pack(side="left", fill="x", expand=True, padx=(7, 8), ipady=5)
        player_skill_search.bind("<KeyRelease>", lambda _event: self._populate_player())
        self.player_skill_search_entry = player_skill_search
        self._button(player_skill_tools, "RESET", self._reset_player_skill_filter, PANEL_3, CYAN).pack(side="left", padx=(0, 8))
        tk.Label(player_skill_tools, textvariable=self.player_skill_result_var, bg=PANEL_2, fg=MINT, font=MONO_SMALL).pack(side="right")
        self.player_skill_tree = ttk.Treeview(
            skills_wrap,
            columns=tuple(PLAYER_SKILL_COLUMN_SPECS),
            show="tree headings",
            style="Archive.Treeview",
            selectmode="browse",
        )
        self.player_skill_tree.column("#0", width=210, minwidth=145, stretch=False)
        for key, spec in PLAYER_SKILL_COLUMN_SPECS.items():
            self.player_skill_tree.column(key, width=spec["width"], minwidth=spec["minwidth"], anchor=spec["anchor"], stretch=False)
        self.player_skill_tree.grid(row=3, column=0, sticky="nsew")
        skill_scroll = ttk.Scrollbar(skills_wrap, orient="vertical", command=self.player_skill_tree.yview, style="Archive.Vertical.TScrollbar")
        skill_scroll.grid(row=3, column=1, sticky="ns")
        player_skill_xscroll = ttk.Scrollbar(skills_wrap, orient="horizontal", command=self.player_skill_tree.xview, style="Archive.Horizontal.TScrollbar")
        player_skill_xscroll.grid(row=4, column=0, sticky="ew")
        self.player_skill_tree.configure(yscrollcommand=skill_scroll.set, xscrollcommand=player_skill_xscroll.set)
        self.player_skill_tree.bind("<<TreeviewSelect>>", self._show_selected_player_skill)
        self.player_skill_tree.bind("<Button-3>", self._show_player_skill_context_menu)
        self._restore_player_skill_table_layout()

        mastery_wrap = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        mastery_wrap.grid(row=0, column=1, sticky="nsew")
        tk.Label(mastery_wrap, text="SELECTED SKILL / ACCOUNT SUMMARY", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 9, "bold"), padx=13, pady=10, anchor="w").pack(fill="x")
        player_skill_actions = tk.Frame(mastery_wrap, bg=PANEL_2, padx=12, pady=8)
        player_skill_actions.pack(side="bottom", fill="x")
        self._button(player_skill_actions, "FIND TRAINER", self._find_selected_skill_trainer, PANEL_3, CYAN).pack(side="left")
        self._button(player_skill_actions, "ORGANIZE SKILL", self._organize_selected_player_skill, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        self.player_summary_text = tk.Text(mastery_wrap, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=14, pady=12, wrap="word", font=MONO, state="disabled")
        self.player_summary_text.pack(side="left", fill="both", expand=True)
        player_scroll = ttk.Scrollbar(mastery_wrap, orient="vertical", command=self.player_summary_text.yview, style="Archive.Vertical.TScrollbar")
        player_scroll.pack(side="right", fill="y")
        self.player_summary_text.configure(yscrollcommand=player_scroll.set)
        self.player_summary_text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 9, "bold"), spacing1=8, spacing3=4)
        self.player_summary_text.tag_configure("label", foreground=MUTED)
        self.player_summary_text.tag_configure("value", foreground=TEXT)
        self.player_summary_text.tag_configure("good", foreground=MINT, font=("Cascadia Mono", 9, "bold"))

    def _build_ship_page(self) -> None:
        page = tk.Frame(self.page_host, bg=BG, padx=14, pady=14)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["ship"] = page

        heading = self._page_heading(
            page,
            "SHIP FITTING",
            "Local what-if fitting calibrated against the latest captured server snapshot — nothing is sent to the game",
        )
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        fit_library = tk.Frame(page, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1, padx=11, pady=8)
        fit_library.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        tk.Label(fit_library, text="SAVED FIT", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        self.saved_fit_combo = ttk.Combobox(
            fit_library,
            textvariable=self.saved_fit_var,
            values=("Unsaved fit",),
            state="readonly",
            width=32,
            style="Archive.TCombobox",
        )
        self.saved_fit_combo.pack(side="left", padx=(7, 8))
        self.saved_fit_combo.bind("<<ComboboxSelected>>", self._load_selected_saved_fit)
        for text_value, command, colour in (
            ("SAVE AS", self._save_fit_as, CYAN),
            ("UPDATE", self._update_saved_fit, MINT),
            ("DUPLICATE", self._duplicate_saved_fit, TEXT),
            ("DELETE", self._delete_saved_fit, RED),
            ("COPY SUMMARY", self._copy_fit_summary, AMBER),
        ):
            tk.Button(
                fit_library,
                text=text_value,
                command=command,
                bg=PANEL_3,
                fg=colour,
                activebackground=BG,
                activeforeground=colour,
                relief="flat",
                bd=0,
                padx=9,
                pady=6,
                cursor="hand2",
                font=("Cascadia Mono", 7, "bold"),
            ).pack(side="left", padx=(0, 6))
        tk.Label(fit_library, text="LOCAL ONLY", bg=PANEL_2, fg=MINT, font=("Cascadia Mono", 7, "bold")).pack(side="right")

        body = tk.Frame(page, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, minsize=300)
        body.grid_columnconfigure(1, weight=2, minsize=350)
        body.grid_columnconfigure(2, weight=3, minsize=390)

        identity = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        identity.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ship_art_wrap = tk.Frame(identity, bg=BG, height=250)
        ship_art_wrap.pack(fill="x")
        ship_art_wrap.pack_propagate(False)
        self.ship_art_label = tk.Label(ship_art_wrap, text="NO SHIP SNAPSHOT", bg=BG, fg=MUTED, font=("Cascadia Mono", 10, "bold"))
        self.ship_art_label.pack(fill="both", expand=True)
        self.ship_name_label = tk.Label(identity, text="Awaiting current ship", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold"), anchor="w", padx=15, wraplength=270)
        self.ship_name_label.pack(fill="x", pady=(13, 2))
        self.ship_class_label = tk.Label(identity, text="Open Ship Specs in game, then refresh", bg=PANEL, fg=MUTED, font=MONO_SMALL, anchor="w", justify="left", padx=15, wraplength=270)
        self.ship_class_label.pack(fill="x", pady=(2, 10))
        self.ship_quick_text = tk.Text(identity, height=10, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=14, pady=8, wrap="word", font=MONO, state="disabled")
        self.ship_quick_text.pack(fill="both", expand=True)
        self.ship_quick_text.tag_configure("label", foreground=MUTED)
        self.ship_quick_text.tag_configure("value", foreground=TEXT)

        loadout = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        loadout.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        loadout.grid_rowconfigure(2, weight=1)
        loadout.grid_columnconfigure(0, weight=1)
        tk.Label(loadout, text="FITTED LOADOUT", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 9, "bold"), padx=13, pady=10, anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew")
        fit_controls = tk.Frame(loadout, bg=PANEL, padx=8, pady=8)
        fit_controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.fit_change_button = tk.Button(
            fit_controls,
            text="CHANGE SELECTED",
            command=self._change_selected_fit_item,
            bg=BLUE,
            fg="#ffffff",
            activebackground="#55a8ff",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=9,
            pady=6,
            cursor="hand2",
            font=("Cascadia Mono", 7, "bold"),
        )
        self.fit_change_button.pack(side="left")
        tk.Button(
            fit_controls,
            text="REMOVE",
            command=self._remove_selected_fit_item,
            bg=PANEL_2,
            fg=AMBER,
            activebackground=PANEL_3,
            activeforeground=AMBER,
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            cursor="hand2",
            font=("Cascadia Mono", 7, "bold"),
        ).pack(side="left", padx=(5, 0))
        tk.Button(
            fit_controls,
            text="RESET CURRENT",
            command=self._reset_fit_to_current,
            bg=PANEL_2,
            fg=MINT,
            activebackground=PANEL_3,
            activeforeground=MINT,
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            cursor="hand2",
            font=("Cascadia Mono", 7, "bold"),
        ).pack(side="right")
        self.ship_fit_tree = ttk.Treeview(loadout, columns=("slot", "type"), show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.ship_fit_tree.heading("#0", text="ITEM", anchor="w")
        self.ship_fit_tree.heading("slot", text="SLOT", anchor="w")
        self.ship_fit_tree.heading("type", text="TYPE", anchor="w")
        self.ship_fit_tree.column("#0", width=190, minwidth=130, stretch=True)
        self.ship_fit_tree.column("slot", width=105, minwidth=80)
        self.ship_fit_tree.column("type", width=100, minwidth=75)
        self.ship_fit_tree.grid(row=2, column=0, sticky="nsew")
        fit_scroll = ttk.Scrollbar(loadout, orient="vertical", command=self.ship_fit_tree.yview, style="Archive.Vertical.TScrollbar")
        fit_scroll.grid(row=2, column=1, sticky="ns")
        self.ship_fit_tree.configure(yscrollcommand=fit_scroll.set)
        self.ship_fit_tree.bind("<Double-1>", self._change_selected_fit_item)
        fit_options = tk.Frame(loadout, bg=PANEL_2, padx=9, pady=7)
        fit_options.grid(row=3, column=0, columnspan=2, sticky="ew")
        tk.Checkbutton(
            fit_options,
            text="APPLY CAPTURED PLAYER SKILLS",
            variable=self.fit_apply_skills_var,
            command=self._render_fit_simulation,
            bg=PANEL_2,
            fg=TEXT,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            selectcolor=BG,
            font=("Cascadia Mono", 7, "bold"),
            cursor="hand2",
        ).pack(anchor="w")

        specs = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        specs.grid(row=0, column=2, sticky="nsew")
        tk.Label(specs, text="PROJECTED PERFORMANCE", bg=PANEL_2, fg=CYAN, font=("Cascadia Mono", 9, "bold"), padx=13, pady=10, anchor="w").pack(fill="x")
        metric_wrap = tk.Frame(specs, bg=PANEL, padx=8, pady=8)
        metric_wrap.pack(fill="x")
        for column in range(3):
            metric_wrap.grid_columnconfigure(column, weight=1)
        self.fit_metric_labels: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, caption) in enumerate(
            (
                ("dps", "TOTAL DPS"),
                ("shield", "SHIELD BANK"),
                ("energy", "ENERGY MARGIN"),
                ("speed", "MAX SPEED"),
                ("mass", "FIT MASS"),
                ("cargo", "HULL CAPACITY"),
            )
        ):
            card = tk.Frame(metric_wrap, bg=BG, highlightbackground=LINE, highlightthickness=1, padx=8, pady=6)
            card.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0 if index % 3 == 0 else 5, 0), pady=(0 if index < 3 else 5, 0))
            value = tk.Label(card, text="-", bg=BG, fg=TEXT, font=("Cascadia Mono", 11, "bold"), anchor="w")
            value.pack(fill="x")
            delta = tk.Label(card, text="PROJECTED", bg=BG, fg=MUTED, font=("Cascadia Mono", 6, "bold"), anchor="w")
            delta.pack(fill="x", pady=(2, 0))
            tk.Label(card, text=caption, bg=BG, fg=MUTED, font=("Cascadia Mono", 6, "bold"), anchor="w").pack(fill="x", pady=(2, 0))
            self.fit_metric_labels[key] = (value, delta)
        self.ship_specs_text = tk.Text(specs, bg=PANEL, fg=TEXT, relief="flat", bd=0, padx=14, pady=10, wrap="none", font=MONO, state="disabled")
        self.ship_specs_text.pack(side="left", fill="both", expand=True)
        specs_scroll = ttk.Scrollbar(specs, orient="vertical", command=self.ship_specs_text.yview, style="Archive.Vertical.TScrollbar")
        specs_scroll.pack(side="right", fill="y")
        self.ship_specs_text.configure(yscrollcommand=specs_scroll.set)
        self.ship_specs_text.tag_configure("section", foreground=CYAN, font=("Cascadia Mono", 9, "bold"), spacing1=8, spacing3=4)
        self.ship_specs_text.tag_configure("label", foreground=MUTED)
        self.ship_specs_text.tag_configure("value", foreground=TEXT)
        self.ship_specs_text.tag_configure("good", foreground=MINT)
        self.ship_specs_text.tag_configure("warning", foreground=AMBER)
        self.ship_specs_text.tag_configure("bad", foreground=RED)
        self._populate_saved_fit_choices()

    def _page_heading(self, parent: tk.Widget, title: str, subtitle: str) -> tk.Frame:
        frame = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=16, pady=11)
        tk.Label(frame, text=title, bg=PANEL, fg=TEXT, font=("Cascadia Mono", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(frame, text=subtitle, bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w").pack(fill="x", pady=(3, 0))
        return frame

    def _open_quick_help(self) -> None:
        messagebox.showinfo(
            "Star Empire Companion — Quick Help",
            "SEARCH\n"
            "Ctrl+K searches items, systems, stations, planets, and skills together.\n"
            "Ctrl+F focuses the search box on the current tab. Ctrl+R refreshes the local archive.\n\n"
            "Use plain words, quoted phrases, fields such as item:Wasp, system:Bestla, resource:gold, "
            "tag:priority, favorite:yes, and numeric comparisons such as damage>=100 or score>=75.\n\n"
            "CUSTOMIZE\n"
            "Click table headings to sort. Right-click a heading to choose columns or a preset. "
            "Column order, widths, sort, and horizontal position are saved when the app closes.\n\n"
            "MY INTEL\n"
            "Right-click a record or use ORGANIZE to add favourites, watchlists, personal categories, tags, and notes. "
            "Official game categories are never changed.\n\n"
            "CROSS-NAVIGATION\n"
            "Open records from search, show stations/items/planets/trainers on the map, and use saved fittings to compare projected stats.\n\n"
            "DATA SAFETY\n"
            "Archive browsing is read-only. The optional Game Link records only data the normal game client already receives; it never sends game commands or adds in-game controls. "
            "No save, account, or server data is edited directly.",
            parent=self.root,
        )

    def _focus_current_search(self, _event=None) -> str:
        entry_by_page = {
            "items": getattr(self, "item_search_entry", None),
            "map": getattr(self, "map_search_entry", None),
            "stations": getattr(self, "station_search_entry", None),
            "training": getattr(self, "training_search_entry", None),
            "scans": getattr(self, "scan_search_entry", None),
            "player": getattr(self, "player_skill_search_entry", None),
        }
        entry = entry_by_page.get(self.current_page)
        if entry is None:
            self._open_global_search()
            return "break"
        entry.focus_set()
        entry.select_range(0, "end")
        return "break"

    def _keyboard_refresh(self, _event=None) -> str:
        if not self.loading:
            self.refresh_data()
        return "break"

    def _button(self, parent: tk.Widget, text: str, command, background: str, foreground: str) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground="#72e6ff" if background == CYAN else PANEL_3,
            activeforeground="#03131b" if background == CYAN else TEXT,
            disabledforeground="#527084",
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            cursor="hand2",
            font=("Cascadia Mono", 9, "bold"),
        )

    def _checkbutton(self, parent: tk.Widget, text: str, variable: tk.BooleanVar, command) -> tk.Checkbutton:
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            bg=PANEL_2,
            fg=MUTED,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            selectcolor=BG,
            font=FONT_SMALL,
            anchor="w",
            cursor="hand2",
        )

    def _organize_record(
        self,
        record_type: str,
        record_id: Any,
        title: str,
        refresh_callback=None,
    ) -> None:
        if record_id in (None, ""):
            return
        current = self.user_state.record_annotation(record_type, record_id)
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Organize — {title}")
        dialog.configure(bg=BG)
        dialog.geometry("620x560")
        dialog.minsize(500, 470)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(6, weight=1)

        tk.Label(
            dialog,
            text=f"MY INTEL  //  {title}",
            bg=PANEL_2,
            fg=CYAN,
            font=("Cascadia Mono", 11, "bold"),
            anchor="w",
            padx=16,
            pady=13,
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            dialog,
            text="Saved locally in this tool. Official game categories and server data are never changed.",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            padx=16,
            pady=10,
        ).grid(row=1, column=0, sticky="ew")

        flags = tk.Frame(dialog, bg=BG, padx=16)
        flags.grid(row=2, column=0, sticky="ew")
        favorite_var = tk.BooleanVar(value=bool(current.get("favorite")))
        watchlist_var = tk.BooleanVar(value=bool(current.get("watchlist")))
        for label, variable in (("FAVOURITE", favorite_var), ("ADD TO WATCHLIST", watchlist_var)):
            tk.Checkbutton(
                flags,
                text=label,
                variable=variable,
                bg=BG,
                fg=TEXT,
                activebackground=BG,
                activeforeground=CYAN,
                selectcolor=PANEL_2,
                font=("Cascadia Mono", 9, "bold"),
            ).pack(side="left", padx=(0, 18))

        fields = tk.Frame(dialog, bg=BG, padx=16, pady=10)
        fields.grid(row=3, column=0, sticky="ew")
        fields.grid_columnconfigure(0, weight=1)
        tk.Label(fields, text="PERSONAL CATEGORY", bg=BG, fg=MUTED, font=MONO_SMALL, anchor="w").grid(row=0, column=0, sticky="ew")
        category_var = tk.StringVar(value=str(current.get("category") or ""))
        category_combo = ttk.Combobox(
            fields,
            textvariable=category_var,
            values=self.user_state.personal_categories(),
            state="normal",
            style="Archive.TCombobox",
        )
        category_combo.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        tk.Label(fields, text="TAGS — comma separated", bg=BG, fg=MUTED, font=MONO_SMALL, anchor="w").grid(row=2, column=0, sticky="ew")
        tags_var = tk.StringVar(value=", ".join(str(value) for value in current.get("tags", [])))
        tags_entry = tk.Entry(fields, textvariable=tags_var, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, font=MONO)
        tags_entry.grid(row=3, column=0, sticky="ew", pady=(4, 0), ipady=7)

        tk.Label(dialog, text="NOTES", bg=BG, fg=MUTED, font=MONO_SMALL, anchor="w", padx=16).grid(row=4, column=0, sticky="ew")
        note = tk.Text(dialog, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, wrap="word", font=FONT, padx=10, pady=9)
        note.grid(row=6, column=0, sticky="nsew", padx=16, pady=(4, 10))
        note.insert("1.0", str(current.get("note") or ""))

        actions = tk.Frame(dialog, bg=PANEL_2, padx=16, pady=11)
        actions.grid(row=7, column=0, sticky="ew")

        def save() -> None:
            try:
                self.user_state.set_record_annotation(
                    record_type,
                    record_id,
                    favorite=favorite_var.get(),
                    watchlist=watchlist_var.get(),
                    category=category_var.get(),
                    tags=tags_var.get(),
                    note=note.get("1.0", "end-1c"),
                )
            except OSError as error:
                messagebox.showerror("Could not save personal intel", str(error), parent=dialog)
                return
            dialog.destroy()
            self.status_var.set(f"Saved personal intel for {title}")
            if refresh_callback:
                refresh_callback()

        self._button(actions, "SAVE", save, CYAN, "#03131b").pack(side="left")
        self._button(actions, "CANCEL", dialog.destroy, PANEL_3, TEXT).pack(side="left", padx=(8, 0))
        category_combo.focus_set()

    def _organize_selected_item(self) -> None:
        selection = self.item_tree.selection()
        if not selection:
            return
        item = self.item_by_id.get(selection[0])
        if item:
            self._organize_record("item", item.get("id"), str(item.get("name") or "Item"), self._refresh_item_metadata_views)

    def _show_selected_item_sellers(self) -> None:
        selection = self.item_tree.selection()
        if not selection:
            return
        item = self.item_by_id.get(selection[0])
        if item:
            self._show_item_sellers_on_map(item)

    def _organize_selected_item_from_station(self) -> None:
        selection = self.station_item_tree.selection()
        if not selection:
            return
        item = self.item_by_id.get(selection[0])
        if item:
            self._organize_record("item", item.get("id"), str(item.get("name") or "Item"), self._refresh_item_metadata_views)

    def _refresh_item_metadata_views(self) -> None:
        self.apply_filters()
        if self.current_station_id:
            self._show_selected_station()

    def _organize_selected_station(self) -> None:
        station = next((entry for entry in self.data.get("stations", []) if entry.get("id") == self.current_station_id), None)
        if station:
            self._organize_record("station", station.get("id"), str(station.get("name") or "Station"), self._populate_stations)

    def _organize_selected_scan(self) -> None:
        scan = self._selected_scan()
        if scan:
            self._organize_record(
                "planet",
                scan_annotation_key(scan),
                str(scan.get("planet_name") or "Planet"),
                self._populate_scans,
            )

    def _global_search_records(self, scope: str, query: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        favourites_only = scope == "My favourites"

        def wanted(kind: str) -> bool:
            return scope in {"Everything", "My favourites", kind}

        def included(personal: dict[str, Any]) -> bool:
            return not favourites_only or bool(personal.get("favorite") or personal.get("watchlist"))

        galaxy = self.data.get("map") if isinstance(self.data.get("map"), dict) else {}
        if wanted("Systems"):
            for system in galaxy.get("systems", []):
                if not isinstance(system, dict):
                    continue
                personal = self.user_state.record_annotation("system", system.get("id") or system.get("name"))
                if not included(personal) or not system_matches_query(system, query, personal):
                    continue
                results.append(
                    {
                        "kind": "system",
                        "name": system.get("name") or "Unknown system",
                        "location": system.get("name") or "Unknown system",
                        "details": f"Hazard {format_number(system.get('hazard'), '0')} • {format_number(system.get('npcStationCount'), '0')} NPC stations",
                        "observed": galaxy.get("observedAt") or "-",
                        "source": "Galaxy map",
                        "systemName": system.get("name"),
                        "system": system,
                        "personal": personal,
                    }
                )
        if wanted("Stations"):
            for station in self.data.get("stations", []):
                if not isinstance(station, dict):
                    continue
                system_name = self._station_system_name(station) or ""
                kind = self._station_kind(station)
                personal = self.user_state.record_annotation("station", station.get("id"))
                if not included(personal) or not station_matches_query(station, self.items, system_name, kind, query, personal):
                    continue
                results.append(
                    {
                        "kind": "station",
                        "name": station.get("name") or "Unknown station",
                        "location": system_name or "Location unknown",
                        "details": f"{kind} • {int(station.get('itemCount') or 0):,} items • {int(station.get('pricedItemCount') or 0):,} priced",
                        "observed": station.get("lastSeen") or "-",
                        "source": ", ".join(station.get("sources") or []) or "Station archive",
                        "station": station,
                        "systemName": system_name,
                        "personal": personal,
                    }
                )
        if wanted("Items"):
            for item in self.items:
                personal = self.user_state.record_annotation("item", item.get("id"))
                if not included(personal) or not item_matches_query(item, query, personal):
                    continue
                markets = [market for market in item.get("markets", []) if isinstance(market, dict)]
                buys = positive_prices(item, "buyPrice")
                latest = max((str(market.get("observedAt") or "") for market in markets), default="")
                results.append(
                    {
                        "kind": "item",
                        "name": item.get("name") or item.get("type") or "Unknown item",
                        "location": f"{len(markets):,} observed markets",
                        "details": f"{item.get('categoryLabel') or 'Unknown'} • T{format_number(item.get('tech'))} • best buy {compact_number(min(buys) if buys else None)}",
                        "observed": latest or "-",
                        "source": "Shop and client catalog",
                        "item": item,
                        "personal": personal,
                    }
                )
        if wanted("Planets"):
            for scan in self.data.get("scans", []):
                if not isinstance(scan, dict):
                    continue
                annotation = self.user_state.scan_annotation(scan)
                personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
                if not included(personal) or not scan_matches_query(scan, annotation, query, personal):
                    continue
                quality, score = self._scan_quality(scan)
                results.append(
                    {
                        "kind": "planet",
                        "name": scan.get("planet_name") or "Unknown planet",
                        "location": self._scan_system_name(scan) or "Location unknown",
                        "details": f"{scan.get('planet_type') or 'Unknown'} • {quality}{f' {score:.0f}' if score is not None else ''} • {self._scan_best_resources(scan, 1)}",
                        "observed": scan.get("observedAt") or "-",
                        "source": "Planet scanner",
                        "scan": scan,
                        "personal": personal,
                    }
                )
        if wanted("Skills"):
            offers = [offer for offer in (self.data.get("training") or {}).get("offers", []) if isinstance(offer, dict)]
            for offer in offers:
                personal = self.user_state.record_annotation("skill", offer.get("skillId"))
                if not included(personal) or not training_matches_query(offer, query, personal):
                    continue
                results.append(
                    {
                        "kind": "skill",
                        "name": offer.get("displayName") or offer.get("skillId") or "Unknown skill",
                        "location": f"{offer.get('stationName') or 'Unknown station'} · {offer.get('systemName') or 'location unknown'}",
                        "details": f"{self._training_offer_status(offer)} • cap {format_number(offer.get('offeredMax'), '0')} • next {format_number(offer.get('nextSpCost'))} SP",
                        "observed": offer.get("observedAt") or "-",
                        "source": "NPC training inventory",
                        "offer": offer,
                        "personal": personal,
                    }
                )
        kind_order = {"system": 0, "station": 1, "planet": 2, "item": 3, "skill": 4}
        results.sort(key=lambda row: (kind_order.get(str(row.get("kind")), 9), str(row.get("name") or "").casefold(), str(row.get("location") or "").casefold()))
        return results

    def _open_global_search(self, _event=None) -> str:
        if self.global_search_dialog and self.global_search_dialog.winfo_exists():
            self.global_search_dialog.deiconify()
            self.global_search_dialog.lift()
            self.global_search_entry.focus_set()
            self.global_search_entry.select_range(0, "end")
            return "break"

        dialog = tk.Toplevel(self.root)
        self.global_search_dialog = dialog
        dialog.title("Search All Star Empire Intel")
        dialog.configure(bg=BG)
        dialog.geometry("1180x720")
        dialog.minsize(900, 560)
        dialog.transient(self.root)
        dialog.grid_rowconfigure(3, weight=1)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.protocol("WM_DELETE_WINDOW", self._close_global_search)

        tk.Label(
            dialog,
            text="UNIVERSAL INTEL SEARCH  //  CTRL+K",
            bg=PANEL_2,
            fg=CYAN,
            font=("Cascadia Mono", 12, "bold"),
            padx=16,
            pady=13,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        search_bar = tk.Frame(dialog, bg=BG, padx=14, pady=11)
        search_bar.grid(row=1, column=0, sticky="ew")
        search_bar.grid_columnconfigure(0, weight=1)
        self.global_search_var = tk.StringVar()
        self.global_scope_var = tk.StringVar(value="Everything")
        self.global_search_entry = tk.Entry(search_bar, textvariable=self.global_search_var, bg=PANEL, fg=TEXT, insertbackground=CYAN, relief="flat", bd=0, font=("Segoe UI", 12))
        self.global_search_entry.grid(row=0, column=0, sticky="ew", ipady=9)
        scope_combo = ttk.Combobox(search_bar, textvariable=self.global_scope_var, state="readonly", values=("Everything", "Items", "Systems", "Stations", "Planets", "Skills", "My favourites"), width=18, style="Archive.TCombobox")
        scope_combo.grid(row=0, column=1, padx=(9, 0))
        tk.Label(search_bar, text="Examples: damage>=100  resource:gold  item:Wasp  tag:priority  favorite:yes", bg=BG, fg=MUTED, font=MONO_SMALL, anchor="w").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0))

        saved_bar = tk.Frame(dialog, bg=PANEL_2, padx=14, pady=8)
        saved_bar.grid(row=2, column=0, sticky="ew")
        tk.Label(saved_bar, text="SAVED SEARCH", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="left")
        self.global_saved_search_var = tk.StringVar(value="Choose a saved search")
        self.global_saved_search_combo = ttk.Combobox(saved_bar, textvariable=self.global_saved_search_var, state="readonly", width=31, style="Archive.TCombobox")
        self.global_saved_search_combo.pack(side="left", padx=(7, 8))
        self.global_saved_search_combo.bind("<<ComboboxSelected>>", self._load_global_saved_search)
        self._button(saved_bar, "SAVE CURRENT", self._save_global_search, PANEL_3, MINT).pack(side="left")
        self._button(saved_bar, "DELETE", self._delete_global_search, PANEL_3, RED).pack(side="left", padx=(7, 0))
        self.global_result_count_var = tk.StringVar()
        tk.Label(saved_bar, textvariable=self.global_result_count_var, bg=PANEL_2, fg=MINT, font=MONO_SMALL).pack(side="right")

        table_wrap = tk.Frame(dialog, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        table_wrap.grid(row=3, column=0, sticky="nsew", padx=14)
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        columns = ("kind", "location", "details", "observed", "source")
        self.global_search_tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings", style="Archive.Treeview", selectmode="browse")
        self.global_search_sort_column = "kind"
        self.global_search_sort_desc = False
        specs = {
            "#0": ("MATCH", 220, "w"),
            "kind": ("TYPE", 80, "w"),
            "location": ("LOCATION", 245, "w"),
            "details": ("SUMMARY", 300, "w"),
            "observed": ("OBSERVED", 145, "w"),
            "source": ("SOURCE", 165, "w"),
        }
        for column, (label, width, anchor) in specs.items():
            self.global_search_tree.heading(column, text=label, anchor=anchor, command=lambda key="name" if column == "#0" else column: self._sort_global_search(key))
            self.global_search_tree.column(column, width=width, minwidth=70, anchor=anchor, stretch=column in {"#0", "details"})
        self.global_search_tree.grid(row=0, column=0, sticky="nsew")
        global_y = ttk.Scrollbar(table_wrap, orient="vertical", command=self.global_search_tree.yview, style="Archive.Vertical.TScrollbar")
        global_y.grid(row=0, column=1, sticky="ns")
        global_x = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.global_search_tree.xview, style="Archive.Horizontal.TScrollbar")
        global_x.grid(row=1, column=0, sticky="ew")
        self.global_search_tree.configure(yscrollcommand=global_y.set, xscrollcommand=global_x.set)
        self.global_search_tree.bind("<Double-1>", self._open_global_search_result)
        self.global_search_tree.bind("<Button-3>", self._show_global_search_context_menu)
        dialog.bind("<Escape>", lambda _event: self._close_global_search())

        actions = tk.Frame(dialog, bg=PANEL_2, padx=14, pady=10)
        actions.grid(row=4, column=0, sticky="ew")
        self._button(actions, "OPEN SELECTED", self._open_global_search_result, CYAN, "#03131b").pack(side="left")
        self._button(actions, "ORGANIZE", self._organize_global_search_result, PANEL_3, MINT).pack(side="left", padx=(8, 0))
        self._button(actions, "COPY RESULTS", self._copy_global_search_results, PANEL_3, AMBER).pack(side="left", padx=(8, 0))
        self._button(actions, "EXPORT CSV", self._export_global_search_results, PANEL_3, CYAN).pack(side="left", padx=(8, 0))
        tk.Label(actions, text="Double-click a result to open its full tab", bg=PANEL_2, fg=MUTED, font=MONO_SMALL).pack(side="right")

        self.global_search_var.trace_add("write", lambda *_args: self._populate_global_search())
        self.global_scope_var.trace_add("write", lambda *_args: self._populate_global_search())
        self._populate_global_saved_searches()
        self._populate_global_search()
        self.global_search_entry.focus_set()
        return "break"

    def _close_global_search(self) -> None:
        if self.global_search_dialog and self.global_search_dialog.winfo_exists():
            self.global_search_dialog.destroy()
        self.global_search_dialog = None

    def _populate_global_saved_searches(self, select_id: str | None = None) -> None:
        if not self.global_search_dialog or not self.global_search_dialog.winfo_exists():
            return
        labels = ["Choose a saved search"]
        self.global_saved_search_by_label = {}
        selected_label = labels[0]
        used: set[str] = set()
        for row in self.user_state.saved_searches():
            label = str(row.get("name") or "Saved search")
            if label in used:
                label = f"{label} [{str(row.get('id') or '')[:6]}]"
            used.add(label)
            labels.append(label)
            self.global_saved_search_by_label[label] = str(row.get("id") or "")
            if select_id and row.get("id") == select_id:
                selected_label = label
        self.global_saved_search_combo.configure(values=labels)
        self.global_saved_search_var.set(selected_label)

    def _populate_global_search(self) -> None:
        if not self.global_search_dialog or not self.global_search_dialog.winfo_exists():
            return
        rows = self._global_search_records(self.global_scope_var.get(), self.global_search_var.get())
        key = self.global_search_sort_column
        rows.sort(key=lambda row: (str(row.get(key) or "").casefold(), str(row.get("name") or "").casefold()), reverse=self.global_search_sort_desc)
        self.global_search_tree.delete(*self.global_search_tree.get_children())
        self.global_search_results = {}
        for index, row in enumerate(rows[:1500]):
            iid = f"global-{index}"
            self.global_search_results[iid] = row
            self.global_search_tree.insert(
                "",
                "end",
                iid=iid,
                text=row.get("name") or "Unknown",
                values=(str(row.get("kind") or "").upper(), row.get("location") or "-", row.get("details") or "-", row.get("observed") or "-", row.get("source") or "-"),
            )
        suffix = "" if len(rows) <= 1500 else " • showing first 1,500"
        self.global_result_count_var.set(f"{len(rows):,} RESULTS{suffix}")
        children = self.global_search_tree.get_children()
        if children:
            self.global_search_tree.selection_set(children[0])
            self.global_search_tree.focus(children[0])

    def _sort_global_search(self, column: str) -> None:
        if self.global_search_sort_column == column:
            self.global_search_sort_desc = not self.global_search_sort_desc
        else:
            self.global_search_sort_column = column
            self.global_search_sort_desc = column == "observed"
        self._populate_global_search()

    def _selected_global_search_result(self) -> dict[str, Any] | None:
        selection = self.global_search_tree.selection() if hasattr(self, "global_search_tree") else ()
        return self.global_search_results.get(selection[0]) if selection else None

    def _show_global_search_context_menu(self, event) -> str | None:
        iid = self.global_search_tree.identify_row(event.y)
        if not iid:
            return None
        self.global_search_tree.selection_set(iid)
        self.global_search_tree.focus(iid)
        menu = tk.Menu(self.global_search_dialog, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Open Full Record", command=self._open_global_search_result)
        menu.add_command(label="Organize / Add Notes", command=self._organize_global_search_result)
        menu.add_separator()
        menu.add_command(label="Copy All Results", command=self._copy_global_search_results)
        menu.add_command(label="Export All Results to CSV", command=self._export_global_search_results)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _open_global_search_result(self, _event=None) -> None:
        row = self._selected_global_search_result()
        if not row:
            return
        kind = row.get("kind")
        self._close_global_search()
        if kind == "system":
            self.show_page("map")
            self._focus_map_system(str(row.get("systemName") or ""))
        elif kind == "station":
            station = row.get("station") or {}
            self.station_search_var.set(str(station.get("name") or ""))
            self.station_kind_var.set("All kinds")
            self.station_location_var.set("All locations")
            self._populate_stations()
            self.show_page("stations")
        elif kind == "item":
            self._open_item_record(row.get("item") or {})
        elif kind == "planet":
            scan = row.get("scan") or {}
            self.scan_search_var.set(str(scan.get("planet_name") or ""))
            self.scan_system_filter_var.set("All systems")
            self.scan_type_filter_var.set("All types")
            self.scan_quality_filter_var.set("All colony ratings")
            self.scan_base_filter_var.set("All base records")
            self._populate_scans()
            self.show_page("scans")
        elif kind == "skill":
            offer = row.get("offer") or {}
            self.training_search_var.set(str(offer.get("displayName") or offer.get("skillId") or ""))
            self.training_system_var.set("All systems")
            self.training_station_var.set("All stations")
            self.training_status_var.set("All offers")
            self._populate_skill_finder()
            self.show_page("training")

    def _organize_global_search_result(self) -> None:
        row = self._selected_global_search_result()
        if not row:
            return
        kind = str(row.get("kind") or "")
        if kind == "system":
            record = row.get("system") or {}
            record_id = record.get("id") or record.get("name")
        elif kind == "station":
            record = row.get("station") or {}
            record_id = record.get("id")
        elif kind == "item":
            record = row.get("item") or {}
            record_id = record.get("id")
        elif kind == "planet":
            record = row.get("scan") or {}
            record_id = scan_annotation_key(record)
        elif kind == "skill":
            record = row.get("offer") or {}
            record_id = record.get("skillId")
        else:
            return
        self._organize_record(kind, record_id, str(row.get("name") or kind.title()), self._populate_global_search)

    def _save_global_search(self) -> None:
        name = simpledialog.askstring("Save Search", "Name this search:", initialvalue=self.global_search_var.get() or self.global_scope_var.get(), parent=self.global_search_dialog)
        if not name or not name.strip():
            return
        try:
            saved = self.user_state.save_search(name.strip(), self.global_scope_var.get(), self.global_search_var.get())
        except OSError as error:
            messagebox.showerror("Could not save search", str(error), parent=self.global_search_dialog)
            return
        self._populate_global_saved_searches(str(saved.get("id")))

    def _load_global_saved_search(self, _event=None) -> None:
        search_id = self.global_saved_search_by_label.get(self.global_saved_search_var.get())
        row = next((entry for entry in self.user_state.saved_searches() if entry.get("id") == search_id), None)
        if row:
            self.global_scope_var.set(str(row.get("scope") or "Everything"))
            self.global_search_var.set(str(row.get("query") or ""))

    def _delete_global_search(self) -> None:
        search_id = self.global_saved_search_by_label.get(self.global_saved_search_var.get())
        if not search_id:
            return
        if not messagebox.askyesno("Delete Saved Search", "Delete this local saved search?", parent=self.global_search_dialog):
            return
        try:
            self.user_state.delete_search(search_id)
        except OSError as error:
            messagebox.showerror("Could not delete search", str(error), parent=self.global_search_dialog)
            return
        self._populate_global_saved_searches()

    def _global_result_rows_for_export(self) -> list[list[str]]:
        rows = [["Type", "Name", "Location", "Summary", "Observed", "Source"]]
        for iid in self.global_search_tree.get_children():
            row = self.global_search_results.get(iid) or {}
            rows.append([str(row.get(key) or "") for key in ("kind", "name", "location", "details", "observed", "source")])
        return rows

    def _copy_global_search_results(self) -> None:
        rows = self._global_result_rows_for_export()
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join("\t".join(row) for row in rows))
        self.status_var.set(f"Copied {max(0, len(rows) - 1):,} search results")

    def _export_global_search_results(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.global_search_dialog,
            title="Export Search Results",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
            initialfile="star-empire-search.csv",
        )
        if not path:
            return
        try:
            with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerows(self._global_result_rows_for_export())
        except OSError as error:
            messagebox.showerror("Could not export search", str(error), parent=self.global_search_dialog)
            return
        self.status_var.set(f"Exported search results to {path}")

    def show_page(self, page: str) -> None:
        if page not in self.pages:
            return
        self.current_page = page
        self.pages[page].tkraise()
        for key, button in self.nav_buttons.items():
            selected = key == page
            button.configure(bg=PANEL_2 if selected else PANEL, fg=CYAN if selected else MUTED)

    def browse_game_directory(self) -> None:
        current = Path(self.game_directory_var.get().strip() or app.GAME_ROOT)
        initial = current if current.is_dir() else current.parent
        selected = filedialog.askdirectory(
            title="Choose the Star Empire game folder",
            initialdir=str(initial) if initial.is_dir() else str(Path.home()),
            mustexist=True,
            parent=self.root,
        )
        if selected:
            self.game_directory_var.set(selected)
            self.use_game_directory()

    def use_game_directory(self) -> None:
        requested = self.game_directory_var.get().strip()
        try:
            selected = app.configure_game_root(requested)
        except (OSError, ValueError) as error:
            self.game_directory_entry.focus_set()
            self.game_directory_entry.selection_range(0, "end")
            messagebox.showerror(
                "Invalid Star Empire Folder",
                f"{error}\n\nNo game file was changed.",
                parent=self.root,
            )
            return

        self.game_directory_var.set(str(selected))
        self.user_state.set_game_directory(selected)
        self.update_logger_status()
        self.refresh_data()

    def update_logger_status(self) -> game_link.PatchInspection:
        status = game_link.inspect_protocol(app.GAME_ROOT)
        self.patch_inspection = status
        if status.state == "installed":
            self.patch_button.configure(
                text="GAME LINK OK",
                bg=PANEL_2,
                fg=MINT,
                activebackground=PANEL_3,
                activeforeground=MINT,
            )
        elif status.state == "repairable":
            self.patch_button.configure(
                text="INSTALL / REPAIR",
                bg="#3a2b08",
                fg=AMBER,
                activebackground="#51400f",
                activeforeground=AMBER,
            )
        else:
            self.patch_button.configure(
                text="PATCH UNAVAILABLE",
                bg="#35151c",
                fg=RED,
                activebackground="#4b1d27",
                activeforeground=RED,
            )
        return status

    def check_or_repair_logger(self) -> None:
        status = self.update_logger_status()
        if status.state == "installed":
            messagebox.showinfo(
                "Star Empire Game Link",
                f"{status.message}\n\n"
                "The Game Link is logger-only. It records data the normal client already receives, sends no game commands, and adds no in-game panel or menu.\n\n"
                f"Target:\n{status.target}",
                parent=self.root,
            )
            return
        if not status.can_repair:
            messagebox.showerror(
                "Game Link Unavailable",
                f"{status.message}\n\nNo game file was changed.\n\nTarget:\n{status.target}",
                parent=self.root,
            )
            return

        confirmed = messagebox.askyesno(
            "Install Star Empire Game Link",
            "Close Star Empire before continuing.\n\n"
            "This installs only the passive archive logger. It records information the normal client receives; it never sends game commands and creates no in-game panel or menu.\n\n"
            "Timestamped backups of Client.exe and protocol.py are created before replacement.\n\n"
            f"Target:\n{status.target}\n\n"
            f"{status.message}\n\nInstall and verify the game link now?",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return

        self.patch_button.configure(state="disabled", text="INSTALLING...")
        self.root.update_idletasks()
        try:
            result = game_link.apply_protocol_patch(app.GAME_ROOT)
        except (OSError, UnicodeError, game_link.PatchError) as error:
            self.update_logger_status()
            messagebox.showerror(
                "Game Link Installation Failed",
                f"The game file was not patched.\n\n{error}",
                parent=self.root,
            )
        else:
            self.update_logger_status()
            backups = game_link_backup_paths(result)
            backup_text = "\n".join(backups) if backups else "No backup was needed."
            messagebox.showinfo(
                "Game Link Installation Complete",
                f"{result.message}\n\nBackups:\n{backup_text}\n\n"
                "Start the game normally and refresh this archive whenever you want to see newly captured data.",
                parent=self.root,
            )
        finally:
            self.patch_button.configure(state="normal")

    def _packaged_companion_path(self) -> Path | None:
        if not getattr(sys, "frozen", False):
            return None
        executable = Path(sys.executable).resolve()
        if executable.name.casefold() != updater.RELEASE_ASSET_NAME.casefold():
            return None
        return executable

    def _set_update_button_ready(self) -> None:
        self.update_button.configure(
            state="normal",
            text="CHECK UPDATE",
            bg=PANEL_2,
            fg=AMBER,
            activebackground=PANEL_3,
            activeforeground=AMBER,
        )

    def _run_application_update_task(self, work, finished) -> None:
        if self.application_update_active:
            return
        self.application_update_active = True

        def worker() -> None:
            try:
                result: Any | None = work()
                error: Exception | None = None
            except Exception as caught:
                result = None
                error = caught
            self.application_update_queue.put((result, error))

        threading.Thread(target=worker, name="Companion update", daemon=True).start()
        self.root.after(80, lambda: self._poll_application_update_task(finished))

    def _poll_application_update_task(self, finished) -> None:
        try:
            result, error = self.application_update_queue.get_nowait()
        except queue.Empty:
            self.root.after(80, lambda: self._poll_application_update_task(finished))
            return
        self.application_update_active = False
        finished(result, error)

    def check_for_application_update(self) -> None:
        if self._packaged_companion_path() is None:
            messagebox.showinfo(
                "Star Empire Companion Update",
                "This source/Python run cannot replace itself. Run the packaged StarEmpireCompanion.exe to check for updates.",
                parent=self.root,
            )
            return
        self.update_button.configure(state="disabled", text="CHECKING...")
        self._run_application_update_task(updater.fetch_latest_release, self._finish_update_check)

    def _finish_update_check(self, result: Any | None, error: Exception | None) -> None:
        self._set_update_button_ready()
        if error is not None:
            messagebox.showerror(
                "Update Check Failed",
                f"No update was downloaded.\n\n{error}",
                parent=self.root,
            )
            return
        if not isinstance(result, updater.CompanionRelease):
            messagebox.showerror("Update Check Failed", "GitHub returned an invalid update result.", parent=self.root)
            return
        if not updater.is_newer_release(result.tag):
            messagebox.showinfo(
                "Star Empire Companion Update",
                f"You are already running the latest public release ({updater.CURRENT_RELEASE_TAG}).",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Companion Update Available",
            f"GitHub release {result.tag} is available.\n\n"
            f"Download {result.executable.name} ({format_number(result.executable.size)} bytes), verify its published SHA-256, and install it when this app restarts?\n\n"
            "No game files or data archives are changed.",
            parent=self.root,
        ):
            return
        self.update_button.configure(state="disabled", text="DOWNLOADING...")
        self._run_application_update_task(
            lambda: updater.stage_verified_update(result),
            self._finish_update_download,
        )

    def _finish_update_download(self, result: Any | None, error: Exception | None) -> None:
        self._set_update_button_ready()
        if error is not None:
            messagebox.showerror(
                "Update Download Failed",
                f"The current Companion was not changed.\n\n{error}",
                parent=self.root,
            )
            return
        staged_update = result if isinstance(result, Path) else None
        target = self._packaged_companion_path()
        if staged_update is None or target is None:
            messagebox.showerror("Update Download Failed", "The verified update could not be staged.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Install Verified Update",
            "The update checksum matches the current GitHub release.\n\n"
            "Close Star Empire Companion now, replace it, and reopen the updated version?",
            parent=self.root,
        ):
            staged_update.unlink(missing_ok=True)
            return
        try:
            updater.schedule_replacement(target, staged_update, os.getpid())
        except updater.UpdateError as update_error:
            messagebox.showerror("Update Installation Failed", str(update_error), parent=self.root)
            return
        messagebox.showinfo(
            "Installing Update",
            "Star Empire Companion will now close. The verified update will replace this executable and reopen it.",
            parent=self.root,
        )
        self.root.after(120, self._on_close)

    def open_share_intel(self) -> None:
        """Open explicit, local-only controls for community observation bundles."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Share community intel")
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="SHARE COMMUNITY INTEL", bg=BG, fg=CYAN,
                 font=("Cascadia Mono", 12, "bold")).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "Export creates a local .secintel.json file for you to share manually.\n"
                "Nothing is uploaded automatically. Player, ship, inventory, local notes,\n"
                "and personal station status are excluded from every bundle."
            ),
            bg=BG, fg=TEXT, justify="left", font=FONT,
        ).pack(anchor="w", pady=(9, 16))
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x")
        self._button(actions, "EXPORT SAFE INTEL…", self._export_shared_intel,
                     CYAN, "#03131b").pack(side="left")
        self._button(actions, "IMPORT INTEL…", self._import_shared_intel,
                     PANEL_2, MINT).pack(side="left", padx=(8, 0))
        self._button(actions, "CLOSE", dialog.destroy, PANEL, MUTED).pack(side="right")

    def _export_shared_intel(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Star Empire Companion intel",
            defaultextension=".secintel.json",
            initialfile="star-empire-companion-intel.secintel.json",
            filetypes=[("S.E.C. shared intel", "*.secintel.json"), ("JSON files", "*.json")],
        )
        if not path:
            return
        try:
            bundle = sharing.write_bundle(Path(path), self.data)
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            messagebox.showerror("Could not export shared intel", str(error), parent=self.root)
            return
        summary = bundle["summary"]
        self.status_var.set(
            f"Shared intel exported • {summary['systems']:,} systems • {summary['scans']:,} scans"
        )
        messagebox.showinfo(
            "Shared intel exported",
            f"Created:\n{path}\n\n"
            f"{summary['systems']:,} systems, {summary['scans']:,} scans, "
            f"{summary['stations']:,} stations, and {summary['items']:,} items.\n\n"
            "Share this file however you choose. S.E.C. does not upload it automatically.",
            parent=self.root,
        )

    def _import_shared_intel(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import Star Empire Companion intel",
            filetypes=[("S.E.C. shared intel", "*.secintel.json"), ("JSON files", "*.json")],
        )
        if not path:
            return
        try:
            bundle, _merged = sharing.import_bundle(
                Path(path), app.STORE._archive_store)
        except (OSError, UnicodeError, TypeError, ValueError, sharing.SharedIntelError) as error:
            messagebox.showerror("Could not import shared intel", str(error), parent=self.root)
            return
        summary = bundle["summary"]
        self.refresh_data()
        self.status_var.set(
            f"Shared intel imported • {summary['systems']:,} systems • {summary['scans']:,} scans"
        )
        messagebox.showinfo(
            "Shared intel imported",
            f"Merged {summary['systems']:,} systems, {summary['scans']:,} scans, "
            f"{summary['stations']:,} stations, and {summary['items']:,} items into your local archive.\n\n"
            "Your player, ship, inventory, local notes, and station ownership status were not imported.",
            parent=self.root,
        )

    def refresh_data(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.refresh_button.configure(state="disabled", text="SCANNING...")
        self.status_dot.configure(fg=AMBER)
        self.status_var.set("Reading current and rotated game logs...")
        self.root.update_idletasks()
        try:
            self.data = app.STORE.get(force=True)
            self._map_territory_cache = None
            self.items = self.data.get("items", [])
            self.item_by_id = {item["id"]: item for item in self.items}
            self.item_icons.clear()
            meta = self.data.get("meta", {})
            self.metric_labels["items"].configure(text=f"{meta.get('itemCount', 0):,}")
            self.metric_labels["stations"].configure(text=f"{meta.get('stationCount', 0):,}")
            self.metric_labels["systems"].configure(text=f"{meta.get('mapSystemCount', 0):,}")
            self.metric_labels["scans"].configure(text=f"{meta.get('scanCount', 0):,}")
            self.source_label.configure(
                text=f"{meta.get('logCount', 0)} merged logs  •  latest {meta.get('latestObservation') or 'unknown'}  •  {meta.get('withArt', 0)} official sprites  •  {meta.get('withStats', 0)} stat sheets"
            )
            self._populate_categories()
            self._populate_station_filter()
            self._populate_stations()
            self._populate_skill_finder()
            self._populate_map()
            self._populate_scans()
            self._populate_system_yields()
            self._populate_player()
            self._populate_ship()
            self.apply_filters()
            if self.global_search_dialog and self.global_search_dialog.winfo_exists():
                self._populate_global_search()
            if meta.get("logExists"):
                self.status_dot.configure(fg=MINT)
                self.status_var.set(
                    f"Archive refreshed • latest observation {meta.get('latestObservation') or 'unknown'} • {meta.get('logBytes', 0):,} log bytes"
                )
            else:
                self.status_dot.configure(fg=RED)
                self.status_var.set(f"Game log not found: {meta.get('logPath', app.LOG_PATH)}")
        except Exception as error:
            self.status_dot.configure(fg=RED)
            self.status_var.set(f"Archive read failed: {error}")
            messagebox.showerror("Star Empire Companion", f"Could not read the local archive.\n\n{error}")
        finally:
            self.loading = False
            self.refresh_button.configure(state="normal", text="REFRESH DATA")

    def _populate_categories(self) -> None:
        current = self._selected_category()
        self.category_list.delete(0, "end")
        self.category_list.insert("end", f"All items  ({len(self.items):,})")
        category_ids = [""]
        for category in self.data.get("categories", []):
            self.category_list.insert("end", f"{category['label']}  ({category['count']:,})")
            category_ids.append(category["id"])
        self.category_ids = category_ids
        try:
            index = category_ids.index(current)
        except ValueError:
            index = 0
        self.category_list.selection_set(index)
        self.category_list.activate(index)

    def _populate_station_filter(self) -> None:
        labels = ["All stations"]
        self.station_by_label = {}
        used: set[str] = set()
        for station in self.data.get("stations", []):
            label = station["name"]
            if label in used:
                label = f"{label} [{station['id']}]"
            used.add(label)
            labels.append(label)
            self.station_by_label[label] = station["id"]
        self.station_combo.configure(values=labels)
        if self.station_var.get() not in labels:
            self.station_var.set("All stations")

    def _selected_category(self) -> str:
        if not hasattr(self, "category_ids"):
            return ""
        selection = self.category_list.curselection()
        if not selection:
            return ""
        index = selection[0]
        return self.category_ids[index] if index < len(self.category_ids) else ""

    def _schedule_filter(self, _event=None) -> None:
        if self.search_after:
            self.root.after_cancel(self.search_after)
        self.search_after = self.root.after(180, self.apply_filters)

    def _reset_item_filters(self) -> None:
        self.search_var.set("")
        self.station_var.set("All stations")
        self.official_only_var.set(False)
        self.priced_only_var.set(False)
        if hasattr(self, "category_list") and self.category_list.size():
            self.category_list.selection_clear(0, "end")
            self.category_list.selection_set(0)
            self.category_list.activate(0)
        self.apply_filters()

    def _restore_item_table_layout(self) -> None:
        saved_layouts = self.user_state.load().get("tableLayouts", {})
        has_saved_layout = isinstance(saved_layouts, dict) and "items" in saved_layouts
        layout = self.user_state.table_layout("items")
        if has_saved_layout:
            self.item_display_order = [column for column in layout["columns"] if column in ITEM_COLUMN_SPECS]
            selected = set(self.item_display_order)
            for key, variable in self.item_column_vars.items():
                variable.set(key in selected)

            widths = layout.get("widths", {})
            name_width = widths.get("name") if isinstance(widths, dict) else None
            if isinstance(name_width, int):
                self.item_tree.column("#0", width=name_width)
            if isinstance(widths, dict):
                for key in ITEM_COLUMN_SPECS:
                    width = widths.get(key)
                    if isinstance(width, int):
                        self.item_tree.column(key, width=width)

            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in ITEM_COLUMN_SPECS:
                self.item_sort_column = sort_column
                self.item_sort_desc = bool(layout.get("sortDescending"))

        self._refresh_item_table_columns()
        self._refresh_item_table_headings()
        xview = float(layout.get("xview") or 0.0) if has_saved_layout else 0.0
        if xview > 0:
            self.item_pending_xview = xview

    def _save_item_table_layout(self) -> None:
        if not hasattr(self, "item_tree") or not self.item_tree.winfo_exists():
            return
        displayed = list(self.item_tree.tk.splitlist(self.item_tree.cget("displaycolumns")))
        widths = {"name": int(self.item_tree.column("#0", "width"))}
        widths.update({key: int(self.item_tree.column(key, "width")) for key in ITEM_COLUMN_SPECS})
        xview = self.item_tree.xview()
        self.user_state.set_table_layout(
            "items",
            columns=displayed,
            widths=widths,
            sort_column=self.item_sort_column,
            sort_descending=self.item_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _item_sort_combo_selected(self, _event=None) -> None:
        column, descending = {
            "Name": ("name", False),
            "Category": ("category", False),
            "Tech high": ("tech", True),
            "Buy low": ("buy", False),
            "Sell high": ("sell", True),
        }.get(self.sort_var.get(), ("name", False))
        self.item_sort_column = column
        self.item_sort_desc = descending
        self._refresh_item_table_headings()
        self.apply_filters()

    def _sort_items_by(self, column: str) -> None:
        if self.item_sort_column == column:
            self.item_sort_desc = not self.item_sort_desc
        else:
            self.item_sort_column = column
            self.item_sort_desc = bool(ITEM_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self.sort_var.set(
            {
                ("name", False): "Name",
                ("category", False): "Category",
                ("tech", True): "Tech high",
                ("buy", False): "Buy low",
                ("sell", True): "Sell high",
            }.get((self.item_sort_column, self.item_sort_desc), "Header sort")
        )
        self._refresh_item_table_headings()
        self.apply_filters()

    def _refresh_item_table_headings(self) -> None:
        if not hasattr(self, "item_tree"):
            return
        name_arrow = " ▼" if self.item_sort_desc else " ▲"
        self.item_tree.heading(
            "#0",
            text="ITEM" + (name_arrow if self.item_sort_column == "name" else ""),
            anchor="w",
            command=lambda: self._sort_items_by("name"),
        )
        for key, spec in ITEM_COLUMN_SPECS.items():
            arrow = ""
            if self.item_sort_column == key:
                arrow = " ▼" if self.item_sort_desc else " ▲"
            self.item_tree.heading(
                key,
                text=spec["label"] + arrow,
                anchor=spec["anchor"],
                command=lambda column=key: self._sort_items_by(column),
            )

    def _refresh_item_table_columns(self) -> None:
        if not hasattr(self, "item_tree"):
            return
        visible = [
            key for key in self.item_display_order
            if key in ITEM_COLUMN_SPECS and self.item_column_vars[key].get()
        ]
        visible.extend(
            key for key in ITEM_COLUMN_SPECS
            if self.item_column_vars[key].get() and key not in visible
        )
        self.item_display_order = list(visible)
        self.item_tree.configure(displaycolumns=visible)

    def _apply_item_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.item_display_order = [column for column in columns if column in ITEM_COLUMN_SPECS]
        for key, variable in self.item_column_vars.items():
            variable.set(key in selected)
        self._refresh_item_table_columns()

    def _show_item_column_menu(self, event) -> str | None:
        if self.item_tree.identify_region(event.x, event.y) != "heading":
            item_id = self.item_tree.identify_row(event.y)
            if not item_id:
                return None
            self.item_tree.selection_set(item_id)
            self.item_tree.focus(item_id)
            item = self.item_by_id.get(item_id)
            if item:
                self._show_item(item)
            menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
            menu.add_command(label="Show Sellers on Map", command=self._show_selected_item_sellers)
            menu.add_command(label="Organize / Add Notes", command=self._organize_selected_item)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=PANEL_2,
            fg=TEXT,
            activebackground=PANEL_3,
            activeforeground=CYAN,
            selectcolor=CYAN,
        )
        for label, columns in ITEM_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_item_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_item_column_preset(tuple(ITEM_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in ITEM_COLUMN_SPECS.items():
            menu.add_checkbutton(
                label=spec["label"].title(),
                variable=self.item_column_vars[key],
                command=self._refresh_item_table_columns,
            )
        self.item_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _sort_item_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sortable: list[tuple[Any, str, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for item in rows:
            personal = self.user_state.record_annotation("item", item.get("id"))
            value = item_column_sort_value(item, self.item_sort_column, personal)
            if value is None:
                missing.append(item)
            else:
                sortable.append((value, str(item.get("name") or "").casefold(), item))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.item_sort_desc)
        missing.sort(key=lambda item: str(item.get("name") or "").casefold())
        return [row[2] for row in sortable] + missing

    def apply_filters(self) -> None:
        self.search_after = None
        query = self.search_var.get().strip()
        category = self._selected_category()
        station_id = self.station_by_label.get(self.station_var.get(), "")
        official_only = self.official_only_var.get()
        priced_only = self.priced_only_var.get()

        filtered = []
        for item in self.items:
            personal = self.user_state.record_annotation("item", item.get("id"))
            if category and item.get("category") != category:
                continue
            if station_id and not any(market.get("stationId") == station_id for market in item.get("markets", [])):
                continue
            if official_only and not item.get("art"):
                continue
            if priced_only and not item.get("flags", {}).get("hasPrice"):
                continue
            if not item_matches_query(item, query, personal):
                continue
            filtered.append(item)

        filtered = self._sort_item_rows(filtered)

        self.filtered_items = filtered
        previous = self.item_tree.selection()
        previous_id = previous[0] if previous else ""
        self.item_tree.delete(*self.item_tree.get_children())
        for item in filtered:
            personal = self.user_state.record_annotation("item", item.get("id"))
            icon = self._item_icon(item)
            self.item_tree.insert(
                "",
                "end",
                iid=item["id"],
                text=item["name"],
                image=icon,
                values=tuple(item_column_display_value(item, column, personal) for column in ITEM_COLUMN_SPECS),
            )
        if self.item_pending_xview is not None:
            xview = self.item_pending_xview
            self.item_pending_xview = None
            self.root.after_idle(
                lambda fraction=xview: self.item_tree.xview_moveto(fraction)
                if self.item_tree.winfo_exists()
                else None
            )
        self.result_var.set(f"{len(filtered):,} of {len(self.items):,} items shown")
        if previous_id and self.item_tree.exists(previous_id):
            self.item_tree.selection_set(previous_id)
            self.item_tree.see(previous_id)
        elif filtered:
            first_id = filtered[0]["id"]
            self.item_tree.selection_set(first_id)
            self.item_tree.focus(first_id)
            self._show_item(filtered[0])
        else:
            self._clear_item_detail("NO MATCHING ITEMS")

    def _item_icon(self, item: dict[str, Any]) -> ImageTk.PhotoImage:
        key = item["id"]
        cached = self.item_icons.get(key)
        if cached:
            return cached

        size = (46, 28)
        image: Image.Image | None = None
        art = item.get("art")
        if isinstance(art, dict) and art.get("folder") and art.get("filename"):
            path = app.ASSET_ROOT / str(art["folder"]) / str(art["filename"])
            try:
                with Image.open(path) as source:
                    contained = ImageOps.contain(source.convert("RGBA"), (44, 26), Image.Resampling.LANCZOS)
                    image = Image.new("RGBA", size, (0, 0, 0, 0))
                    image.alpha_composite(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
            except (OSError, ValueError):
                image = None

        if image is None:
            image = self._generated_art(item, size, labels=False)
        photo = ImageTk.PhotoImage(image)
        self.item_icons[key] = photo
        return photo

    def _show_selected_item(self, _event=None) -> None:
        selection = self.item_tree.selection()
        if selection:
            item = self.item_by_id.get(selection[0])
            if item:
                self._show_item(item)

    def _show_item(self, item: dict[str, Any]) -> None:
        self.item_name_label.configure(text=item.get("name", "Unknown item"))
        personal = self.user_state.record_annotation("item", item.get("id"))
        meta = [item.get("categoryLabel", "Unknown")]
        if item.get("tech") is not None:
            meta.append(f"TECH {format_number(item['tech'])}")
        if item.get("cargoSize") is not None:
            meta.append(f"SIZE {format_number(item['cargoSize'])}")
        meta.append("OFFICIAL ART" if item.get("art") else "GENERATED INTEL ART")
        if personal.get("favorite"):
            meta.append("★ FAVOURITE")
        if personal.get("watchlist"):
            meta.append("WATCHLIST")
        self.item_meta_label.configure(text="  •  ".join(meta))
        description = item.get("description") or "No client description was observed for this item."
        self.item_description_label.configure(text=description)

        image = self._load_item_image(item, (388, 220))
        self.current_photo = ImageTk.PhotoImage(image)
        self.item_art_label.configure(image=self.current_photo, text="")

        self.item_text.configure(state="normal")
        self.item_text.delete("1.0", "end")
        stats = item.get("stats", {})
        if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
            self.item_text.insert("end", "MY INTEL\n", "section")
            self._insert_pair(self.item_text, "Category", str(personal.get("category") or "—"))
            self._insert_pair(self.item_text, "Tags", ", ".join(personal.get("tags", [])) or "—")
            if personal.get("note"):
                self.item_text.insert("end", str(personal.get("note")) + "\n", "value")
            self.item_text.insert("end", "\n", "value")
        self.item_text.insert("end", "STAT SHEET\n", "section")
        if stats:
            for key, value in sorted(stats.items(), key=lambda pair: str(pair[0]).casefold()):
                self._insert_pair(self.item_text, str(key).replace("_", " ").title(), self._format_detail_value(value))
        else:
            self.item_text.insert("end", "No detailed stats have been observed.\n", "label")

        self.item_text.insert("end", "\nMARKET OBSERVATIONS\n", "section")
        markets = item.get("markets", [])
        if markets:
            for market in markets:
                self.item_text.insert("end", f"{market.get('stationName', 'Unknown station')}\n", "station")
                price_line = (
                    f"  Buy {format_number(market.get('buyPrice'))} cr"
                    f"  |  Sell {format_number(market.get('sellPrice'))} cr"
                    f"  |  Stock {format_number(market.get('stock'))}"
                )
                self.item_text.insert("end", price_line + "\n", "price")
                self.item_text.insert(
                    "end",
                    f"  {market.get('sourceLabel', market.get('source', 'Unknown'))}  •  {market.get('observedAt') or 'time unknown'}\n",
                    "label",
                )
        else:
            self.item_text.insert("end", "No market observations recorded.\n", "label")
        self.item_text.configure(state="disabled")
        self.item_text.yview_moveto(0)

    def _clear_item_detail(self, message: str) -> None:
        self.current_photo = None
        self.item_art_label.configure(image="", text=message)
        self.item_name_label.configure(text="Item details")
        self.item_meta_label.configure(text="")
        self.item_description_label.configure(text="")
        self.item_text.configure(state="normal")
        self.item_text.delete("1.0", "end")
        self.item_text.configure(state="disabled")

    def _restore_station_table_layout(self) -> None:
        saved_layouts = self.user_state.load().get("tableLayouts", {})
        has_saved_layout = isinstance(saved_layouts, dict) and "stations" in saved_layouts
        layout = self.user_state.table_layout("stations")
        if has_saved_layout:
            columns = [column for column in layout.get("columns", []) if column in STATION_COLUMN_SPECS]
            if columns:
                self.station_display_order = columns
                selected = set(columns)
                for key, variable in self.station_column_vars.items():
                    variable.set(key in selected)
            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.station_tree.column("#0", width=widths["name"])
                for key in STATION_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.station_tree.column(key, width=widths[key])
            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in STATION_COLUMN_SPECS:
                self.station_sort_column = sort_column
                self.station_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.station_pending_xview = xview
        self._refresh_station_table_columns()
        self._refresh_station_table_headings()

    def _save_station_table_layout(self) -> None:
        if not hasattr(self, "station_tree") or not self.station_tree.winfo_exists():
            return
        displayed = list(self.station_tree.tk.splitlist(self.station_tree.cget("displaycolumns")))
        widths = {"name": int(self.station_tree.column("#0", "width"))}
        widths.update({key: int(self.station_tree.column(key, "width")) for key in STATION_COLUMN_SPECS})
        xview = self.station_tree.xview()
        self.user_state.set_table_layout(
            "stations",
            columns=displayed,
            widths=widths,
            sort_column=self.station_sort_column,
            sort_descending=self.station_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_station_table_columns(self) -> None:
        visible = [
            key for key in self.station_display_order
            if key in STATION_COLUMN_SPECS and self.station_column_vars[key].get()
        ]
        visible.extend(key for key in STATION_COLUMN_SPECS if self.station_column_vars[key].get() and key not in visible)
        self.station_display_order = visible
        self.station_tree.configure(displaycolumns=visible)

    def _apply_station_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.station_display_order = [column for column in columns if column in STATION_COLUMN_SPECS]
        for key, variable in self.station_column_vars.items():
            variable.set(key in selected)
        self._refresh_station_table_columns()

    def _refresh_station_table_headings(self) -> None:
        arrow = " ▼" if self.station_sort_desc else " ▲"
        self.station_tree.heading(
            "#0",
            text="STATION" + (arrow if self.station_sort_column == "name" else ""),
            anchor="w",
            command=lambda: self._sort_stations_by("name"),
        )
        for key, spec in STATION_COLUMN_SPECS.items():
            self.station_tree.heading(
                key,
                text=spec["label"] + (arrow if self.station_sort_column == key else ""),
                anchor=spec["anchor"],
                command=lambda column=key: self._sort_stations_by(column),
            )

    def _sort_stations_by(self, column: str) -> None:
        if self.station_sort_column == column:
            self.station_sort_desc = not self.station_sort_desc
        else:
            self.station_sort_column = column
            self.station_sort_desc = bool(STATION_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_station_table_headings()
        self._populate_stations()

    def _sort_station_rows(self, rows: list[tuple[dict[str, Any], str, str]]) -> list[tuple[dict[str, Any], str, str]]:
        sortable: list[tuple[Any, str, dict[str, Any], str, str]] = []
        missing: list[tuple[dict[str, Any], str, str]] = []
        for station, system_name, kind in rows:
            personal = self.user_state.record_annotation("station", station.get("id"))
            value = station_column_sort_value(station, self.station_sort_column, system_name, kind, personal)
            if value is None:
                missing.append((station, system_name, kind))
            else:
                sortable.append((value, str(station.get("name") or "").casefold(), station, system_name, kind))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.station_sort_desc)
        missing.sort(key=lambda row: str(row[0].get("name") or "").casefold())
        return [(row[2], row[3], row[4]) for row in sortable] + missing

    def _show_station_column_menu(self, event) -> str:
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, selectcolor=CYAN)
        for label, columns in STATION_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_station_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_station_column_preset(tuple(STATION_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in STATION_COLUMN_SPECS.items():
            menu.add_checkbutton(label=spec["label"].title(), variable=self.station_column_vars[key], command=self._refresh_station_table_columns)
        self.station_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_station_context_menu(self, event) -> str | None:
        if self.station_tree.identify_region(event.x, event.y) == "heading":
            return self._show_station_column_menu(event)
        station_id = self.station_tree.identify_row(event.y)
        if not station_id:
            return None
        self.station_tree.selection_set(station_id)
        self.station_tree.focus(station_id)
        self._show_selected_station()
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Show on Map", command=self._show_station_on_map)
        menu.add_command(label="Organize / Add Notes", command=self._organize_selected_station)
        menu.add_command(label="Copy Inventory", command=self._copy_station_inventory)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _restore_station_item_table_layout(self) -> None:
        saved_layouts = self.user_state.load().get("tableLayouts", {})
        has_saved_layout = isinstance(saved_layouts, dict) and "station_items" in saved_layouts
        layout = self.user_state.table_layout("station_items")
        if has_saved_layout:
            columns = [column for column in layout.get("columns", []) if column in STATION_ITEM_COLUMN_SPECS]
            if columns:
                self.station_item_display_order = columns
                selected = set(columns)
                for key, variable in self.station_item_column_vars.items():
                    variable.set(key in selected)
            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.station_item_tree.column("#0", width=widths["name"])
                for key in STATION_ITEM_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.station_item_tree.column(key, width=widths[key])
            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in STATION_ITEM_COLUMN_SPECS:
                self.station_item_sort_column = sort_column
                self.station_item_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.station_item_pending_xview = xview
        self._refresh_station_item_table_columns()
        self._refresh_station_item_table_headings()

    def _save_station_item_table_layout(self) -> None:
        if not hasattr(self, "station_item_tree") or not self.station_item_tree.winfo_exists():
            return
        displayed = list(self.station_item_tree.tk.splitlist(self.station_item_tree.cget("displaycolumns")))
        widths = {"name": int(self.station_item_tree.column("#0", "width"))}
        widths.update({key: int(self.station_item_tree.column(key, "width")) for key in STATION_ITEM_COLUMN_SPECS})
        xview = self.station_item_tree.xview()
        self.user_state.set_table_layout(
            "station_items",
            columns=displayed,
            widths=widths,
            sort_column=self.station_item_sort_column,
            sort_descending=self.station_item_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_station_item_table_columns(self) -> None:
        visible = [
            key for key in self.station_item_display_order
            if key in STATION_ITEM_COLUMN_SPECS and self.station_item_column_vars[key].get()
        ]
        visible.extend(key for key in STATION_ITEM_COLUMN_SPECS if self.station_item_column_vars[key].get() and key not in visible)
        self.station_item_display_order = visible
        self.station_item_tree.configure(displaycolumns=visible)

    def _apply_station_item_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.station_item_display_order = [column for column in columns if column in STATION_ITEM_COLUMN_SPECS]
        for key, variable in self.station_item_column_vars.items():
            variable.set(key in selected)
        self._refresh_station_item_table_columns()

    def _refresh_station_item_table_headings(self) -> None:
        arrow = " ▼" if self.station_item_sort_desc else " ▲"
        self.station_item_tree.heading(
            "#0",
            text="OBSERVED ITEM" + (arrow if self.station_item_sort_column == "name" else ""),
            anchor="w",
            command=lambda: self._sort_station_items_by("name"),
        )
        for key, spec in STATION_ITEM_COLUMN_SPECS.items():
            self.station_item_tree.heading(
                key,
                text=spec["label"] + (arrow if self.station_item_sort_column == key else ""),
                anchor=spec["anchor"],
                command=lambda column=key: self._sort_station_items_by(column),
            )

    def _sort_station_items_by(self, column: str) -> None:
        if self.station_item_sort_column == column:
            self.station_item_sort_desc = not self.station_item_sort_desc
        else:
            self.station_item_sort_column = column
            self.station_item_sort_desc = bool(STATION_ITEM_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_station_item_table_headings()
        self._show_selected_station()

    def _sort_station_item_rows(self, rows: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        sortable: list[tuple[Any, str, dict[str, Any], list[dict[str, Any]]]] = []
        missing: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for item, markets in rows:
            personal = self.user_state.record_annotation("item", item.get("id"))
            value = station_item_column_sort_value(item, markets, self.station_item_sort_column, personal)
            if value is None:
                missing.append((item, markets))
            else:
                sortable.append((value, str(item.get("name") or "").casefold(), item, markets))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.station_item_sort_desc)
        missing.sort(key=lambda row: str(row[0].get("name") or "").casefold())
        return [(row[2], row[3]) for row in sortable] + missing

    def _show_station_item_column_menu(self, event) -> str:
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, selectcolor=CYAN)
        for label, columns in STATION_ITEM_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_station_item_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_station_item_column_preset(tuple(STATION_ITEM_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in STATION_ITEM_COLUMN_SPECS.items():
            menu.add_checkbutton(label=spec["label"].title(), variable=self.station_item_column_vars[key], command=self._refresh_station_item_table_columns)
        self.station_item_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_station_item_context_menu(self, event) -> str | None:
        if self.station_item_tree.identify_region(event.x, event.y) == "heading":
            return self._show_station_item_column_menu(event)
        item_id = self.station_item_tree.identify_row(event.y)
        if not item_id:
            return None
        self.station_item_tree.selection_set(item_id)
        self.station_item_tree.focus(item_id)
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Open Full Item Details", command=self._open_station_item)
        menu.add_command(label="Organize Item / Add Notes", command=self._organize_selected_item_from_station)
        menu.add_command(label="Show Station on Map", command=self._show_station_on_map)
        menu.add_command(label="Copy Item Row", command=self._copy_selected_station_item)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _copy_selected_station_item(self) -> None:
        selection = self.station_item_tree.selection()
        if not selection:
            return
        row = self.station_item_tree.item(selection[0])
        displayed = list(self.station_item_tree.tk.splitlist(self.station_item_tree.cget("displaycolumns")))
        value_by_column = dict(zip(STATION_ITEM_COLUMN_SPECS, row.get("values", [])))
        parts = [str(row.get("text") or "")]
        parts.extend(f"{STATION_ITEM_COLUMN_SPECS[column]['label']}: {value_by_column.get(column, '-')}" for column in displayed)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(parts))
        self.status_var.set(f"Copied station item row for {row.get('text') or 'item'}")

    def _reset_station_filters(self) -> None:
        self.station_search_var.set("")
        self.station_kind_var.set("All kinds")
        self.station_location_var.set("All locations")
        self._populate_stations()

    def _populate_stations(self) -> None:
        previous = self.station_tree.selection()
        previous_id = previous[0] if previous else self.current_station_id or ""
        query = self.station_search_var.get().strip()
        kind_filter = self.station_kind_var.get()
        location_filter = self.station_location_var.get()
        filtered: list[tuple[dict[str, Any], str, str]] = []
        for station in self.data.get("stations", []):
            system_name = self._station_system_name(station) or ""
            kind = self._station_kind(station)
            personal = self.user_state.record_annotation("station", station.get("id"))
            if kind_filter != "All kinds" and kind != kind_filter.upper():
                continue
            if location_filter == "Mapped" and not system_name:
                continue
            if location_filter == "Unmapped" and system_name:
                continue
            if not station_matches_query(station, self.items, system_name, kind, query, personal):
                continue
            filtered.append((station, system_name, kind))

        filtered = self._sort_station_rows(filtered)
        self.station_tree.delete(*self.station_tree.get_children())
        for station, system_name, kind in filtered:
            personal = self.user_state.record_annotation("station", station.get("id"))
            self.station_tree.insert(
                "",
                "end",
                iid=station["id"],
                text=station["name"],
                values=tuple(station_column_display_value(station, column, system_name, kind, personal) for column in STATION_COLUMN_SPECS),
            )
        if self.station_pending_xview is not None:
            xview = self.station_pending_xview
            self.station_pending_xview = None
            self.root.after_idle(
                lambda fraction=xview: self.station_tree.xview_moveto(fraction)
                if self.station_tree.winfo_exists()
                else None
            )
        total = len(self.data.get("stations", []))
        self.station_result_label.configure(text=f"{len(filtered):,} / {total:,} STATIONS")
        children = self.station_tree.get_children()
        if children:
            chosen = previous_id if previous_id in children else children[0]
            self.station_tree.selection_set(chosen)
            self.station_tree.focus(chosen)
            self.station_tree.see(chosen)
            self._show_selected_station()
        else:
            self.current_station_id = None
            self.station_name_label.configure(text="No matching stations")
            self.station_summary_label.configure(text="Adjust the search or filters")
            self.station_item_tree.delete(*self.station_item_tree.get_children())
            self.station_item_result_var.set("")
            self.station_map_button.configure(state="disabled")

    def _station_kind(self, station: dict[str, Any]) -> str:
        sources = " ".join(str(value) for value in station.get("sources", [])).casefold()
        return "PLAYER" if station.get("isMine") or "player station" in sources else "NPC"

    def _station_system_name(self, station: dict[str, Any]) -> str | None:
        return self._resolve_map_system_name(station.get("systemName"), station.get("name"))

    def _show_selected_station(self, _event=None) -> None:
        selection = self.station_tree.selection()
        if not selection:
            return
        station_id = selection[0]
        station = next((entry for entry in self.data.get("stations", []) if entry["id"] == station_id), None)
        if not station:
            return
        self.current_station_id = station_id
        system_name = self._station_system_name(station)
        kind = self._station_kind(station)
        personal = self.user_state.record_annotation("station", station_id)
        personal_flags = ""
        if personal.get("favorite"):
            personal_flags += "  •  ★ favourite"
        if personal.get("watchlist"):
            personal_flags += "  •  watchlist"
        if personal.get("category"):
            personal_flags += f"  •  {personal.get('category')}"
        self.station_name_label.configure(text=station["name"])
        self.station_summary_label.configure(
            text=(
                f"{kind}  •  {system_name or 'location unknown'}  •  "
                f"{station.get('itemCount', 0):,} trade items  •  last seen {station.get('lastSeen') or '-'}{personal_flags}"
            )
        )
        self.station_map_button.configure(state="normal" if system_name else "disabled")
        previous_item = self.station_item_tree.selection()
        previous_item_id = previous_item[0] if previous_item else ""
        self.station_item_tree.delete(*self.station_item_tree.get_children())
        station_items: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        query = self.station_item_search_var.get().strip()
        for item in self.items:
            markets = [market for market in item.get("markets", []) if market.get("stationId") == station_id]
            personal = self.user_state.record_annotation("item", item.get("id"))
            if markets and station_item_matches_query(item, markets, query, personal):
                station_items.append((item, markets))
        station_items = self._sort_station_item_rows(station_items)
        for item, markets in station_items:
            personal = self.user_state.record_annotation("item", item.get("id"))
            self.station_item_tree.insert(
                "",
                "end",
                iid=item["id"],
                text=item["name"],
                values=tuple(station_item_column_display_value(item, markets, column, personal) for column in STATION_ITEM_COLUMN_SPECS),
            )
        self.station_item_result_var.set(f"{len(station_items):,} ITEMS")
        if self.station_item_pending_xview is not None:
            xview = self.station_item_pending_xview
            self.station_item_pending_xview = None
            self.root.after_idle(
                lambda fraction=xview: self.station_item_tree.xview_moveto(fraction)
                if self.station_item_tree.winfo_exists()
                else None
            )
        available = self.station_item_tree.get_children()
        if previous_item_id in available:
            self.station_item_tree.selection_set(previous_item_id)
            self.station_item_tree.focus(previous_item_id)

    def _show_station_on_map(self) -> None:
        station = next(
            (entry for entry in self.data.get("stations", []) if entry.get("id") == self.current_station_id),
            None,
        )
        if not station:
            return
        system_name = self._station_system_name(station)
        if not system_name:
            messagebox.showinfo(
                "Station location unknown",
                "This station was observed before its system name was captured. Visit it again, then refresh the archive.",
                parent=self.root,
            )
            return
        self.show_page("map")
        self._focus_map_system(system_name)

    def _copy_station_inventory(self) -> None:
        station = next(
            (entry for entry in self.data.get("stations", []) if entry.get("id") == self.current_station_id),
            None,
        )
        if not station:
            return
        system_name = self._station_system_name(station) or "Location unknown"
        displayed = list(self.station_item_tree.tk.splitlist(self.station_item_tree.cget("displaycolumns")))
        headers = ["Item", *(STATION_ITEM_COLUMN_SPECS[column]["label"] for column in displayed)]
        lines = [station.get("name") or "Station", f"System: {system_name}", "", "\t".join(headers)]
        for iid in self.station_item_tree.get_children():
            row = self.station_item_tree.item(iid)
            value_by_column = dict(zip(STATION_ITEM_COLUMN_SPECS, row.get("values", [])))
            lines.append("\t".join([str(row.get("text") or ""), *(str(value_by_column.get(column, "-")) for column in displayed)]))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status_var.set(f"Copied {len(lines) - 4:,} trade rows from {station.get('name') or 'station'}")

    def _open_station_item(self, _event=None) -> None:
        selection = self.station_item_tree.selection()
        if not selection:
            return
        item_id = selection[0]
        item = self.item_by_id.get(item_id)
        if item:
            self._open_item_record(item)

    def _open_item_record(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "")
        self.search_var.set(item.get("name", ""))
        self.station_var.set("All stations")
        self.category_list.selection_clear(0, "end")
        self.category_list.selection_set(0)
        self.apply_filters()
        self.show_page("items")
        if self.item_tree.exists(item_id):
            self.item_tree.selection_set(item_id)
            self.item_tree.focus(item_id)
            self.item_tree.see(item_id)
            self._show_item(item)

    def _show_item_sellers_on_map(self, item: dict[str, Any]) -> None:
        systems = {
            system_name.casefold(): system_name
            for market in item.get("markets", [])
            if (system_name := self._resolve_map_system_name(market.get("systemName"), market.get("stationName")))
        }
        if not systems:
            messagebox.showinfo("Seller locations unknown", "No mapped seller has been observed for this item yet.", parent=self.root)
            return
        self.map_search_var.set(str(item.get("name") or ""))
        self.map_mode_var.set("Everything")
        self._populate_map()
        self.map_highlight_systems = set(systems)
        self.show_page("map")
        first = next(iter(systems.values()))
        self._focus_map_system(first)

    def _restore_training_table_layout(self) -> None:
        saved = self.user_state.load().get("tableLayouts", {})
        has_layout = isinstance(saved, dict) and "training" in saved
        layout = self.user_state.table_layout("training")
        if has_layout:
            columns = [column for column in layout.get("columns", []) if column in TRAINING_COLUMN_SPECS]
            if columns:
                self.training_display_order = columns
                selected = set(columns)
                for key, variable in self.training_column_vars.items():
                    variable.set(key in selected)
            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.training_tree.column("#0", width=widths["name"])
                for key in TRAINING_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.training_tree.column(key, width=widths[key])
            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in TRAINING_COLUMN_SPECS:
                self.training_sort_column = sort_column
                self.training_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.training_pending_xview = xview
        self._refresh_training_columns()
        self._refresh_training_headings()

    def _save_training_table_layout(self) -> None:
        if not hasattr(self, "training_tree") or not self.training_tree.winfo_exists():
            return
        displayed = list(self.training_tree.tk.splitlist(self.training_tree.cget("displaycolumns")))
        widths = {"name": int(self.training_tree.column("#0", "width"))}
        widths.update({key: int(self.training_tree.column(key, "width")) for key in TRAINING_COLUMN_SPECS})
        xview = self.training_tree.xview()
        self.user_state.set_table_layout(
            "training",
            columns=displayed,
            widths=widths,
            sort_column=self.training_sort_column,
            sort_descending=self.training_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_training_columns(self) -> None:
        visible = [key for key in self.training_display_order if key in TRAINING_COLUMN_SPECS and self.training_column_vars[key].get()]
        visible.extend(key for key in TRAINING_COLUMN_SPECS if self.training_column_vars[key].get() and key not in visible)
        self.training_display_order = visible
        self.training_tree.configure(displaycolumns=visible)

    def _apply_training_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.training_display_order = [column for column in columns if column in TRAINING_COLUMN_SPECS]
        for key, variable in self.training_column_vars.items():
            variable.set(key in selected)
        self._refresh_training_columns()

    def _refresh_training_headings(self) -> None:
        arrow = " ▼" if self.training_sort_desc else " ▲"
        self.training_tree.heading("#0", text="SKILL" + (arrow if self.training_sort_column == "name" else ""), anchor="w", command=lambda: self._sort_training_by("name"))
        for key, spec in TRAINING_COLUMN_SPECS.items():
            self.training_tree.heading(key, text=spec["label"] + (arrow if self.training_sort_column == key else ""), anchor=spec["anchor"], command=lambda column=key: self._sort_training_by(column))

    def _sort_training_by(self, column: str) -> None:
        if self.training_sort_column == column:
            self.training_sort_desc = not self.training_sort_desc
        else:
            self.training_sort_column = column
            self.training_sort_desc = bool(TRAINING_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_training_headings()
        self._populate_skill_finder()

    def _sort_training_rows(self, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sortable: list[tuple[Any, str, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for offer in offers:
            personal = self.user_state.record_annotation("skill", offer.get("skillId"))
            value = training_column_sort_value(offer, self.training_sort_column, personal)
            if value is None:
                missing.append(offer)
            else:
                sortable.append((value, str(offer.get("displayName") or offer.get("skillId") or "").casefold(), offer))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.training_sort_desc)
        missing.sort(key=lambda offer: str(offer.get("displayName") or offer.get("skillId") or "").casefold())
        return [row[2] for row in sortable] + missing

    def _show_training_column_menu(self, event) -> str:
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, selectcolor=CYAN)
        for label, columns in TRAINING_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_training_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_training_column_preset(tuple(TRAINING_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in TRAINING_COLUMN_SPECS.items():
            menu.add_checkbutton(label=spec["label"].title(), variable=self.training_column_vars[key], command=self._refresh_training_columns)
        self.training_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_training_context_menu(self, event) -> str | None:
        if self.training_tree.identify_region(event.x, event.y) == "heading":
            return self._show_training_column_menu(event)
        iid = self.training_tree.identify_row(event.y)
        if not iid:
            return None
        self.training_tree.selection_set(iid)
        self.training_tree.focus(iid)
        self._show_selected_training_offer()
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Show Trainer on Map", command=self._show_training_on_map)
        menu.add_command(label="Open Station", command=self._open_training_station)
        menu.add_command(label="Find Required Item", command=self._open_training_required_item)
        menu.add_separator()
        menu.add_command(label="Organize Skill / Add Notes", command=self._organize_selected_training_skill)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _reset_training_filters(self) -> None:
        self.training_search_var.set("")
        self.training_system_var.set("All systems")
        self.training_station_var.set("All stations")
        self.training_status_var.set("All offers")
        self._populate_skill_finder()

    def _organize_selected_training_skill(self) -> None:
        offer = self._selected_training_offer()
        if offer:
            self._organize_record("skill", offer.get("skillId"), str(offer.get("displayName") or offer.get("skillId") or "Skill"), self._refresh_skill_metadata_views)

    def _refresh_skill_metadata_views(self) -> None:
        self._populate_skill_finder()
        self._populate_player()

    def _open_training_station(self) -> None:
        offer = self._selected_training_offer()
        if not offer:
            return
        self.station_search_var.set(str(offer.get("stationName") or ""))
        self.station_kind_var.set("All kinds")
        self.station_location_var.set("All locations")
        self._populate_stations()
        self.show_page("stations")

    def _open_training_required_item(self) -> None:
        offer = self._selected_training_offer()
        if not offer or self._training_item_cost_text(offer) == "-":
            return
        query = str(offer.get("itemCostDisplay") or offer.get("itemCostType") or "").replace("_", " ").strip()
        self.search_var.set(query)
        self.station_var.set("All stations")
        self.category_list.selection_clear(0, "end")
        self.category_list.selection_set(0)
        self.apply_filters()
        self.show_page("items")

    def _training_offer_status(self, offer: dict[str, Any]) -> str:
        return training_offer_status(offer)

    def _training_item_cost_text(self, offer: dict[str, Any]) -> str:
        return training_item_cost_text(offer)

    def _populate_skill_finder(self) -> None:
        offers = [offer for offer in (self.data.get("training") or {}).get("offers", []) if isinstance(offer, dict)]
        previous = self.training_tree.selection()
        previous_target = self.training_offer_targets.get(previous[0]) if previous else None
        previous_key = (
            f"{previous_target.get('stationId')}|{previous_target.get('skillId')}"
            if previous_target else ""
        )
        systems = sorted({str(offer.get("systemName")) for offer in offers if offer.get("systemName")}, key=str.casefold)
        stations = sorted({str(offer.get("stationName")) for offer in offers if offer.get("stationName")}, key=str.casefold)
        specs = (
            (self.training_system_combo, self.training_system_var, ["All systems", *systems]),
            (self.training_station_combo, self.training_station_var, ["All stations", *stations]),
            (
                self.training_status_combo,
                self.training_status_var,
                ["All offers", "Ready now", "Needs items", "Needs SP / credits", "At station cap"],
            ),
        )
        for combo, variable, values in specs:
            combo.configure(values=values)
            if variable.get() not in values:
                variable.set(values[0])

        self.training_tree.delete(*self.training_tree.get_children())
        self.training_offer_targets = {}
        query = self.training_search_var.get().strip()
        restored = ""
        filtered: list[dict[str, Any]] = []
        for offer in offers:
            status = self._training_offer_status(offer)
            if self.training_system_var.get() != "All systems" and offer.get("systemName") != self.training_system_var.get():
                continue
            if self.training_station_var.get() != "All stations" and offer.get("stationName") != self.training_station_var.get():
                continue
            if self.training_status_var.get() != "All offers" and status != self.training_status_var.get():
                continue
            personal = self.user_state.record_annotation("skill", offer.get("skillId"))
            if not training_matches_query(offer, query, personal):
                continue
            filtered.append(offer)

        filtered = self._sort_training_rows(filtered)
        for index, offer in enumerate(filtered):
            iid = f"training-{index}"
            self.training_offer_targets[iid] = offer
            key = f"{offer.get('stationId')}|{offer.get('skillId')}"
            if key == previous_key:
                restored = iid
            personal = self.user_state.record_annotation("skill", offer.get("skillId"))
            self.training_tree.insert(
                "",
                "end",
                iid=iid,
                text=offer.get("displayName") or offer.get("skillId") or "Unknown skill",
                values=tuple(training_column_display_value(offer, column, personal) for column in TRAINING_COLUMN_SPECS),
            )
        if self.training_pending_xview is not None:
            xview = self.training_pending_xview
            self.training_pending_xview = None
            self.root.after_idle(lambda fraction=xview: self.training_tree.xview_moveto(fraction) if self.training_tree.winfo_exists() else None)
        self.training_result_label.configure(text=f"{len(filtered):,} / {len(offers):,} OFFERS")
        children = self.training_tree.get_children()
        if children:
            chosen = restored or children[0]
            self.training_tree.selection_set(chosen)
            self.training_tree.focus(chosen)
            self.training_tree.see(chosen)
            self._show_selected_training_offer()
        else:
            self.training_name_label.configure(text="No captured NPC trainers")
            self.training_summary_label.configure(text="Repair the Game Link, restart the game, then revisit NPC stations")
            self.training_map_button.configure(state="disabled")
            self.training_item_button.configure(state="disabled")
            self._set_text(
                self.training_text,
                "NPC station training inventories have not been captured yet.\n\n"
                "Close the game, select INSTALL / REPAIR, restart Star Empire, and dock at NPC stations. "
                "Their training offers will then be archived automatically.",
            )

    def _selected_training_offer(self) -> dict[str, Any] | None:
        selection = self.training_tree.selection()
        return self.training_offer_targets.get(selection[0]) if selection else None

    def _show_selected_training_offer(self, _event=None) -> None:
        offer = self._selected_training_offer()
        if not offer:
            return
        status = self._training_offer_status(offer)
        name = str(offer.get("displayName") or offer.get("skillId") or "Unknown skill")
        personal = self.user_state.record_annotation("skill", offer.get("skillId"))
        personal_flags = ""
        if personal.get("favorite"):
            personal_flags += "  ·  ★"
        if personal.get("watchlist"):
            personal_flags += "  ·  WATCH"
        if personal.get("category"):
            personal_flags += f"  ·  {personal.get('category')}"
        self.training_name_label.configure(text=name + personal_flags)
        self.training_summary_label.configure(
            text=(
                f"{offer.get('stationName') or 'Unknown station'}  •  "
                f"{offer.get('systemName') or 'location unknown'}  •  {status}"
            )
        )
        self.training_map_button.configure(state="normal" if offer.get("systemName") else "disabled")
        self.training_item_button.configure(state="normal" if self._training_item_cost_text(offer) != "-" else "disabled")
        widget = self.training_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
            widget.insert("end", "MY INTEL\n", "section")
            self._insert_pair(widget, "Category", str(personal.get("category") or "—"))
            self._insert_pair(widget, "Tags", ", ".join(personal.get("tags", [])) or "—")
            if personal.get("note"):
                widget.insert("end", str(personal.get("note")) + "\n", "value")
            widget.insert("end", "\n", "value")
        widget.insert("end", "TRAINING STATUS\n", "section")
        widget.insert("end", f"{status}\n", "good" if status == "Ready now" else "warning" if status != "At station cap" else "bad")
        self._insert_pair(widget, "Current level", format_number(offer.get("currentLevel"), "0"))
        self._insert_pair(widget, "Trainer cap", format_number(offer.get("offeredMax"), "0"))
        self._insert_pair(widget, "Global cap", format_number(offer.get("globalMax")))

        widget.insert("end", "\nNEXT LEVEL COST\n", "section")
        sp_cost = offer.get("nextSpCost")
        self._insert_pair(widget, "Skill points", f"{format_number(sp_cost)}  ·  {format_number(offer.get('availableSkillPoints'), '0')} available")
        self._insert_pair(widget, "Credits", f"{format_number(offer.get('nextCreditCost'), '0')} cr")
        item_cost = self._training_item_cost_text(offer)
        if item_cost != "-":
            self._insert_pair(widget, "Required item", item_cost)
            self._insert_pair(widget, "Owned", format_number(offer.get("itemOwned"), "0"))
        else:
            self._insert_pair(widget, "Required item", "None")

        if offer.get("description"):
            widget.insert("end", "\nDESCRIPTION\n", "section")
            widget.insert("end", str(offer.get("description")) + "\n", "value")

        widget.insert("end", "\nBONUS PER LEVEL\n", "section")
        bonus_rows = 0
        for key, value in sorted((offer.get("statBonus") or {}).items(), key=lambda pair: str(pair[0]).casefold()):
            self._insert_pair(widget, str(key).replace("_", " ").title(), f"+{format_number(value)}")
            bonus_rows += 1
        for key, value in sorted((offer.get("pctBonus") or {}).items(), key=lambda pair: str(pair[0]).casefold()):
            self._insert_pair(widget, str(key).replace("_", " ").title(), f"+{fitting.number(value) * 100:g}%")
            bonus_rows += 1
        if not bonus_rows:
            widget.insert("end", "Unlock / requirement skill; no numeric bonus reported.\n", "label")

        widget.insert("end", "\nTRAINER LOCATION\n", "section")
        self._insert_pair(widget, "Station", str(offer.get("stationName") or "Unknown"))
        self._insert_pair(widget, "System", str(offer.get("systemName") or "Location unknown"))
        self._insert_pair(widget, "Observed", str(offer.get("observedAt") or "Unknown"))
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _show_training_on_map(self) -> None:
        offer = self._selected_training_offer()
        if not offer or not offer.get("systemName"):
            return
        self.show_page("map")
        self._focus_map_system(str(offer["systemName"]))

    def _populate_map(self) -> None:
        galaxy = self.data.get("map") if isinstance(self.data.get("map"), dict) else {}
        values = {
            "systems": galaxy.get("systemCount", 0),
            "edges": galaxy.get("edgeCount", 0),
            "mapped": galaxy.get("mappedStationCount", 0),
            "unmapped": galaxy.get("unmappedStationCount", 0),
        }
        for key, label in self.map_metric_labels.items():
            label.configure(text=f"{int(values[key] or 0):,}", fg=RED if key == "unmapped" and values[key] else TEXT)
        available = {str(system.get("name") or "").casefold() for system in galaxy.get("systems", []) if isinstance(system, dict)}
        if self.map_selected_system and self.map_selected_system.casefold() not in available:
            self.map_selected_system = None
        self._apply_map_search()
        if galaxy.get("hasData"):
            self._render_map_intro()
        else:
            self._render_map_empty_state()
        self.root.after_idle(self._draw_map)

    def _apply_map_search(self, _event=None) -> None:
        query = self.map_search_var.get().strip()
        mode = self.map_mode_var.get()
        galaxy = self.data.get("map") if isinstance(self.data.get("map"), dict) else {}
        systems = [system for system in galaxy.get("systems", []) if isinstance(system, dict)]
        stations = [station for station in self.data.get("stations", []) if isinstance(station, dict)]
        scans = [scan for scan in self.data.get("scans", []) if isinstance(scan, dict)]
        previous = self.map_result_tree.selection() if hasattr(self, "map_result_tree") else ()
        previous_target = self.map_result_targets.get(previous[0]) if previous else None
        self.map_result_tree.delete(*self.map_result_tree.get_children())
        self.map_result_targets.clear()
        self.map_highlight_systems.clear()
        results: list[dict[str, Any]] = []
        favourites_only = mode == "My favourites"

        def included(personal: dict[str, Any]) -> bool:
            return not favourites_only or bool(personal.get("favorite") or personal.get("watchlist"))

        if mode in {"Everything", "Systems", "My favourites"}:
            for system in systems:
                record_id = system.get("id") or system.get("name")
                personal = self.user_state.record_annotation("system", record_id)
                if not included(personal) or not system_matches_query(system, query, personal):
                    continue
                station_counts = system.get("stationCounts") if isinstance(system.get("stationCounts"), dict) else {}
                station_total = int(fitting.number(system.get("npcStationCount")) + sum(fitting.number(value) for value in station_counts.values()))
                results.append(
                    {
                        "kind": "system",
                        "name": system.get("name") or "Unknown system",
                        "location": system.get("name") or "Unknown system",
                        "details": f"Hazard {format_number(system.get('hazard'), '0')} • {station_total:,} stations",
                        "observed": galaxy.get("observedAt") or "-",
                        "systemName": system.get("name"),
                        "system": system,
                        "personal": personal,
                    }
                )

        if mode in {"Everything", "Stations", "My favourites"} and (query or mode != "Everything"):
            for station in stations:
                system_name = self._resolve_map_system_name(station.get("systemName"), station.get("name"))
                kind = self._station_kind(station)
                personal = self.user_state.record_annotation("station", station.get("id"))
                if not included(personal) or not station_matches_query(station, self.items, system_name or "", kind, query, personal):
                    continue
                results.append(
                    {
                        "kind": "station",
                        "name": station.get("name") or "Unknown station",
                        "location": system_name or "Location unknown",
                        "details": f"{kind} • {int(station.get('itemCount') or 0):,} items",
                        "observed": station.get("lastSeen") or "-",
                        "systemName": system_name,
                        "station": station,
                        "personal": personal,
                    }
                )

        if mode in {"Everything", "Shop items", "My favourites"} and (query or mode != "Everything"):
            for item in self.items:
                personal = self.user_state.record_annotation("item", item.get("id"))
                if not included(personal) or not item_matches_query(item, query, personal):
                    continue
                for market in item.get("markets", []):
                    if not isinstance(market, dict):
                        continue
                    station_name = market.get("stationName") or "Unknown station"
                    system_name = self._resolve_map_system_name(market.get("systemName"), station_name)
                    results.append(
                        {
                            "kind": "item",
                            "name": item.get("name") or item.get("type") or "Unknown item",
                            "location": f"{station_name} · {system_name or 'location unknown'}",
                            "details": f"Buy {compact_number(market.get('buyPrice'))} • sell {compact_number(market.get('sellPrice'))} • stock {format_number(market.get('stock'))}",
                            "observed": market.get("observedAt") or "-",
                            "systemName": system_name,
                            "item": item,
                            "market": market,
                            "personal": personal,
                        }
                    )

        if mode in {"Everything", "Planets", "My favourites"} and (query or mode != "Everything"):
            for scan in scans:
                annotation = self.user_state.scan_annotation(scan)
                personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
                if not included(personal) or not scan_matches_query(scan, annotation, query, personal):
                    continue
                system_name = self._scan_system_name(scan)
                quality_label, quality_score = self._scan_quality(scan)
                score_text = f" {quality_score:.0f}" if quality_score is not None else ""
                results.append(
                    {
                        "kind": "planet",
                        "name": scan.get("planet_name") or "Unknown planet",
                        "location": system_name or "Location unknown",
                        "details": f"{scan.get('planet_type') or 'Unknown'} • {quality_label}{score_text} • {self._scan_best_resources(scan, 1)}",
                        "observed": scan.get("observedAt") or "-",
                        "systemName": system_name,
                        "scan": scan,
                        "personal": personal,
                    }
                )

        results = self._sort_map_result_rows(results)
        total = len(results)
        visible = results[:600]
        restored_iid = ""
        for index, result in enumerate(visible):
            iid = f"map-result-{index}"
            self.map_result_targets[iid] = result
            row = dict(result)
            row["kind"] = {"system": "SYSTEM", "station": "STATION", "item": "ITEM", "planet": "PLANET"}.get(result["kind"], result["kind"].upper())
            self.map_result_tree.insert("", "end", iid=iid, text=result["name"], values=tuple(map_result_column_display_value(row, column) for column in MAP_RESULT_COLUMN_SPECS))
            system_name = str(result.get("systemName") or "").strip()
            if query and system_name:
                self.map_highlight_systems.add(system_name.casefold())
            if previous_target and result["kind"] == previous_target.get("kind") and result["name"] == previous_target.get("name") and result["location"] == previous_target.get("location"):
                restored_iid = iid
        if self.map_result_pending_xview is not None:
            xview = self.map_result_pending_xview
            self.map_result_pending_xview = None
            self.root.after_idle(lambda fraction=xview: self.map_result_tree.xview_moveto(fraction) if self.map_result_tree.winfo_exists() else None)
        suffix = "" if total <= len(visible) else f" • showing first {len(visible):,}"
        unknown = galaxy.get("unmappedStationCount", 0) or 0
        self.map_result_var.set(f"{total:,} matches{suffix} • {unknown:,} shops need a fresh location")
        if restored_iid:
            self.map_result_tree.selection_set(restored_iid)
            self.map_result_tree.focus(restored_iid)
            self.map_result_tree.see(restored_iid)
        self._draw_map()

    def _selected_map_target(self) -> dict[str, Any] | None:
        selection = self.map_result_tree.selection()
        if selection:
            return self.map_result_targets.get(selection[0])
        if self.map_selected_system:
            system = next(
                (entry for entry in (self.data.get("map") or {}).get("systems", []) if str(entry.get("name") or "").casefold() == self.map_selected_system.casefold()),
                None,
            )
            if system:
                personal = self.user_state.record_annotation("system", system.get("id") or system.get("name"))
                return {
                    "kind": "system",
                    "name": system.get("name") or "Unknown system",
                    "location": system.get("name") or "Unknown system",
                    "details": f"Hazard {format_number(system.get('hazard'), '0')}",
                    "observed": (self.data.get("map") or {}).get("observedAt") or "-",
                    "systemName": system.get("name"),
                    "system": system,
                    "personal": personal,
                }
        return None

    def _show_map_result_context_menu(self, event) -> str | None:
        if self.map_result_tree.identify_region(event.x, event.y) == "heading":
            return self._show_map_result_column_menu(event)
        iid = self.map_result_tree.identify_row(event.y)
        if not iid:
            return None
        self.map_result_tree.selection_set(iid)
        self.map_result_tree.focus(iid)
        self._show_selected_map_result()
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Open Record", command=self._open_selected_map_record)
        menu.add_command(label="Organize / Add Notes", command=self._organize_selected_map_record)
        menu.add_command(label="Copy Result", command=self._copy_selected_map_result)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _open_selected_map_record(self) -> None:
        target = self._selected_map_target()
        if not target:
            return
        kind = target.get("kind")
        if kind == "station":
            station = target.get("station") or {}
            self.station_search_var.set(str(station.get("name") or ""))
            self.station_kind_var.set("All kinds")
            self.station_location_var.set("All locations")
            self._populate_stations()
            self.show_page("stations")
        elif kind == "item":
            self._open_item_record(target.get("item") or {})
        elif kind == "planet":
            scan = target.get("scan") or {}
            self.scan_search_var.set(str(scan.get("planet_name") or ""))
            self.scan_system_filter_var.set("All systems")
            self.scan_type_filter_var.set("All types")
            self.scan_quality_filter_var.set("All colony ratings")
            self.scan_base_filter_var.set("All base records")
            self._populate_scans()
            self.show_page("scans")
        elif kind == "system":
            self._focus_map_system(str(target.get("systemName") or ""))

    def _organize_selected_map_record(self) -> None:
        target = self._selected_map_target()
        if not target:
            return
        kind = str(target.get("kind") or "record")
        record: dict[str, Any]
        if kind == "system":
            record = target.get("system") or {}
            record_id = record.get("id") or record.get("name")
        elif kind == "station":
            record = target.get("station") or {}
            record_id = record.get("id")
        elif kind == "item":
            record = target.get("item") or {}
            record_id = record.get("id")
        elif kind == "planet":
            record = target.get("scan") or {}
            record_id = scan_annotation_key(record)
        else:
            return
        self._organize_record(kind, record_id, str(target.get("name") or kind.title()), self._refresh_map_metadata_views)

    def _refresh_map_metadata_views(self) -> None:
        self.apply_filters()
        self._populate_stations()
        self._populate_scans()
        self._apply_map_search()

    def _copy_selected_map_result(self) -> None:
        target = self._selected_map_target()
        if not target:
            return
        lines = [
            str(target.get("name") or "Map result"),
            f"Type: {str(target.get('kind') or 'record').title()}",
            f"Location: {target.get('location') or 'Unknown'}",
            f"Summary: {target.get('details') or '-'}",
            f"Observed: {target.get('observed') or '-'}",
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status_var.set(f"Copied map result for {target.get('name') or 'record'}")

    def _show_map_system_context(self, event, name: str) -> str:
        self._select_map_canvas_system(name)
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Organize System / Add Notes", command=lambda: self._organize_map_system(name))
        menu.add_command(label="Copy System Name", command=lambda: self._copy_text(name, f"Copied system name {name}"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _organize_map_system(self, name: str) -> None:
        system = next((entry for entry in (self.data.get("map") or {}).get("systems", []) if str(entry.get("name") or "").casefold() == name.casefold()), None)
        if system:
            self._organize_record("system", system.get("id") or system.get("name"), name, self._refresh_map_metadata_views)

    def _copy_text(self, value: str, status: str = "Copied") -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status_var.set(status)

    def _resolve_map_system_name(self, explicit_name: Any, station_name: Any) -> str | None:
        systems = [
            str(system.get("name") or "").strip()
            for system in (self.data.get("map") or {}).get("systems", [])
            if isinstance(system, dict) and str(system.get("name") or "").strip()
        ]
        canonical = {name.casefold(): name for name in systems}
        explicit = str(explicit_name or "").strip()
        if explicit:
            return canonical.get(explicit.casefold(), explicit)
        station = str(station_name or "").strip().casefold()
        if not station:
            return None
        for system_name in sorted(systems, key=len, reverse=True):
            folded = system_name.casefold()
            start = station.find(folded)
            while start >= 0:
                end = start + len(folded)
                before_clear = start == 0 or not station[start - 1].isalnum()
                after_clear = end == len(station) or not station[end].isalnum()
                if before_clear and after_clear:
                    return system_name
                start = station.find(folded, start + 1)
        return None

    def _show_selected_map_result(self, _event=None) -> None:
        selection = self.map_result_tree.selection()
        if not selection:
            return
        target = self.map_result_targets.get(selection[0])
        if not target:
            return
        kind = target.get("kind")
        if kind == "system":
            self._focus_map_system(str(target.get("systemName") or ""))
            self._render_map_system_detail(target.get("system") or {})
        elif kind == "station":
            system_name = str(target.get("systemName") or "")
            if system_name:
                self._focus_map_system(system_name)
            station = dict(target.get("station") or {})
            if system_name and not station.get("systemName"):
                station["systemName"] = system_name
            self._render_map_station_detail(station)
        elif kind == "item":
            system_name = str(target.get("systemName") or "")
            if system_name:
                self._focus_map_system(system_name)
            market = dict(target.get("market") or {})
            if system_name and not market.get("systemName"):
                market["systemName"] = system_name
            self._render_map_item_detail(target.get("item") or {}, market)
        elif kind == "planet":
            system_name = str(target.get("systemName") or "")
            if system_name:
                self._focus_map_system(system_name)
            self._render_map_planet_detail(target.get("scan") or {})

    def _render_map_intro(self) -> None:
        galaxy = self.data.get("map") or {}
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "GALAXY ARCHIVE READY\n", "section")
        self._insert_pair(widget, "Systems", format_number(galaxy.get("systemCount"), "0"))
        self._insert_pair(widget, "Jump connections", format_number(galaxy.get("edgeCount"), "0"))
        self._insert_pair(widget, "Located stations", format_number(galaxy.get("mappedStationCount"), "0"))
        self._insert_pair(widget, "Observed", str(galaxy.get("observedAt") or "Unknown"))
        widget.insert("end", "\nHOW TO USE\n", "section")
        widget.insert("end", "Search systems, stations, shop items, or planet scans. Select a result to focus it; use Open Record for the full archive entry. Right-click systems or results for notes and actions. Drag to pan and use the mouse wheel to zoom.\n", "value")
        if galaxy.get("unmappedStationCount"):
            widget.insert("end", "\nLOCATION COVERAGE\n", "section")
            widget.insert("end", f"{galaxy.get('unmappedStationCount'):,} older shop observations do not yet identify their system. Revisit those shops after the current Game Link is installed to map them.\n", "warning")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_map_empty_state(self) -> None:
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "MAP DATA NOT CAPTURED\n", "section")
        widget.insert("end", "The archive has shop records, but the installed game logger has not yet recorded the galaxy layout.\n", "value")
        widget.insert("end", "\nCOLLECT THE MAP\n", "section")
        widget.insert("end", "1. Select INSTALL / REPAIR and install the current Game Link.\n2. Start Star Empire and log in.\n3. Open the in-game galaxy map once.\n4. Return here and select REFRESH DATA.\n", "warning")
        widget.insert("end", "\nYou can already search existing shop items. Their result will show ‘location unknown’ until that station is revisited with the current Game Link.\n", "label")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_map_system_detail(self, system: dict[str, Any]) -> None:
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        name = str(system.get("name") or "Unknown system")
        personal = self.user_state.record_annotation("system", system.get("id") or name)
        widget.insert("end", "SYSTEM\n", "section")
        widget.insert("end", name + "\n", "good")
        self._insert_pair(widget, "System ID", str(system.get("id") or "-"))
        self._insert_pair(widget, "Coordinates", f"{format_number(system.get('x'))}, {format_number(system.get('y'))}")
        self._insert_pair(widget, "Hazard", format_number(system.get("hazard"), "0"))
        self._insert_pair(widget, "Explored", "Yes" if system.get("explored") else "No")
        self._insert_pair(widget, "Ownership", str(system.get("ownership") or "Unclaimed"))
        counts = system.get("stationCounts") or {}
        self._insert_pair(widget, "NPC stations", format_number(system.get("npcStationCount"), "0"))
        self._insert_pair(widget, "Your stations", format_number(counts.get("mine"), "0"))
        self._insert_pair(widget, "Coalition stations", format_number(counts.get("coalition"), "0"))
        self._insert_pair(widget, "Other stations", format_number(counts.get("others"), "0"))
        if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
            widget.insert("end", "\nMY INTEL\n", "section")
            self._insert_pair(widget, "Category", str(personal.get("category") or "—"))
            self._insert_pair(widget, "Tags", ", ".join(personal.get("tags", [])) or "—")
            if personal.get("note"):
                widget.insert("end", str(personal.get("note")) + "\n", "value")
        planets = system.get("planetTypes") if isinstance(system.get("planetTypes"), dict) else {}
        widget.insert("end", "\nCELESTIALS\n", "section")
        if planets:
            for planet_type, count in sorted(planets.items()):
                self._insert_pair(widget, str(planet_type).replace("_", " ").title(), format_number(count, "0"))
        else:
            widget.insert("end", "No planet summary captured.\n", "label")
        self._insert_pair(widget, "Moons", format_number(system.get("moonCount"), "0"))

        scanned_bodies = scans_for_system(self.data.get("scans"), name)
        body_label = "BODY" if len(scanned_bodies) == 1 else "BODIES"
        widget.insert("end", "\nSYSTEM EXTRACTOR SLOTS (USED / MAX · TIER MIX)\n", "section")
        base_summary = system_extraction_base_summary(scanned_bodies)
        extractor_records = [
            record for record in self.data.get("privateExtractorUsage", [])
            if isinstance(record, dict)
            and str(record.get("systemName") or "").strip().casefold() == name.casefold()
        ]
        observed_base_label = "BASE" if len(extractor_records) == 1 else "BASES"
        widget.insert(
            "end",
            f"{base_summary['bodies']} SCANNED {body_label} · {base_summary['maxBases']} MAX BASES "
            f"({base_summary['planetBodies']} PLANETS ×{MAX_EXTRACTION_BASES_PER_PLANET}, "
            f"{base_summary['moonBodies']} MOONS ×{MAX_EXTRACTION_BASES_PER_MOON}) · "
            f"{len(extractor_records)} OBSERVED {observed_base_label}\n",
            "label",
        )
        widget.insert("end", "MAX IS BUILD CAPACITY; USED/TIERS ONLY REFLECT BASES YOU DOCKED AT.\n", "label")
        if scanned_bodies:
            max_slots = system_extractor_slot_capacities(scanned_bodies)
            observed_slots = (
                app.system_extractor_slots(extractor_records, name)
                if extractor_records else None
            )
            observed_tiers = (
                app.system_extractor_tier_counts(extractor_records, name)
                if extractor_records else {}
            )
            capacity_entries = system_extraction_capacity_entries(
                max_slots,
                observed_slots,
                observed_tiers,
            )
            if capacity_entries:
                self._insert_system_extraction_entries(
                    widget,
                    capacity_entries,
                )
                if not extractor_records:
                    widget.insert(
                        "end",
                        "Dock at a managed extraction base to record used slots locally.\n",
                        "label",
                    )
            else:
                widget.insert("end", "No recorded extraction yield in this system.\n", "label")
        else:
            widget.insert("end", "No planet resource scans captured here yet.\n", "label")

        galaxy = self.data.get("map") or {}
        connections = []
        for edge in galaxy.get("edges", []):
            if edge.get("source") == name:
                connections.append(edge.get("target"))
            elif edge.get("target") == name:
                connections.append(edge.get("source"))
        widget.insert("end", f"\nJUMP CONNECTIONS ({len(connections)})\n", "section")
        widget.insert("end", "\n".join(f"• {connection}" for connection in sorted(connections, key=str.casefold)) + ("\n" if connections else "No connections captured.\n"), "value")
        station_ids = set(system.get("stationIds") or [])
        station_rows = [station for station in self.data.get("stations", []) if station.get("id") in station_ids]
        widget.insert("end", f"\nKNOWN STATIONS ({len(station_rows)})\n", "section")
        if station_rows:
            for station in station_rows:
                widget.insert("end", f"• {station.get('name')} — {station.get('itemCount', 0):,} shop items\n", "value")
        else:
            widget.insert("end", "No named station observations mapped here yet.\n", "label")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_map_station_detail(self, station: dict[str, Any]) -> None:
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "STATION / SHOP\n", "section")
        widget.insert("end", str(station.get("name") or "Unknown station") + "\n", "good")
        personal = self.user_state.record_annotation("station", station.get("id"))
        self._insert_pair(widget, "Station ID", str(station.get("id") or "-"))
        location = str(station.get("systemName") or "Location unknown")
        widget.insert("end", "System: ", "label")
        widget.insert("end", location + "\n", "value" if station.get("systemName") else "warning")
        self._insert_pair(widget, "Your station", "Yes" if station.get("isMine") else "No")
        self._insert_pair(widget, "Observed items", format_number(station.get("itemCount"), "0"))
        self._insert_pair(widget, "Priced items", format_number(station.get("pricedItemCount"), "0"))
        self._insert_pair(widget, "Sources", ", ".join(station.get("sources") or []) or "-")
        self._insert_pair(widget, "Last seen", str(station.get("lastSeen") or "-"))
        if personal.get("category") or personal.get("tags") or personal.get("note"):
            self._insert_pair(widget, "My category", str(personal.get("category") or "—"))
            self._insert_pair(widget, "My tags", ", ".join(personal.get("tags", [])) or "—")
        item_ids = set(station.get("itemIds") or [])
        items = [item for item in self.items if item.get("id") in item_ids]
        widget.insert("end", f"\nSHOP INVENTORY ({len(items)})\n", "section")
        for item in items[:45]:
            markets = [market for market in item.get("markets", []) if market.get("stationId") == station.get("id")]
            buys = positive_prices({"markets": markets}, "buyPrice")
            price = f" — {format_number(min(buys), '0')} cr" if buys else ""
            widget.insert("end", f"• {item.get('name')}{price}\n", "value")
        if len(items) > 45:
            widget.insert("end", f"…and {len(items) - 45:,} more. Search the item name for exact market rows.\n", "label")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_map_item_detail(self, item: dict[str, Any], market: dict[str, Any]) -> None:
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "SHOP ITEM\n", "section")
        widget.insert("end", str(item.get("name") or "Unknown item") + "\n", "good")
        personal = self.user_state.record_annotation("item", item.get("id"))
        self._insert_pair(widget, "Category", str(item.get("categoryLabel") or "Unknown"))
        self._insert_pair(widget, "Tech", format_number(item.get("tech")))
        self._insert_pair(widget, "Station", str(market.get("stationName") or "Unknown"))
        location = str(market.get("systemName") or "Location unknown")
        widget.insert("end", "System: ", "label")
        widget.insert("end", location + "\n", "value" if market.get("systemName") else "warning")
        self._insert_pair(widget, "Buy", f"{format_number(market.get('buyPrice'))} cr")
        self._insert_pair(widget, "Sell", f"{format_number(market.get('sellPrice'))} cr")
        self._insert_pair(widget, "Stock", format_number(market.get("stock")))
        self._insert_pair(widget, "Observed", str(market.get("observedAt") or "-"))
        self._insert_pair(widget, "Source", str(market.get("sourceLabel") or market.get("source") or "-"))
        if personal.get("category") or personal.get("tags") or personal.get("note"):
            self._insert_pair(widget, "My category", str(personal.get("category") or "—"))
            self._insert_pair(widget, "My tags", ", ".join(personal.get("tags", [])) or "—")
        if item.get("description"):
            widget.insert("end", "\nDESCRIPTION\n", "section")
            widget.insert("end", str(item.get("description")) + "\n", "value")
        widget.insert("end", "\nTIP\n", "section")
        widget.insert("end", "Open this item from the Item Catalog for its complete stat sheet and every observed market.\n", "label")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_map_planet_detail(self, scan: dict[str, Any]) -> None:
        widget = self.map_detail_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        name = str(scan.get("planet_name") or "Unknown planet")
        annotation = self.user_state.scan_annotation(scan)
        personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
        quality_label, quality_score = self._scan_quality(scan)
        widget.insert("end", "PLANET SCAN\n", "section")
        widget.insert("end", name + "\n", "good")
        self._insert_pair(widget, "System", self._scan_system_name(scan) or "Location unknown")
        self._insert_pair(widget, "Type", str(scan.get("planet_type") or "Unknown"))
        self._insert_pair(widget, "Colony rating", f"{quality_label} · {quality_score:.1f}" if quality_score is not None else quality_label)
        self._insert_pair(widget, "Bases", format_number(annotation.get("baseCount"), "0") if annotation.get("hasBase") else "None recorded")
        self._insert_pair(widget, "Best resources", self._scan_best_resources(scan))
        self._insert_pair(widget, "Observed", str(scan.get("observedAt") or "Unknown"))
        if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
            widget.insert("end", "\nMY INTEL\n", "section")
            self._insert_pair(widget, "Category", str(personal.get("category") or "—"))
            self._insert_pair(widget, "Tags", ", ".join(personal.get("tags", [])) or "—")
            if personal.get("note"):
                widget.insert("end", str(personal.get("note")) + "\n", "value")
        widget.insert("end", "\nOpen Record jumps to the full Planet Archive entry.\n", "label")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _schedule_map_redraw(self, _event=None) -> None:
        if self.map_resize_after:
            self.root.after_cancel(self.map_resize_after)
        self.map_resize_after = self.root.after(70, self._draw_map)

    def _map_node_spacing(self, positioned: list[dict[str, Any]]) -> float:
        """Return a low percentile of the nearest-neighbour distance.

        This is what a disc may safely span.  A percentile rather than the
        mean, because the galaxy's dense core is what overlaps first; and a
        percentile rather than the minimum, because a handful of systems share
        identical coordinates and would force the radius to zero.

        Cached against the system count so the O(n) grid sweep runs once per
        data refresh rather than on every pan.
        """
        cache = getattr(self, "_map_spacing_cache", None)
        if cache and cache[0] == len(positioned):
            return cache[1]
        points = [(float(system["x"]), float(system["y"])) for system in positioned]
        spacing = 1.0
        if len(points) >= 2:
            cell = max(1e-6, (max(p[0] for p in points) - min(p[0] for p in points)) / 48.0)
            buckets: dict[tuple[int, int], list[int]] = {}
            for index, (x, y) in enumerate(points):
                buckets.setdefault((int(x // cell), int(y // cell)), []).append(index)
            nearest: list[float] = []
            for index, (x, y) in enumerate(points):
                best = float("inf")
                cx, cy = int(x // cell), int(y // cell)
                for gx in range(cx - 1, cx + 2):
                    for gy in range(cy - 1, cy + 2):
                        for other in buckets.get((gx, gy), ()):
                            if other == index:
                                continue
                            distance = math.hypot(points[other][0] - x, points[other][1] - y)
                            if 0.0 < distance < best:
                                best = distance
                if best < float("inf"):
                    nearest.append(best)
            if nearest:
                nearest.sort()
                spacing = max(1e-4, nearest[len(nearest) // 20])
        self._map_spacing_cache = (len(positioned), spacing)
        return spacing

    def _draw_map(self) -> None:
        """Galaxy map drawn to follow the in-game map.

        The key behaviour copied from the game is that a system's disc SCALES
        with zoom rather than staying a fixed size: zoomed out you get a field
        of small dots, zoomed in you get large discs carrying a name and a
        hazard badge.  Labels only appear once the discs are big enough to
        hold them, which is why the fully zoomed-out view is bare.
        """
        if not hasattr(self, "map_canvas"):
            return
        if self.map_zoom_redraw_after is not None:
            self.root.after_cancel(self.map_zoom_redraw_after)
            self.map_zoom_redraw_after = None
        self.map_resize_after = None
        canvas = self.map_canvas
        canvas.delete("all")
        self.map_territory_photo = None
        self.map_territory_label_photo = None
        width = max(2, canvas.winfo_width())
        height = max(2, canvas.winfo_height())
        for x in range(0, width, 64):
            canvas.create_line(x, 0, x, height, fill=MAP_GRID)
        for y in range(0, height, 64):
            canvas.create_line(0, y, width, y, fill=MAP_GRID)

        galaxy = self.data.get("map") if isinstance(self.data.get("map"), dict) else {}
        positioned, map_edges = connected_map_systems(galaxy)
        if not positioned:
            canvas.create_text(width / 2, height / 2 - 18, text="NO CONNECTED SYSTEMS RECORDED", fill=AMBER, font=("Cascadia Mono", 13, "bold"))
            canvas.create_text(width / 2, height / 2 + 18, text="Open the in-game map and refresh after jump links have been recorded.", fill=MUTED, font=("Cascadia Mono", 8))
            self.map_zoom_label.configure(text=f"{self.map_zoom * 100:.0f}%")
            return

        territory_positions = galaxy.get("territoryPositions")
        territory_snapshot = galaxy.get("territory")
        territory_cache = self._map_territory_cache
        if (
            territory_cache is None
            or territory_cache[0] is not territory_positions
            or territory_cache[1] is not territory_snapshot
        ):
            territory_cells, territory_regions = territory_map_geometry(galaxy)
            self._map_territory_cache = (
                territory_positions,
                territory_snapshot,
                territory_cells,
                territory_regions,
            )
        else:
            territory_cells = territory_cache[2]
            territory_regions = territory_cache[3]

        show_coalition_control = bool(self.map_show_coalition_var.get())
        min_x, min_y, max_x, max_y = map_fit_bounds(positioned)
        self.map_world_center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        self.map_fit_scale = min(
            max(0.0001, (width - 70) / max(1.0, max_x - min_x)),
            max(0.0001, (height - 70) / max(1.0, max_y - min_y)),
        )
        scale = self.map_fit_scale * self.map_zoom
        centre_x = width / 2.0 + self.map_pan_x
        centre_y = height / 2.0 + self.map_pan_y

        def screen(system: dict[str, Any]) -> tuple[float, float]:
            return (
                centre_x + (float(system["x"]) - self.map_world_center[0]) * scale,
                centre_y + (float(system["y"]) - self.map_world_center[1]) * scale,
            )

        def screen_point(point: tuple[float, float]) -> tuple[float, float]:
            return (
                centre_x + (float(point[0]) - self.map_world_center[0]) * scale,
                centre_y + (float(point[1]) - self.map_world_center[1]) * scale,
            )

        # Disc size follows the MEASURED spacing between neighbouring systems.
        # An earlier attempt used the average sqrt(area / count), but the
        # galaxy is a spiral with a dense core where systems sit far closer
        # than average -- 3.9x closer here -- so discs sized on the average
        # buried the core in overlapping circles.
        typical_gap = self._map_node_spacing(positioned)
        gap_px = typical_gap * scale
        radius = max(MAP_NODE_MIN_RADIUS,
                     min(MAP_NODE_MAX_RADIUS, gap_px * MAP_NODE_GAP_FRACTION))
        # A name needs far more room than a disc, so labels are gated on the
        # gap being wide enough to hold text rather than on the disc size.
        # The 35px threshold keeps names visible at twice the former overview
        # distance; the screen-cell pass below still keeps the map readable.
        detail_scale = gap_px >= MAP_LABEL_GAP
        show_labels = bool(self.map_show_names_var.get()) and detail_scale
        labels_left = MAP_LABEL_BUDGET
        label_cells: set[tuple[int, int]] = set()
        selected_fold = str(self.map_selected_system or "").casefold()
        by_name = {_map_system_name(system): system for system in positioned}
        territory_rows = (
            territory_snapshot.items()
            if isinstance(territory_snapshot, dict) else ()
        )
        territory_by_fold = {
            str(name).strip().casefold(): entry
            for name, entry in territory_rows
            if isinstance(entry, dict) and str(name).strip()
        }
        if show_coalition_control and territory_cells:
            visible_cells: list[TerritoryCell] = []
            overlay_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay_image, "RGBA")
            for cell in territory_cells:
                polygon = [screen_point(point) for point in cell.polygon]
                if len(polygon) < 3:
                    continue
                xs = [point[0] for point in polygon]
                ys = [point[1] for point in polygon]
                if max(xs) < 0 or min(xs) > width or max(ys) < 0 or min(ys) > height:
                    continue
                overlay_draw.polygon(
                    [(round(x), round(y)) for x, y in polygon],
                    fill=(*cell.color, 46),
                )
                visible_cells.append(cell)

            for line_width, color_kind in ((7, "glow"), (4, "dark"), (2, "core")):
                for cell in visible_cells:
                    if color_kind == "glow":
                        line_color = (*cell.color, 72)
                    elif color_kind == "dark":
                        line_color = (*MAP_CANVAS_BG_RGB, 230)
                    else:
                        line_color = (*cell.color, 220)
                    for start, end in cell.boundary_segments:
                        sx, sy = screen_point(start)
                        tx, ty = screen_point(end)
                        overlay_draw.line(
                            [(round(sx), round(sy)), (round(tx), round(ty))],
                            fill=line_color,
                            width=line_width,
                        )
            self.map_territory_photo = ImageTk.PhotoImage(overlay_image)
            canvas.create_image(
                0,
                0,
                image=self.map_territory_photo,
                anchor="nw",
                tags=("map-world", "map-territory"),
            )
        for edge in map_edges:
            source = by_name.get(str(edge.get("source") or "").casefold())
            target = by_name.get(str(edge.get("target") or "").casefold())
            if not source or not target:
                continue
            sx, sy = screen(source)
            tx, ty = screen(target)
            involved = {str(source.get("name") or "").casefold(),
                        str(target.get("name") or "").casefold()}
            lit = bool(involved & (self.map_highlight_systems
                                   | ({selected_fold} if selected_fold else set())))
            canvas.create_line(
                sx, sy, tx, ty, fill=MAP_EDGE_LIT if lit else MAP_EDGE,
                width=2 if lit else 1, tags=("map-world", "map-edge"))

        drawn = 0
        for system in positioned:
            x, y = screen(system)
            if x < -60 or x > width + 60 or y < -60 or y > height + 60:
                continue
            name = str(system.get("name") or "Unknown")
            folded = name.casefold()
            selected = folded == selected_fold
            highlighted = folded in self.map_highlight_systems
            explored = bool(system.get("explored"))
            territory_entry = territory_by_fold.get(folded)
            owned_territory = (
                show_coalition_control
                and explored
                and bool(system.get("ownership"))
                and territory_entry is not None
            )
            has_station = bool(
                system.get("stationIds") or system.get("npcStationCount")
                or any((system.get("stationCounts") or {}).values()))
            if selected:
                fill, rim = MAP_SELECTED_FILL, MAP_SELECTED_RIM
            elif highlighted:
                fill, rim = MAP_HIGHLIGHT_FILL, MAP_HIGHLIGHT_RIM
            elif not explored:
                fill, rim = MAP_UNKNOWN_FILL, MAP_UNKNOWN_RIM
            elif has_station:
                fill, rim = MAP_STATION_FILL, MAP_STATION_RIM
            else:
                fill, rim = MAP_SYSTEM_FILL, MAP_SYSTEM_RIM
            if owned_territory and not (selected or highlighted):
                rim = _rgb_hex(territory_rgb(territory_entry))
            node_radius = radius * (1.25 if selected or highlighted else 1.0)
            node = canvas.create_oval(
                x - node_radius, y - node_radius, x + node_radius, y + node_radius,
                fill=fill, outline=rim,
                width=2 if (selected or highlighted or owned_territory) else 1,
                tags=("map-world", "map-node", f"system-node-{folded}"))
            canvas.tag_bind(node, "<Button-1>", lambda _e, n=name: self._select_map_canvas_system(n))
            canvas.tag_bind(node, "<Button-3>", lambda e, n=name: self._show_map_system_context(e, n))
            drawn += 1

            if selected:
                # The game tags the system you are looking at.  The archive has
                # no "current system" field, so this marks the SELECTION rather
                # than claiming to know where the ship is.
                tag_text = "SELECTED"
                half = 4.2 * len(tag_text) / 2.0 + 5
                tag_y = y - node_radius - 15
                canvas.create_rectangle(
                    x - half, tag_y, x + half, tag_y + 12,
                    fill=MAP_TAG_FILL, outline=MAP_SELECTED_RIM, width=1,
                    tags=("map-world", "map-label"))
                canvas.create_text(
                    x, tag_y + 6, text=tag_text, fill=MAP_SELECTED_RIM,
                    anchor="center", font=("Cascadia Mono", 6, "bold"),
                    tags=("map-world", "map-label"))

            if not show_labels or labels_left <= 0:
                continue
            label = name if explored else "???"
            label_y = y + node_radius + 3
            label_cell = (int(x // MAP_LABEL_CELL), int(label_y // MAP_LABEL_CELL))
            if not (selected or highlighted) and label_cell in label_cells:
                continue
            label_cells.add(label_cell)
            labels_left -= 1
            text_id = canvas.create_text(
                x, label_y, text=label,
                fill=MAP_LABEL if explored else MAP_UNKNOWN_LABEL, anchor="n",
                font=("Cascadia Mono", 7, "bold"),
                tags=("map-world", "map-label", "map-node"))
            canvas.tag_bind(text_id, "<Button-1>", lambda _e, n=name: self._select_map_canvas_system(n))
            canvas.tag_bind(text_id, "<Button-3>", lambda e, n=name: self._show_map_system_context(e, n))
            hazard = fitting.number(system.get("hazard"))
            if explored and hazard >= 10:
                badge = f"HAZARD {int(hazard)}"
                half = 4.2 * len(badge) / 2.0 + 4
                badge_y = label_y + 12
                canvas.create_rectangle(
                    x - half, badge_y, x + half, badge_y + 11,
                    fill=MAP_HAZARD_FILL, outline=MAP_HAZARD_RIM, width=1,
                    tags=("map-world", "map-label", "map-hazard"))
                canvas.create_text(
                    x, badge_y + 5, text=badge, fill=MAP_HAZARD_TEXT,
                    anchor="center", font=("Cascadia Mono", 6, "bold"),
                    tags=("map-world", "map-label", "map-hazard"))

        # Coalition names remain available throughout the zoom range.  The
        # exact containment mask still decides whether a complete name fits;
        # zooming in can therefore reveal a tiny region, never hide one.
        if show_coalition_control and territory_regions:
            label_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            painted_label = False
            territory_cell_by_name = {
                cell.system_name.casefold(): cell
                for cell in territory_cells
            }
            for region in territory_regions:
                coalition_name = region.coalition_name.strip()
                if not coalition_name or region.area <= 0.0:
                    continue
                screen_anchor = screen_point(region.anchor)
                if (
                    screen_anchor[0] < -80
                    or screen_anchor[0] > width + 80
                    or screen_anchor[1] < -80
                    or screen_anchor[1] > height + 80
                ):
                    continue
                containment_mask = Image.new("L", (width, height), 0)
                containment_draw = ImageDraw.Draw(containment_mask)
                for system_name in region.system_names:
                    cell = territory_cell_by_name.get(system_name.casefold())
                    if cell is None:
                        continue
                    containment_draw.polygon(
                        [
                            (round(x), round(y))
                            for x, y in (
                                screen_point(point)
                                for point in cell.polygon
                            )
                        ],
                        fill=255,
                    )
                painted_label = paint_straight_map_label(
                    label_image,
                    coalition_name,
                    screen_anchor,
                    territory_label_font_pixels(region.area, scale),
                    containment_mask,
                    preferred_angle_degrees=territory_label_fallback_angle(
                        [screen_point(point) for point in region.label_path]
                    ),
                ) or painted_label
            if painted_label:
                self.map_territory_label_photo = ImageTk.PhotoImage(label_image)
                canvas.create_image(
                    0,
                    0,
                    image=self.map_territory_label_photo,
                    anchor="nw",
                    tags=("map-world", "map-territory-label"),
                    state="disabled",
                )

        self.map_zoom_label.configure(text=f"{self.map_zoom * 100:.0f}%")

    def _select_map_canvas_system(self, name: str) -> None:
        if hasattr(self, "map_result_tree"):
            self.map_result_tree.selection_remove(self.map_result_tree.selection())
        self.map_selected_system = name
        system = next((entry for entry in (self.data.get("map") or {}).get("systems", []) if str(entry.get("name") or "").casefold() == name.casefold()), None)
        if system:
            self._render_map_system_detail(system)
        self._draw_map()

    def _focus_map_system(self, name: str) -> None:
        if not name:
            return
        system = next((entry for entry in (self.data.get("map") or {}).get("systems", []) if str(entry.get("name") or "").casefold() == name.casefold()), None)
        if not system:
            return
        self.map_selected_system = str(system.get("name") or name)
        self._draw_map()
        if system.get("hasPosition") and hasattr(self, "map_world_center") and hasattr(self, "map_fit_scale"):
            self.map_pan_x = -(float(system.get("x") or 0) - self.map_world_center[0]) * self.map_fit_scale * self.map_zoom
            self.map_pan_y = -(float(system.get("y") or 0) - self.map_world_center[1]) * self.map_fit_scale * self.map_zoom
            self._draw_map()

    def _fit_map_view(self) -> None:
        self.map_zoom = 1.0
        self.map_pan_x = 0.0
        self.map_pan_y = 0.0
        self._draw_map()

    def _change_map_zoom(self, factor: float) -> None:
        width = max(2, self.map_canvas.winfo_width())
        height = max(2, self.map_canvas.winfo_height())
        self._zoom_map_at(factor, width / 2.0, height / 2.0)

    def _map_mousewheel(self, event) -> str:
        factor = 1.18 if event.delta > 0 else 1 / 1.18
        self._zoom_map_at(factor, event.x, event.y)
        return "break"

    def _zoom_map_at(self, factor: float, cursor_x: float, cursor_y: float) -> None:
        old_zoom = self.map_zoom
        new_zoom = min(MAP_ZOOM_MAX, max(MAP_ZOOM_MIN, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 0.0001:
            return
        width = max(2, self.map_canvas.winfo_width())
        height = max(2, self.map_canvas.winfo_height())
        scale = new_zoom / old_zoom
        self.map_pan_x = cursor_x - width / 2.0 - (cursor_x - width / 2.0 - self.map_pan_x) * scale
        self.map_pan_y = cursor_y - height / 2.0 - (cursor_y - height / 2.0 - self.map_pan_y) * scale
        self.map_zoom = new_zoom
        self.map_zoom_label.configure(text=f"{self.map_zoom * 100:.0f}%")
        if self.map_zoom_redraw_after is None:
            self.map_zoom_redraw_after = self.root.after(
                16,
                self._redraw_map_after_zoom,
            )

    def _redraw_map_after_zoom(self) -> None:
        self.map_zoom_redraw_after = None
        self._draw_map()

    def _map_pan_start(self, event) -> None:
        current = self.map_canvas.find_withtag("current")
        if current and "map-node" in self.map_canvas.gettags(current[0]):
            self.map_drag_origin = None
            return
        self.map_drag_origin = (event.x, event.y)

    def _map_pan_move(self, event) -> None:
        if self.map_drag_origin is None:
            return
        old_x, old_y = self.map_drag_origin
        dx, dy = event.x - old_x, event.y - old_y
        self.map_pan_x += dx
        self.map_pan_y += dy
        self.map_canvas.move("map-world", dx, dy)
        self.map_drag_origin = (event.x, event.y)

        # Every visible map item moves immediately with the cursor.  A full
        # canvas rebuild while the button is held blocks later motion events,
        # so refresh culled off-screen content once when the drag completes.

    def _map_pan_end(self, _event=None) -> None:
        self.map_drag_origin = None
        self._draw_map()

    def _restore_scan_table_layout(self) -> None:
        saved_layouts = self.user_state.load().get("tableLayouts", {})
        has_saved_layout = isinstance(saved_layouts, dict) and "scans" in saved_layouts
        layout = self.user_state.table_layout("scans")
        if has_saved_layout:
            columns = [column for column in layout.get("columns", []) if column in SCAN_COLUMN_SPECS]
            if columns:
                self.scan_display_order = columns
                selected = set(columns)
                for key, variable in self.scan_column_vars.items():
                    variable.set(key in selected)

            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.scan_tree.column("#0", width=widths["name"])
                for key in SCAN_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.scan_tree.column(key, width=widths[key])

            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in SCAN_COLUMN_SPECS:
                self.scan_sort_column = sort_column
                self.scan_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.scan_pending_xview = xview
        self._refresh_scan_table_columns()
        self._refresh_scan_table_headings()

    def _save_scan_table_layout(self) -> None:
        if not hasattr(self, "scan_tree") or not self.scan_tree.winfo_exists():
            return
        displayed = list(self.scan_tree.tk.splitlist(self.scan_tree.cget("displaycolumns")))
        widths = {"name": int(self.scan_tree.column("#0", "width"))}
        widths.update({key: int(self.scan_tree.column(key, "width")) for key in SCAN_COLUMN_SPECS})
        xview = self.scan_tree.xview()
        self.user_state.set_table_layout(
            "scans",
            columns=displayed,
            widths=widths,
            sort_column=self.scan_sort_column,
            sort_descending=self.scan_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_scan_table_columns(self) -> None:
        if not hasattr(self, "scan_tree"):
            return
        visible = [
            key for key in self.scan_display_order
            if key in SCAN_COLUMN_SPECS and self.scan_column_vars[key].get()
        ]
        visible.extend(
            key for key in SCAN_COLUMN_SPECS
            if self.scan_column_vars[key].get() and key not in visible
        )
        self.scan_display_order = list(visible)
        self.scan_tree.configure(displaycolumns=visible)

    def _apply_scan_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.scan_display_order = [column for column in columns if column in SCAN_COLUMN_SPECS]
        for key, variable in self.scan_column_vars.items():
            variable.set(key in selected)
        self._refresh_scan_table_columns()

    def _refresh_scan_table_headings(self) -> None:
        if not hasattr(self, "scan_tree"):
            return
        arrow = " ▼" if self.scan_sort_desc else " ▲"
        self.scan_tree.heading(
            "#0",
            text="PLANET" + (arrow if self.scan_sort_column == "name" else ""),
            anchor="w",
            command=lambda: self._sort_scans_by("name"),
        )
        for key, spec in SCAN_COLUMN_SPECS.items():
            self.scan_tree.heading(
                key,
                text=spec["label"] + (arrow if self.scan_sort_column == key else ""),
                anchor=spec["anchor"],
                command=lambda column=key: self._sort_scans_by(column),
            )

    def _sort_scans_by(self, column: str) -> None:
        if self.scan_sort_column == column:
            self.scan_sort_desc = not self.scan_sort_desc
        else:
            self.scan_sort_column = column
            self.scan_sort_desc = bool(SCAN_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_scan_table_headings()
        self._populate_scans()

    def _sort_scan_rows(self, rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        sortable: list[tuple[Any, str, dict[str, Any], dict[str, Any]]] = []
        missing: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for scan, annotation in rows:
            personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
            value = scan_column_sort_value(scan, self.scan_sort_column, annotation, personal)
            if value is None:
                missing.append((scan, annotation))
            else:
                sortable.append((value, str(scan.get("planet_name") or "").casefold(), scan, annotation))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.scan_sort_desc)
        missing.sort(key=lambda row: str(row[0].get("planet_name") or "").casefold())
        return [(row[2], row[3]) for row in sortable] + missing

    def _show_scan_column_menu(self, event) -> str:
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=PANEL_2,
            fg=TEXT,
            activebackground=PANEL_3,
            activeforeground=CYAN,
            selectcolor=CYAN,
        )
        for label, columns in SCAN_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_scan_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_scan_column_preset(tuple(SCAN_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in SCAN_COLUMN_SPECS.items():
            menu.add_checkbutton(
                label=spec["label"].title(),
                variable=self.scan_column_vars[key],
                command=self._refresh_scan_table_columns,
            )
        self.scan_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_scan_context_menu(self, event) -> str | None:
        if self.scan_tree.identify_region(event.x, event.y) == "heading":
            return self._show_scan_column_menu(event)
        scan_id = self.scan_tree.identify_row(event.y)
        if not scan_id:
            return None
        self.scan_tree.selection_set(scan_id)
        self.scan_tree.focus(scan_id)
        self._show_selected_scan()
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Show on Map", command=self._show_scan_on_map)
        menu.add_command(label="Organize / Add Notes", command=self._organize_selected_scan)
        menu.add_command(label="Copy Planet Details", command=self._copy_selected_scan)
        menu.add_separator()
        menu.add_command(label="Toggle Base Record", command=self._toggle_selected_scan_base)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _reset_scan_filters(self) -> None:
        self.scan_search_var.set("")
        self.scan_system_filter_var.set("All systems")
        self.scan_type_filter_var.set("All types")
        self.scan_quality_filter_var.set("All colony ratings")
        self.scan_base_filter_var.set("All base records")
        self._populate_scans()

    def _toggle_selected_scan_base(self) -> None:
        scan = self._selected_scan()
        if not scan:
            return
        annotation = self.user_state.scan_annotation(scan)
        enabled = not annotation.get("hasBase")
        count = max(1, int(annotation.get("baseCount") or 0)) if enabled else 0
        try:
            self.user_state.set_scan_annotation(
                scan,
                system_name=self._scan_system_name(scan),
                has_base=enabled,
                base_count=count,
            )
        except OSError as error:
            messagebox.showerror("Could not save base record", str(error), parent=self.root)
            return
        self._populate_scans()

    def _copy_selected_scan(self) -> None:
        scan = self._selected_scan()
        if not scan:
            return
        annotation = self.user_state.scan_annotation(scan)
        quality_label, quality_score = self._scan_quality(scan)
        lines = [
            str(scan.get("planet_name") or "Unknown planet"),
            f"System: {self._scan_system_name(scan) or 'Location unknown'}",
            f"Type: {scan.get('planet_type') or 'Unknown'}",
            f"Colony rating: {quality_label}" + (f" ({quality_score:.1f})" if quality_score is not None else ""),
            f"Bases: {annotation.get('baseCount', 0) if annotation.get('hasBase') else 0}",
            f"Resources: {self._scan_best_resources(scan, limit=99)}",
            f"Last scanned: {scan.get('observedAt') or 'Unknown'}",
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status_var.set(f"Copied planet details for {scan.get('planet_name') or 'planet'}")

    def _populate_scans(self) -> None:
        scans = [scan for scan in self.data.get("scans", []) if isinstance(scan, dict)]
        previous = self.scan_tree.selection()
        previous_id = previous[0] if previous else ""
        systems = sorted({self._scan_system_name(scan) for scan in scans if self._scan_system_name(scan)}, key=str.casefold)
        planet_types = sorted({str(scan.get("planet_type") or "Unknown") for scan in scans}, key=str.casefold)
        quality_labels = sorted({self._scan_quality(scan)[0] for scan in scans}, key=self._scan_quality_order)
        filter_specs = (
            (self.scan_system_filter_combo, self.scan_system_filter_var, ["All systems", *systems]),
            (self.scan_type_filter_combo, self.scan_type_filter_var, ["All types", *planet_types]),
            (self.scan_quality_filter_combo, self.scan_quality_filter_var, ["All colony ratings", *quality_labels]),
            (self.scan_base_filter_combo, self.scan_base_filter_var, ["All base records", "Has bases", "No base recorded"]),
        )
        for combo, variable, values in filter_specs:
            combo.configure(values=values)
            if variable.get() not in values:
                variable.set(values[0])
        self.scan_system_combo.configure(
            values=sorted(
                {
                    str(system.get("name") or "").strip()
                    for system in (self.data.get("map") or {}).get("systems", [])
                    if str(system.get("name") or "").strip()
                },
                key=str.casefold,
            )
        )

        query = self.scan_search_var.get().strip()
        filtered: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for scan in scans:
            annotation = self.user_state.scan_annotation(scan)
            personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
            system_name = annotation.get("systemName") or scan.get("system_name") or ""
            quality_label, _quality_index = self._scan_quality(scan)
            planet_type = str(scan.get("planet_type") or "Unknown")
            if self.scan_system_filter_var.get() != "All systems" and system_name != self.scan_system_filter_var.get():
                continue
            if self.scan_type_filter_var.get() != "All types" and planet_type != self.scan_type_filter_var.get():
                continue
            if self.scan_quality_filter_var.get() != "All colony ratings" and quality_label != self.scan_quality_filter_var.get():
                continue
            if self.scan_base_filter_var.get() == "Has bases" and not annotation.get("hasBase"):
                continue
            if self.scan_base_filter_var.get() == "No base recorded" and annotation.get("hasBase"):
                continue
            if not scan_matches_query(scan, annotation, query, personal):
                continue
            filtered.append((scan, annotation))

        filtered = self._sort_scan_rows(filtered)
        self.scan_tree.delete(*self.scan_tree.get_children())
        for scan, annotation in filtered:
            personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
            scan_id = scan_annotation_key(scan)
            self.scan_tree.insert(
                "",
                "end",
                iid=scan_id,
                text=scan.get("planet_name") or "Unknown planet",
                values=tuple(scan_column_display_value(scan, column, annotation, personal) for column in SCAN_COLUMN_SPECS),
            )
        if self.scan_pending_xview is not None:
            xview = self.scan_pending_xview
            self.scan_pending_xview = None
            self.root.after_idle(
                lambda fraction=xview: self.scan_tree.xview_moveto(fraction)
                if self.scan_tree.winfo_exists()
                else None
            )
        self.scan_result_label.configure(text=f"{len(filtered):,} / {len(scans):,} PLANETS")
        children = self.scan_tree.get_children()
        if children:
            chosen = previous_id if previous_id in children else children[0]
            self.scan_tree.selection_set(chosen)
            self.scan_tree.focus(chosen)
            self.scan_tree.see(chosen)
            self._show_selected_scan()
        else:
            self.current_scan_photo = None
            self.scan_art_label.configure(image="", text="NO MATCHING PLANET SCANS")
            self.scan_name_label.configure(text="Planet scan details")
            self.scan_system_var.set("")
            self.scan_has_base_var.set(False)
            self.scan_base_count_var.set("0")
            self._set_text(self.scan_text, "Adjust the search or filters, or scan a planet in game and select Refresh Data.")

    def _scan_quality_order(self, label: str) -> int:
        order = {"Exceptional": 0, "Strong": 1, "Viable": 2, "Difficult": 3, "Hostile": 4, "Unknown": 5}
        return order.get(label, 99)

    def _scan_quality(self, scan: dict[str, Any]) -> tuple[str, float | None]:
        return scan_quality(scan)

    def _scan_best_resources(self, scan: dict[str, Any], limit: int = 3) -> str:
        return scan_best_resources(scan, limit)

    def _scan_system_name(self, scan: dict[str, Any]) -> str:
        annotation = self.user_state.scan_annotation(scan)
        return str(annotation.get("systemName") or scan.get("system_name") or "").strip()

    def _selected_scan(self) -> dict[str, Any] | None:
        selection = self.scan_tree.selection()
        if not selection:
            return None
        scan_id = selection[0]
        return next((entry for entry in self.data.get("scans", []) if scan_annotation_key(entry) == scan_id), None)

    def _scan_tree_click(self, event) -> None:
        if self.scan_tree.identify_region(event.x, event.y) != "cell":
            return
        display_index = self.scan_tree.identify_column(event.x)
        try:
            index = int(display_index.removeprefix("#")) - 1
        except ValueError:
            return
        displayed = list(self.scan_tree.tk.splitlist(self.scan_tree.cget("displaycolumns")))
        if index < 0 or index >= len(displayed) or displayed[index] != "base":
            return
        scan_id = self.scan_tree.identify_row(event.y)
        scan = next((entry for entry in self.data.get("scans", []) if scan_annotation_key(entry) == scan_id), None)
        if not scan:
            return
        self.scan_tree.selection_set(scan_id)
        self._toggle_selected_scan_base()

    def _sync_scan_base_controls(self) -> None:
        try:
            count = max(0, int(self.scan_base_count_var.get() or 0))
        except ValueError:
            count = 0
        if self.scan_has_base_var.get() and count == 0:
            self.scan_base_count_var.set("1")
        elif not self.scan_has_base_var.get():
            self.scan_base_count_var.set("0")

    def _save_scan_annotation(self) -> None:
        scan = self._selected_scan()
        if not scan:
            return
        try:
            count = max(0, int(self.scan_base_count_var.get() or 0))
        except ValueError:
            messagebox.showerror("Invalid base count", "Base count must be a whole number from 0 to 999.", parent=self.root)
            return
        count = min(999, count)
        has_base = self.scan_has_base_var.get() or count > 0
        if has_base and count == 0:
            count = 1
        if not has_base:
            count = 0
        try:
            self.user_state.set_scan_annotation(
                scan,
                system_name=self.scan_system_var.get(),
                has_base=has_base,
                base_count=count,
            )
        except OSError as error:
            messagebox.showerror("Could not save planet record", str(error), parent=self.root)
            return
        self.scan_base_count_var.set(str(count))
        self.scan_has_base_var.set(has_base)
        self.status_var.set(f"Saved local base record for {scan.get('planet_name') or 'planet'}")
        self._populate_scans()

    def _show_scan_on_map(self) -> None:
        scan = self._selected_scan()
        if not scan:
            return
        system_name = self._scan_system_name(scan)
        if not system_name:
            messagebox.showinfo("Planet location unknown", "Choose the planet's system and save it first.", parent=self.root)
            return
        self.show_page("map")
        self._focus_map_system(system_name)

    def _show_selected_scan(self, _event=None) -> None:
        scan = self._selected_scan()
        if not scan:
            return
        name = scan.get("planet_name") or "Unknown planet"
        system_name = self._scan_system_name(scan)
        annotation = self.user_state.scan_annotation(scan)
        personal = self.user_state.record_annotation("planet", scan_annotation_key(scan))
        quality_label, quality_index = self._scan_quality(scan)
        personal_flags = ""
        if personal.get("favorite"):
            personal_flags += "  ·  ★"
        if personal.get("watchlist"):
            personal_flags += "  ·  WATCH"
        if personal.get("category"):
            personal_flags += f"  ·  {personal.get('category')}"
        self.scan_name_label.configure(text=f"{name}  ·  {quality_label}{personal_flags}")
        self.scan_system_var.set(system_name)
        self.scan_has_base_var.set(bool(annotation.get("hasBase")))
        self.scan_base_count_var.set(str(annotation.get("baseCount") or 0))
        scan_art = scan.get("art")
        if scan_art:
            item = {
                "name": name,
                "type": scan.get("planet_type", "planet"),
                "category": "planet",
                "colour": [63, 151, 224],
                "art": scan_art,
            }
        else:
            item = {
                "name": name,
                "type": scan.get("planet_type", "planet"),
                "category": "planet",
                "colour": [63, 151, 224],
                "art": None,
            }
        image = self._load_item_image(item, (470, 205))
        self.current_scan_photo = ImageTk.PhotoImage(image)
        self.scan_art_label.configure(image=self.current_scan_photo, text="")

        self.scan_text.configure(state="normal")
        self.scan_text.delete("1.0", "end")
        if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
            self.scan_text.insert("end", "MY INTEL\n", "section")
            self._insert_pair(self.scan_text, "Category", str(personal.get("category") or "—"))
            self._insert_pair(self.scan_text, "Tags", ", ".join(personal.get("tags", [])) or "—")
            if personal.get("note"):
                self.scan_text.insert("end", str(personal.get("note")) + "\n", "value")
            self.scan_text.insert("end", "\n", "value")
        self.scan_text.insert("end", "LOCATION & BASE\n", "section")
        self._insert_pair(self.scan_text, "System", system_name or "Location unknown")
        self._insert_pair(self.scan_text, "Base record", f"Yes · {annotation.get('baseCount', 0)} base(s)" if annotation.get("hasBase") else "No base recorded")

        self.scan_text.insert("end", "\nCOLONY SUITABILITY\n", "section")
        score_text = f"{quality_index:.1f} / 100" if quality_index is not None else "Not reported"
        self._insert_pair(self.scan_text, "Rating", f"{quality_label} · {score_text}")
        colonization = scan.get("colonization") if isinstance(scan.get("colonization"), list) else []
        if colonization:
            for row in colonization:
                if not isinstance(row, dict):
                    continue
                category = str(row.get("category_label") or "Environment")
                setting = str(row.get("setting_label") or "Unknown")
                penalty = format_number(row.get("penalty_pct"), "0")
                tag = "good" if not row.get("penalty_pct") else "warning" if float(row.get("penalty_pct") or 0) < 50 else "bad"
                self.scan_text.insert("end", f"{category}: ", "label")
                self.scan_text.insert("end", f"{setting}  ·  {penalty}% penalty\n", tag)
        else:
            self.scan_text.insert("end", "No environmental assessment captured.\n", "label")

        self.scan_text.insert("end", "\nRESOURCE YIELDS\n", "section")
        resources = scan.get("resources") if isinstance(scan.get("resources"), dict) else {}
        positive_resources = []
        for resource_name, value in resources.items():
            try:
                amount = float(value)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                positive_resources.append((amount, str(resource_name).replace("_", " ").title()))
        positive_resources.sort(key=lambda row: (-row[0], row[1].casefold()))
        if positive_resources:
            for amount, resource_name in positive_resources:
                self._insert_pair(self.scan_text, resource_name, format_number(amount))
        else:
            self.scan_text.insert("end", "No positive resource yields reported.\n", "label")

        self.scan_text.insert("end", "\nRECOMMENDED EXTRACTORS\n", "section")
        extractors = scan.get("extractors") if isinstance(scan.get("extractors"), dict) else {}
        if extractors:
            for extractor_name, value in sorted(extractors.items(), key=lambda pair: (-float(pair[1] or 0), str(pair[0]).casefold())):
                self._insert_pair(self.scan_text, str(extractor_name).replace("_", " ").title(), format_number(value))
        else:
            self.scan_text.insert("end", "No extractor recommendations captured.\n", "label")

        self.scan_text.insert("end", "\nSCAN METADATA\n", "section")
        self._insert_pair(self.scan_text, "Planet type", str(scan.get("planet_type") or "Unknown"))
        self._insert_pair(self.scan_text, "Planet ID", str(scan.get("planet_id") or "-"))
        self._insert_pair(self.scan_text, "Scan range", f"{format_number(scan.get('scan_range'))} u")
        self._insert_pair(self.scan_text, "Last scanned", str(scan.get("observedAt") or "Unknown"))
        self.scan_text.configure(state="disabled")
        self.scan_text.yview_moveto(0)

    def _restore_player_skill_table_layout(self) -> None:
        saved = self.user_state.load().get("tableLayouts", {})
        has_layout = isinstance(saved, dict) and "player_skills" in saved
        layout = self.user_state.table_layout("player_skills")
        if has_layout:
            columns = [column for column in layout.get("columns", []) if column in PLAYER_SKILL_COLUMN_SPECS]
            if columns:
                self.player_skill_display_order = columns
                selected = set(columns)
                for key, variable in self.player_skill_column_vars.items():
                    variable.set(key in selected)
            widths = layout.get("widths", {})
            if isinstance(widths, dict):
                if isinstance(widths.get("name"), int):
                    self.player_skill_tree.column("#0", width=widths["name"])
                for key in PLAYER_SKILL_COLUMN_SPECS:
                    if isinstance(widths.get(key), int):
                        self.player_skill_tree.column(key, width=widths[key])
            sort_column = str(layout.get("sortColumn") or "")
            if sort_column == "name" or sort_column in PLAYER_SKILL_COLUMN_SPECS:
                self.player_skill_sort_column = sort_column
                self.player_skill_sort_desc = bool(layout.get("sortDescending"))
            xview = float(layout.get("xview") or 0.0)
            if xview > 0:
                self.player_skill_pending_xview = xview
        self._refresh_player_skill_columns()
        self._refresh_player_skill_headings()

    def _save_player_skill_table_layout(self) -> None:
        if not hasattr(self, "player_skill_tree") or not self.player_skill_tree.winfo_exists():
            return
        displayed = list(self.player_skill_tree.tk.splitlist(self.player_skill_tree.cget("displaycolumns")))
        widths = {"name": int(self.player_skill_tree.column("#0", "width"))}
        widths.update({key: int(self.player_skill_tree.column(key, "width")) for key in PLAYER_SKILL_COLUMN_SPECS})
        xview = self.player_skill_tree.xview()
        self.user_state.set_table_layout(
            "player_skills",
            columns=displayed,
            widths=widths,
            sort_column=self.player_skill_sort_column,
            sort_descending=self.player_skill_sort_desc,
            xview=float(xview[0]) if xview else 0.0,
        )

    def _refresh_player_skill_columns(self) -> None:
        visible = [key for key in self.player_skill_display_order if key in PLAYER_SKILL_COLUMN_SPECS and self.player_skill_column_vars[key].get()]
        visible.extend(key for key in PLAYER_SKILL_COLUMN_SPECS if self.player_skill_column_vars[key].get() and key not in visible)
        self.player_skill_display_order = visible
        self.player_skill_tree.configure(displaycolumns=visible)

    def _apply_player_skill_column_preset(self, columns: tuple[str, ...]) -> None:
        selected = set(columns)
        self.player_skill_display_order = [column for column in columns if column in PLAYER_SKILL_COLUMN_SPECS]
        for key, variable in self.player_skill_column_vars.items():
            variable.set(key in selected)
        self._refresh_player_skill_columns()

    def _refresh_player_skill_headings(self) -> None:
        arrow = " ▼" if self.player_skill_sort_desc else " ▲"
        self.player_skill_tree.heading("#0", text="SKILL" + (arrow if self.player_skill_sort_column == "name" else ""), anchor="w", command=lambda: self._sort_player_skills_by("name"))
        for key, spec in PLAYER_SKILL_COLUMN_SPECS.items():
            self.player_skill_tree.heading(key, text=spec["label"] + (arrow if self.player_skill_sort_column == key else ""), anchor=spec["anchor"], command=lambda column=key: self._sort_player_skills_by(column))

    def _sort_player_skills_by(self, column: str) -> None:
        if self.player_skill_sort_column == column:
            self.player_skill_sort_desc = not self.player_skill_sort_desc
        else:
            self.player_skill_sort_column = column
            self.player_skill_sort_desc = bool(PLAYER_SKILL_COLUMN_SPECS.get(column, {}).get("first_desc"))
        self._refresh_player_skill_headings()
        self._populate_player()

    def _sort_player_skill_rows(self, rows: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, dict[str, Any]]]:
        sortable: list[tuple[Any, str, int, dict[str, Any]]] = []
        missing: list[tuple[int, dict[str, Any]]] = []
        for index, skill in rows:
            personal = self.user_state.record_annotation("skill", skill.get("skill_id"))
            value = player_skill_column_sort_value(skill, self.player_skill_sort_column, personal)
            if value is None:
                missing.append((index, skill))
            else:
                sortable.append((value, str(skill.get("display_name") or skill.get("skill_id") or "").casefold(), index, skill))
        sortable.sort(key=lambda row: (row[0], row[1]), reverse=self.player_skill_sort_desc)
        missing.sort(key=lambda row: str(row[1].get("display_name") or row[1].get("skill_id") or "").casefold())
        return [(row[2], row[3]) for row in sortable] + missing

    def _player_skill_display_value(self, skill: dict[str, Any], column: str, personal: dict[str, Any]) -> str:
        if column in PERSONAL_COLUMN_SPECS:
            return personal_column_display_value(personal, column)
        if column == "rank":
            maximum = skill.get("max_level")
            return f"{format_number(skill.get('level'), '0')} / {format_number(maximum)}" if maximum not in (None, "") else format_number(skill.get("level"), "0")
        if column == "maximum":
            return format_number(skill.get("max_level"))
        if column == "cost":
            return format_number(skill.get("cost_paid"), "0")
        if column == "next_sp":
            return format_number(skill.get("next_cost"))
        if column == "next_credits":
            value = skill.get("next_credit_cost")
            return f"{format_number(value, '0')} cr" if fitting.number(value) > 0 else "-"
        if column == "bonus":
            return self._skill_bonus_text(skill)
        return "-"

    def _show_player_skill_column_menu(self, event) -> str:
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN, selectcolor=CYAN)
        for label, columns in PLAYER_SKILL_COLUMN_PRESETS.items():
            menu.add_command(label=f"{label.upper()} COLUMNS", command=lambda values=columns: self._apply_player_skill_column_preset(values))
        menu.add_command(label="SHOW ALL COLUMNS", command=lambda: self._apply_player_skill_column_preset(tuple(PLAYER_SKILL_COLUMN_SPECS)))
        menu.add_separator()
        for key, spec in PLAYER_SKILL_COLUMN_SPECS.items():
            menu.add_checkbutton(label=spec["label"].title(), variable=self.player_skill_column_vars[key], command=self._refresh_player_skill_columns)
        self.player_skill_column_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_player_skill_context_menu(self, event) -> str | None:
        if self.player_skill_tree.identify_region(event.x, event.y) == "heading":
            return self._show_player_skill_column_menu(event)
        iid = self.player_skill_tree.identify_row(event.y)
        if not iid:
            return None
        self.player_skill_tree.selection_set(iid)
        self.player_skill_tree.focus(iid)
        self._show_selected_player_skill()
        menu = tk.Menu(self.root, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground=CYAN)
        menu.add_command(label="Find NPC Trainer", command=self._find_selected_skill_trainer)
        menu.add_command(label="Organize Skill / Add Notes", command=self._organize_selected_player_skill)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _organize_selected_player_skill(self) -> None:
        skill = self._selected_player_skill()
        if skill:
            self._organize_record("skill", skill.get("skill_id"), str(skill.get("display_name") or skill.get("skill_id") or "Skill"), self._refresh_skill_metadata_views)

    def _reset_player_skill_filter(self) -> None:
        self.player_skill_search_var.set("")
        self._populate_player()

    def _find_selected_skill_trainer(self) -> None:
        skill = self._selected_player_skill()
        if not skill:
            return
        self.training_search_var.set(str(skill.get("display_name") or skill.get("skill_id") or ""))
        self.training_system_var.set("All systems")
        self.training_station_var.set("All stations")
        self.training_status_var.set("All offers")
        self._populate_skill_finder()
        self.show_page("training")

    def _populate_player(self) -> None:
        player = self.data.get("player") or {}
        xp = player.get("xp") if isinstance(player.get("xp"), dict) else {}
        current = xp.get("current") or 0
        needed = xp.get("needed") or 0
        enemy_kills = xp.get("enemyKills") or 0
        player_kills = xp.get("playerKills") or 0

        values = {
            "credits": format_number(player.get("credits")),
            "level": format_number(xp.get("level")),
            "xp": f"{format_number(current)} / {format_number(needed)}" if needed else "-",
            "points": format_number(xp.get("skillPoints")),
            "kills": f"{format_number(enemy_kills)} / {format_number(player_kills)}",
            "playtime": self._format_duration(xp.get("playSeconds")),
        }
        for key, label in self.player_metric_labels.items():
            label.configure(text=values[key], fg=TEXT if player.get("hasData") else MUTED)

        self.root.update_idletasks()
        width = max(1, self.player_xp_canvas.winfo_width())
        self.player_xp_canvas.delete("all")
        self.player_xp_canvas.create_rectangle(0, 2, width, 16, fill=BG, outline=LINE)
        progress = min(1.0, max(0.0, float(current) / float(needed))) if needed else 0.0
        if progress:
            self.player_xp_canvas.create_rectangle(1, 3, max(2, int((width - 2) * progress)), 15, fill=BLUE, outline="")
        caption = f"LEVEL PROGRESS  {progress * 100:.1f}%" if needed else "AWAITING XP SNAPSHOT"
        self.player_xp_canvas.create_text(8, 9, text=caption, fill=TEXT if needed else MUTED, anchor="w", font=("Cascadia Mono", 7, "bold"))

        previous_selection = self.player_skill_tree.selection()
        previous_id = previous_selection[0] if previous_selection else ""
        self.player_skill_tree.delete(*self.player_skill_tree.get_children())
        raw_skills = player.get("skills") if isinstance(player.get("skills"), list) else []
        self.player_skills = [skill if isinstance(skill, dict) else {} for skill in raw_skills]
        query = self.player_skill_search_var.get().strip()
        filtered = []
        for index, skill in enumerate(self.player_skills):
            personal = self.user_state.record_annotation("skill", skill.get("skill_id"))
            if player_skill_matches_query(skill, query, personal):
                filtered.append((index, skill))
        filtered = self._sort_player_skill_rows(filtered)
        for index, skill in filtered:
            name = str(skill.get("display_name") or skill.get("name") or skill.get("skill_id") or f"Skill {index + 1}")
            personal = self.user_state.record_annotation("skill", skill.get("skill_id"))
            self.player_skill_tree.insert(
                "",
                "end",
                iid=f"skill-{index}",
                text=name,
                values=tuple(self._player_skill_display_value(skill, column, personal) for column in PLAYER_SKILL_COLUMN_SPECS),
            )
        self.player_skill_result_var.set(f"{len(filtered):,} / {len(self.player_skills):,}")
        if self.player_skill_pending_xview is not None:
            xview = self.player_skill_pending_xview
            self.player_skill_pending_xview = None
            self.root.after_idle(lambda fraction=xview: self.player_skill_tree.xview_moveto(fraction) if self.player_skill_tree.winfo_exists() else None)
        available_ids = self.player_skill_tree.get_children()
        chosen_id = previous_id if previous_id in available_ids else next(
            (f"skill-{index}" for index, skill in enumerate(self.player_skills) if fitting.number(skill.get("level")) > 0 and f"skill-{index}" in available_ids),
            available_ids[0] if available_ids else "",
        )
        if chosen_id:
            self.player_skill_tree.selection_set(chosen_id)
            self.player_skill_tree.focus(chosen_id)
            self.player_skill_tree.see(chosen_id)
        self._render_player_summary()

    def _show_selected_player_skill(self, _event=None) -> None:
        self._render_player_summary()

    def _selected_player_skill(self) -> dict[str, Any] | None:
        selection = self.player_skill_tree.selection()
        if not selection or not selection[0].startswith("skill-"):
            return None
        try:
            index = int(selection[0].split("-", 1)[1])
        except (TypeError, ValueError):
            return None
        return self.player_skills[index] if 0 <= index < len(self.player_skills) else None

    def _render_player_summary(self) -> None:
        player = self.data.get("player") or {}
        widget = self.player_summary_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if not player.get("hasData"):
            widget.insert(
                "end",
                "No player snapshot has been collected yet.\n\n"
                "Install or repair the Game Link, start the game, then select Refresh Data. "
                "Credits, XP and skills arrive naturally during login and play.",
                "value",
            )
            widget.configure(state="disabled")
            return

        skill = self._selected_player_skill()
        if skill:
            name = str(skill.get("display_name") or skill.get("name") or skill.get("skill_id") or "Selected Skill")
            personal = self.user_state.record_annotation("skill", skill.get("skill_id"))
            level = fitting.number(skill.get("level"))
            maximum = skill.get("max_level")
            rank = f"{format_number(level, '0')} / {format_number(maximum)}" if maximum not in (None, "") else format_number(level, "0")
            widget.insert("end", "SELECTED SKILL\n", "section")
            widget.insert("end", name + "\n", "good")
            self._insert_pair(widget, "Rank", rank)
            self._insert_pair(widget, "Points spent", format_number(skill.get("cost_paid"), "0"))
            if any((personal.get("favorite"), personal.get("watchlist"), personal.get("category"), personal.get("tags"), personal.get("note"))):
                widget.insert("end", "\nMY INTEL\n", "section")
                self._insert_pair(widget, "Category", str(personal.get("category") or "—"))
                self._insert_pair(widget, "Tags", ", ".join(personal.get("tags", [])) or "—")
                if personal.get("note"):
                    widget.insert("end", str(personal.get("note")) + "\n", "value")

            current_effects = self._skill_effect_entries(skill, current_rank=True)
            widget.insert("end", f"\nCURRENT EFFECTS — RANK {format_number(level, '0')}\n", "section")
            if current_effects:
                for effect in current_effects:
                    widget.insert("end", f"• {effect}\n", "value")
            else:
                widget.insert("end", "Not active at rank 0.\n", "label")

            per_rank = self._skill_effect_entries(skill, current_rank=False)
            if per_rank:
                widget.insert("end", "\nEACH ADDITIONAL RANK\n", "section")
                for effect in per_rank:
                    widget.insert("end", f"• {effect}\n", "value")
            description = str(skill.get("description") or "").strip()
            if description:
                widget.insert("end", "\nDESCRIPTION\n", "section")
                widget.insert("end", description + "\n", "value")

        widget.insert("end", "\nACCOUNT SNAPSHOT\n", "section")
        self._insert_pair(widget, "Observed", str(player.get("observedAt") or "Unknown"))
        self._insert_pair(widget, "Skills listed", format_number(len(self.player_skills), "0"))
        self._insert_pair(widget, "Total points spent", format_number(sum(int(skill.get("cost_paid", 0) or 0) for skill in self.player_skills), "0"))
        breakdown = player.get("creditBreakdown")
        if isinstance(breakdown, list) and breakdown:
            widget.insert("end", "\nCREDIT BREAKDOWN\n", "section")
            for entry in breakdown:
                if isinstance(entry, dict):
                    label = str(entry.get("source") or entry.get("label") or entry.get("reason") or "Entry")
                    amount = entry.get("amount", entry.get("credits", entry.get("value")))
                    self._insert_pair(widget, label, format_number(amount))
        mastery = player.get("masteryTree")
        widget.insert("end", "\nMASTERY TREE\n", "section")
        mastery_lines = self._mastery_lines(mastery)
        widget.insert("end", "\n".join(mastery_lines) if mastery_lines else "No mastery tree returned by this server.", "value")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _populate_saved_fit_choices(self, select_id: str | None = None) -> None:
        rows = self.user_state.saved_fittings()
        self.saved_fit_by_label = {}
        labels = ["Unsaved fit"]
        used: set[str] = set()
        selected_label = "Unsaved fit"
        for row in rows:
            label = str(row.get("name") or "Saved fit")
            if label in used:
                label = f"{label} [{str(row.get('id') or '')[:6]}]"
            used.add(label)
            labels.append(label)
            self.saved_fit_by_label[label] = str(row.get("id") or "")
            if select_id and row.get("id") == select_id:
                selected_label = label
        self.saved_fit_combo.configure(values=labels)
        if select_id:
            self.current_saved_fit_id = select_id
            self.saved_fit_var.set(selected_label)
        elif self.saved_fit_var.get() not in labels:
            self.saved_fit_var.set("Unsaved fit")
            self.current_saved_fit_id = None

    def _saved_fit_state(self) -> dict[str, Any]:
        state = self._clone_fit_state(self.fit_state)
        state["applySkills"] = self.fit_apply_skills_var.get()
        return state

    def _resolve_saved_fit_item(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        identifier = str(value.get("id") or "")
        if identifier and identifier in self.item_by_id:
            return self.item_by_id[identifier]
        found = self._catalog_item(str(value.get("category") or "equipment"), value.get("type"), value.get("name"))
        if found:
            return found
        return {
            "id": identifier or f"saved:{value.get('category') or 'equipment'}:{app._normalise(value.get('name'))}",
            "type": value.get("type") or app._normalise(value.get("name")),
            "category": value.get("category") or "equipment",
            "categoryLabel": str(value.get("category") or "equipment").replace("_", " ").title(),
            "name": value.get("name") or "Saved item",
            "description": "Recovered from a locally saved fitting; revisit a shop if its catalog entry has changed.",
            "tech": value.get("tech"),
            "cargoSize": value.get("cargoSize"),
            "stats": value.get("stats") if isinstance(value.get("stats"), dict) else {},
            "markets": [],
            "art": None,
        }

    def _fit_state_from_saved(self, value: dict[str, Any]) -> dict[str, Any]:
        weapons = [self._resolve_saved_fit_item(item) for item in value.get("weaponSlots", []) if item is None or isinstance(item, dict)]
        plugins = [self._resolve_saved_fit_item(item) for item in value.get("pluginSlotsList", []) if item is None or isinstance(item, dict)]
        auxiliary = []
        for entry in value.get("aux", []) if isinstance(value.get("aux"), list) else []:
            if isinstance(entry, dict):
                auxiliary.append(
                    {
                        "slot": str(entry.get("slot") or "auxiliary"),
                        "category": str(entry.get("category") or "equipment"),
                        "item": self._resolve_saved_fit_item(entry.get("item")),
                        "locked": bool(entry.get("locked")),
                    }
                )
        max_weapons = max(int(value.get("maxWeaponSlots") or 0), len(weapons))
        plugin_slots = max(int(value.get("pluginSlots") or 0), len(plugins))
        weapons.extend([None] * (max_weapons - len(weapons)))
        plugins.extend([None] * (plugin_slots - len(plugins)))
        return {
            "hull": self._resolve_saved_fit_item(value.get("hull")),
            "engine": self._resolve_saved_fit_item(value.get("engine")),
            "shield": self._resolve_saved_fit_item(value.get("shield")),
            "energy": self._resolve_saved_fit_item(value.get("energy")),
            "weaponSlots": weapons,
            "pluginSlotsList": plugins,
            "aux": auxiliary,
            "maxWeaponSlots": max_weapons,
            "pluginSlots": plugin_slots,
        }

    def _load_selected_saved_fit(self, _event=None) -> None:
        fitting_id = self.saved_fit_by_label.get(self.saved_fit_var.get())
        if not fitting_id:
            self.current_saved_fit_id = None
            return
        row = next((entry for entry in self.user_state.saved_fittings() if entry.get("id") == fitting_id), None)
        if not row:
            return
        self.current_saved_fit_id = fitting_id
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        self.fit_state = self._fit_state_from_saved(state)
        self.fit_apply_skills_var.set(bool(state.get("applySkills", True)))
        self._render_fit_tree()
        self._render_fit_simulation()
        self.status_var.set(f"Loaded saved fitting {row.get('name') or 'fit'}")

    def _save_fit_as(self) -> None:
        if not self.fit_state or not self.fit_state.get("hull"):
            messagebox.showinfo("Save Fitting", "Capture or select a hull before saving this fitting.", parent=self.root)
            return
        default_name = str((self.fit_state.get("hull") or {}).get("name") or "Ship") + " Fit"
        name = simpledialog.askstring("Save Fitting", "Name this local fitting:", initialvalue=default_name, parent=self.root)
        if not name or not name.strip():
            return
        try:
            saved = self.user_state.save_fitting(name.strip(), self._saved_fit_state())
        except OSError as error:
            messagebox.showerror("Could not save fitting", str(error), parent=self.root)
            return
        self._populate_saved_fit_choices(str(saved.get("id")))
        self.status_var.set(f"Saved fitting {saved.get('name')}")

    def _update_saved_fit(self) -> None:
        if not self.current_saved_fit_id:
            self._save_fit_as()
            return
        current = next((entry for entry in self.user_state.saved_fittings() if entry.get("id") == self.current_saved_fit_id), None)
        if not current:
            self._save_fit_as()
            return
        try:
            saved = self.user_state.save_fitting(str(current.get("name") or "Saved fit"), self._saved_fit_state(), self.current_saved_fit_id)
        except OSError as error:
            messagebox.showerror("Could not update fitting", str(error), parent=self.root)
            return
        self._populate_saved_fit_choices(self.current_saved_fit_id)
        self.status_var.set(f"Updated fitting {saved.get('name')}")

    def _duplicate_saved_fit(self) -> None:
        current = next((entry for entry in self.user_state.saved_fittings() if entry.get("id") == self.current_saved_fit_id), None)
        default_name = f"{current.get('name')} Copy" if current else str((self.fit_state.get("hull") or {}).get("name") or "Ship") + " Fit Copy"
        name = simpledialog.askstring("Duplicate Fitting", "Name the copy:", initialvalue=default_name, parent=self.root)
        if not name or not name.strip():
            return
        try:
            saved = self.user_state.save_fitting(name.strip(), self._saved_fit_state())
        except OSError as error:
            messagebox.showerror("Could not duplicate fitting", str(error), parent=self.root)
            return
        self._populate_saved_fit_choices(str(saved.get("id")))
        self.status_var.set(f"Duplicated fitting as {saved.get('name')}")

    def _delete_saved_fit(self) -> None:
        if not self.current_saved_fit_id:
            return
        current = next((entry for entry in self.user_state.saved_fittings() if entry.get("id") == self.current_saved_fit_id), None)
        if not current:
            return
        if not messagebox.askyesno("Delete Saved Fitting", f"Delete the local fitting “{current.get('name')}”?", parent=self.root):
            return
        try:
            self.user_state.delete_fitting(self.current_saved_fit_id)
        except OSError as error:
            messagebox.showerror("Could not delete fitting", str(error), parent=self.root)
            return
        self.current_saved_fit_id = None
        self.saved_fit_var.set("Unsaved fit")
        self._populate_saved_fit_choices()
        self.status_var.set(f"Deleted fitting {current.get('name')}")

    def _copy_fit_summary(self) -> None:
        if not self.fit_state or not self.fit_state.get("hull"):
            return
        hull = self.fit_state.get("hull") or {}
        lines = [str(hull.get("name") or "Projected Ship"), f"Captured player skills: {'ON' if self.fit_apply_skills_var.get() else 'OFF'}", ""]
        for key, caption in (("dps", "Total DPS"), ("shield", "Shield bank"), ("energy", "Energy margin"), ("speed", "Max speed"), ("mass", "Fit mass"), ("cargo", "Hull capacity")):
            lines.append(f"{caption}: {self.fit_metric_labels[key][0].cget('text')}")
        lines.append("")
        for iid in self.ship_fit_tree.get_children():
            row = self.ship_fit_tree.item(iid)
            values = row.get("values", [])
            slot = values[0] if values else "Slot"
            lines.append(f"{slot}: {row.get('text') or '(empty)'}")
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status_var.set("Copied projected fitting summary")

    def _populate_ship(self) -> None:
        ship_data = self.data.get("ship") or {}
        specs = ship_data.get("specs") if isinstance(ship_data.get("specs"), dict) else {}
        ship = specs.get("ship") if isinstance(specs.get("ship"), dict) else {}
        inventory = ship_data.get("inventory") if isinstance(ship_data.get("inventory"), list) else []
        name = str(ship.get("display_name") or ship.get("ship_name") or "Awaiting current ship")
        classification = str(ship.get("classification") or "Unclassified hull")
        state = str(specs.get("state") or "unknown").replace("_", " ")
        self.ship_name_label.configure(text=name)
        self.ship_class_label.configure(
            text=(
                f"{classification}  •  {state}\nObserved {ship_data.get('observedAt') or 'not yet'}"
                if ship_data.get("hasData")
                else "Open Ship Specs in game, then refresh"
            )
        )

        image = self._ship_art_image(ship, (300, 250))
        self.current_ship_photo = ImageTk.PhotoImage(image)
        self.ship_art_label.configure(image=self.current_ship_photo, text="")

        self.ship_quick_text.configure(state="normal")
        self.ship_quick_text.delete("1.0", "end")
        if ship_data.get("hasData"):
            self._insert_pair(self.ship_quick_text, "Mass", format_number(ship.get("effective_mass")))
            self._insert_pair(self.ship_quick_text, "Cargo", f"{format_number(ship.get('cargo_used'), '0')} / {format_number(ship.get('cargo_capacity'), '0')}")
            self._insert_pair(self.ship_quick_text, "Plugin slots", format_number(ship.get("plugin_slots"), "0"))
            self._insert_pair(self.ship_quick_text, "Visibility", format_number(ship.get("visibility")))
            self._insert_pair(self.ship_quick_text, "Fitted rows", format_number(sum(1 for item in inventory if isinstance(item, dict) and (item.get("slot") or item.get("built_in") or item.get("item_class") == "equipped_plugin")), "0"))
        else:
            self.ship_quick_text.insert("end", "No passive ship snapshot has been collected.\n\nInstall or repair the Game Link and open the in-game Ship Specs tab to capture authoritative effective statistics.")
        self.ship_quick_text.configure(state="disabled")

        self.current_fit_state = self._fit_state_from_snapshot(specs, inventory)
        self._reset_fit_to_current()

    def _catalog_item(self, category: str, item_type: Any, display_name: Any = "") -> dict[str, Any] | None:
        wanted_type = app._normalise(item_type)
        wanted_name = app._normalise(display_name)
        for item in self.items:
            if item.get("category") != category:
                continue
            if wanted_type and app._normalise(item.get("type")) == wanted_type:
                return item
            if wanted_name and app._normalise(item.get("name")) == wanted_name:
                return item
        return None

    def _inventory_catalog_item(self, row: dict[str, Any], category: str | None = None) -> dict[str, Any]:
        item_category = str(category or row.get("item_category") or row.get("item_class") or "equipment")
        found = self._catalog_item(item_category, row.get("item_type"), row.get("display_name"))
        if found:
            return found
        return {
            "id": f"snapshot:{item_category}:{row.get('item_type') or app._normalise(row.get('display_name'))}",
            "type": row.get("item_type") or app._normalise(row.get("display_name")),
            "category": item_category,
            "categoryLabel": item_category.replace("_", " ").title(),
            "name": row.get("display_name") or row.get("item_type") or "Unknown item",
            "description": row.get("description") or "Captured from the current ship snapshot.",
            "tech": row.get("tech"),
            "cargoSize": row.get("cargo_size"),
            "colour": row.get("color_rgb") or [80, 140, 255],
            "stats": row.get("stats") if isinstance(row.get("stats"), dict) else {},
            "markets": [],
            "art": None,
        }

    def _fit_state_from_snapshot(self, specs: dict[str, Any], inventory: list[dict[str, Any]]) -> dict[str, Any]:
        ship = specs.get("ship") if isinstance(specs.get("ship"), dict) else {}
        hull = self._catalog_item("ship", ship.get("ship_type"), ship.get("display_name"))
        if hull is None and ship:
            hull = {
                "id": f"snapshot:ship:{ship.get('ship_type') or 'current'}",
                "type": ship.get("ship_type") or "current_ship",
                "category": "ship",
                "categoryLabel": "Ships",
                "name": ship.get("display_name") or "Current Ship",
                "description": "Captured current hull.",
                "tech": ship.get("level"),
                "stats": {
                    "Classification": ship.get("classification"),
                    "Mass": ship.get("mass"),
                    "Cargo Cap": ship.get("cargo_capacity"),
                    "Speed": (specs.get("engine") or {}).get("max_speed"),
                    **{
                        f"{damage_type.title()} Resist": f"{float(ship.get(f'{damage_type}_resistance', 0) or 0) * 100:g}%"
                        for damage_type in ("kinetic", "laser", "thermal", "biogenic", "mining", "energy")
                    },
                },
                "markets": [],
                "art": None,
            }

        fitted_rows = [
            row for row in inventory
            if isinstance(row, dict)
            and (row.get("slot") or row.get("built_in") or row.get("item_class") == "equipped_plugin")
            and row.get("item_class") not in {"cargo", "cargo_item", "cargo_summary", "scoop_cargo_summary", "scoop_cargo_item"}
        ]
        core: dict[str, dict[str, Any] | None] = {"engine": None, "shield": None, "energy": None}
        for kind in core:
            row = next(
                (
                    entry for entry in fitted_rows
                    if entry.get("slot") == kind or entry.get("item_class") == kind
                ),
                None,
            )
            if row:
                core[kind] = self._inventory_catalog_item(row, kind)

        summary = next((row for row in inventory if isinstance(row, dict) and row.get("item_class") == "cargo_summary"), {})
        weapon_rows = [row for row in fitted_rows if str(row.get("slot") or "").startswith("weapon") or row.get("item_class") == "weapon"]
        max_weapons = max(int(summary.get("max_weapon_slots", 0) or 0), len(weapon_rows))
        weapon_slots: list[dict[str, Any] | None] = [None] * max_weapons
        for fallback_index, row in enumerate(sorted(weapon_rows, key=lambda entry: str(entry.get("slot") or ""))):
            suffix = str(row.get("slot") or "").rsplit("_", 1)[-1]
            index = int(suffix) if suffix.isdigit() else fallback_index
            if index >= len(weapon_slots):
                weapon_slots.extend([None] * (index + 1 - len(weapon_slots)))
            weapon_slots[index] = self._inventory_catalog_item(row, "weapon")

        plugin_count = max(int(ship.get("plugin_slots", 0) or 0), sum(1 for row in fitted_rows if row.get("item_class") == "equipped_plugin"))
        plugin_slots: list[dict[str, Any] | None] = [None] * plugin_count
        for fallback_index, row in enumerate(entry for entry in fitted_rows if entry.get("item_class") == "equipped_plugin"):
            index = int(row.get("slot_index", fallback_index) or 0)
            if index >= len(plugin_slots):
                plugin_slots.extend([None] * (index + 1 - len(plugin_slots)))
            plugin_slots[index] = self._inventory_catalog_item(row, "ship_plugin")

        excluded = {"engine", "shield", "energy", "weapon", "equipped_plugin"}
        auxiliary = []
        for row in fitted_rows:
            if row.get("item_class") in excluded or str(row.get("slot") or "").startswith("weapon"):
                continue
            category = str(row.get("item_category") or row.get("item_class") or "equipment")
            auxiliary.append(
                {
                    "slot": str(row.get("slot") or "built_in"),
                    "category": category,
                    "item": self._inventory_catalog_item(row, category),
                    "locked": bool(row.get("built_in") or row.get("can_unequip") is False),
                }
            )
        auxiliary.sort(key=lambda entry: (entry["slot"] == "built_in", entry["slot"], entry["item"].get("name", "")))
        return {
            "hull": hull,
            **core,
            "weaponSlots": weapon_slots,
            "pluginSlotsList": plugin_slots,
            "aux": auxiliary,
            "maxWeaponSlots": len(weapon_slots),
            "pluginSlots": len(plugin_slots),
        }

    def _clone_fit_state(self, source: dict[str, Any]) -> dict[str, Any]:
        return {
            "hull": source.get("hull"),
            "engine": source.get("engine"),
            "shield": source.get("shield"),
            "energy": source.get("energy"),
            "weaponSlots": list(source.get("weaponSlots") or []),
            "pluginSlotsList": list(source.get("pluginSlotsList") or []),
            "aux": [dict(entry) for entry in source.get("aux") or [] if isinstance(entry, dict)],
            "maxWeaponSlots": int(source.get("maxWeaponSlots", 0) or 0),
            "pluginSlots": int(source.get("pluginSlots", 0) or 0),
        }

    def _reset_fit_to_current(self) -> None:
        self.fit_state = self._clone_fit_state(self.current_fit_state)
        self.current_saved_fit_id = None
        if hasattr(self, "saved_fit_var"):
            self.saved_fit_var.set("Unsaved fit")
        self._render_fit_tree()
        self._render_fit_simulation()

    def _render_fit_tree(self) -> None:
        selected = self.ship_fit_tree.selection()
        selected_id = selected[0] if selected else "fit-hull"
        self.ship_fit_tree.delete(*self.ship_fit_tree.get_children())
        self.fit_row_targets.clear()

        def add(iid: str, item: dict[str, Any] | None, slot: str, kind: str, target: tuple[str, int | None]) -> None:
            name = str((item or {}).get("name") or (item or {}).get("display_name") or "(empty)")
            self.ship_fit_tree.insert("", "end", iid=iid, text=name, values=(slot, kind))
            self.fit_row_targets[iid] = target

        for kind in ("hull", "engine", "shield", "energy"):
            add(f"fit-{kind}", self.fit_state.get(kind), kind.upper(), kind.replace("_", " ").title(), (kind, None))
        for index, item in enumerate(self.fit_state.get("weaponSlots") or []):
            add(f"fit-weapon-{index}", item, f"WEAPON {index + 1}", "Weapon", ("weapon", index))
        for index, item in enumerate(self.fit_state.get("pluginSlotsList") or []):
            add(f"fit-plugin-{index}", item, f"PLUGIN {index + 1}", "Ship Plugin", ("plugin", index))
        for index, entry in enumerate(self.fit_state.get("aux") or []):
            item = entry.get("item") if isinstance(entry, dict) else None
            slot = str(entry.get("slot") or "auxiliary").replace("_", " ").upper()
            kind = str(entry.get("category") or "equipment").replace("_", " ").title()
            if entry.get("locked"):
                kind += " • LOCKED"
            add(f"fit-aux-{index}", item, slot, kind, ("aux", index))

        if self.ship_fit_tree.exists(selected_id):
            self.ship_fit_tree.selection_set(selected_id)
            self.ship_fit_tree.focus(selected_id)
            self.ship_fit_tree.see(selected_id)
        elif self.ship_fit_tree.exists("fit-hull"):
            self.ship_fit_tree.selection_set("fit-hull")
            self.ship_fit_tree.focus("fit-hull")

    def _selected_fit_target(self) -> tuple[str, int | None] | None:
        selection = self.ship_fit_tree.selection()
        return self.fit_row_targets.get(selection[0]) if selection else None

    def _change_selected_fit_item(self, _event=None) -> None:
        target = self._selected_fit_target()
        if target is None:
            messagebox.showinfo("Ship Fitting", "Select a fitting row first.", parent=self.root)
            return
        kind, index = target
        if kind == "aux" and index is not None and self.fit_state["aux"][index].get("locked"):
            messagebox.showinfo("Ship Fitting", "That module is permanently built into the captured hull.", parent=self.root)
            return
        category = {
            "hull": "ship",
            "engine": "engine",
            "shield": "shield",
            "energy": "energy",
            "weapon": "weapon",
            "plugin": "ship_plugin",
        }.get(kind)
        if kind == "aux" and index is not None:
            category = str(self.fit_state["aux"][index].get("category") or "")
        if not category:
            messagebox.showinfo("Ship Fitting", "No compatible catalog category was found for that slot.", parent=self.root)
            return
        self._open_fit_item_chooser(category, target)

    def _remove_selected_fit_item(self) -> None:
        target = self._selected_fit_target()
        if target is None:
            messagebox.showinfo("Ship Fitting", "Select a fitting row first.", parent=self.root)
            return
        kind, index = target
        if kind == "hull":
            messagebox.showinfo("Ship Fitting", "A projected fit must have a hull. Choose another hull instead.", parent=self.root)
            return
        if kind == "aux" and index is not None:
            if self.fit_state["aux"][index].get("locked"):
                messagebox.showinfo("Ship Fitting", "That module is permanently built into the captured hull.", parent=self.root)
                return
            self.fit_state["aux"][index]["item"] = None
        elif kind == "weapon" and index is not None:
            self.fit_state["weaponSlots"][index] = None
        elif kind == "plugin" and index is not None:
            self.fit_state["pluginSlotsList"][index] = None
        else:
            self.fit_state[kind] = None
        self._render_fit_tree()
        self._render_fit_simulation()

    def _assign_fit_target(self, target: tuple[str, int | None], item: dict[str, Any]) -> None:
        kind, index = target
        if kind == "weapon" and index is not None:
            self.fit_state["weaponSlots"][index] = item
        elif kind == "plugin" and index is not None:
            self.fit_state["pluginSlotsList"][index] = item
        elif kind == "aux" and index is not None:
            self.fit_state["aux"][index]["item"] = item
        else:
            self.fit_state[kind] = item
        self._render_fit_tree()
        self._render_fit_simulation()

    def _open_fit_item_chooser(self, category: str, target: tuple[str, int | None]) -> None:
        candidates = sorted(
            (item for item in self.items if item.get("category") == category),
            key=lambda item: str(item.get("name") or "").casefold(),
        )
        if not candidates:
            messagebox.showinfo("Ship Fitting", f"No {category.replace('_', ' ')} items are in the captured catalog.", parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Choose {category.replace('_', ' ').title()}")
        dialog.configure(bg=BG)
        dialog.geometry("940x610")
        dialog.minsize(760, 460)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.grid_rowconfigure(2, weight=1)
        dialog.grid_columnconfigure(0, weight=1)

        tk.Label(
            dialog,
            text=f"FIT {category.replace('_', ' ').upper()}",
            bg=PANEL_2,
            fg=CYAN,
            font=("Cascadia Mono", 11, "bold"),
            padx=15,
            pady=12,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        search_var = tk.StringVar()
        search = tk.Entry(dialog, textvariable=search_var, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, font=MONO, highlightbackground=LINE, highlightthickness=1)
        search.grid(row=1, column=0, sticky="ew", padx=12, pady=10, ipady=7)

        wrap = tk.Frame(dialog, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        wrap.grid(row=2, column=0, sticky="nsew", padx=12)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        chooser = ttk.Treeview(wrap, columns=("tech", "size", "performance", "market"), show="tree headings", style="Archive.Treeview", selectmode="browse")
        chooser.heading("#0", text="ITEM", anchor="w")
        chooser.heading("tech", text="TECH", anchor="w")
        chooser.heading("size", text="SIZE", anchor="w")
        chooser.heading("performance", text="KEY STATS", anchor="w")
        chooser.heading("market", text="BEST OBSERVED BUY", anchor="w")
        chooser.column("#0", width=230, minwidth=170, stretch=True)
        chooser.column("tech", width=65, minwidth=55, stretch=False)
        chooser.column("size", width=65, minwidth=55, stretch=False)
        chooser.column("performance", width=300, minwidth=190, stretch=True)
        chooser.column("market", width=205, minwidth=150, stretch=True)
        chooser.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=chooser.yview, style="Archive.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        chooser.configure(yscrollcommand=scrollbar.set)
        visible: dict[str, dict[str, Any]] = {}

        def performance(item: dict[str, Any]) -> str:
            labels = {
                "weapon": ("Damage", "Fire Rate", "Range"),
                "engine": ("Thrust", "Turning", "Mass"),
                "shield": ("Shield Bank", "Recharge Rate", "Regen Energy Cost"),
                "energy": ("Capacity", "Output", "Mass"),
                "ship": ("Mass", "Speed", "Cargo Cap"),
            }.get(category)
            item_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
            if labels is None:
                labels = tuple(key for key in item_stats if key not in {"Tech", "Cargo Size", "Mass"})[:3]
            return "  •  ".join(f"{label}: {item_stats[label]}" for label in labels if label in item_stats) or "No captured stat sheet"

        def market(item: dict[str, Any]) -> str:
            offers = [
                (float(entry["buyPrice"]), str(entry.get("stationName") or "Unknown"))
                for entry in item.get("markets", [])
                if isinstance(entry.get("buyPrice"), (int, float)) and entry["buyPrice"] > 0
            ]
            if not offers:
                return "Not observed for sale"
            price, station = min(offers)
            return f"{format_number(price, '0')} cr • {station}"

        def populate(*_args) -> None:
            query = search_var.get().strip()
            chooser.delete(*chooser.get_children())
            visible.clear()
            for item in candidates:
                personal = self.user_state.record_annotation("item", item.get("id"))
                if not item_matches_query(item, query, personal):
                    continue
                iid = str(item.get("id") or f"chooser-{len(visible)}")
                visible[iid] = item
                chooser.insert(
                    "",
                    "end",
                    iid=iid,
                    text=item.get("name") or item.get("type") or "Unknown item",
                    values=(format_number(item.get("tech")), format_number(item.get("cargoSize")), performance(item), market(item)),
                )
            children = chooser.get_children()
            if children:
                chooser.selection_set(children[0])
                chooser.focus(children[0])

        def apply_choice(_event=None) -> None:
            selection = chooser.selection()
            if not selection:
                return
            item = visible.get(selection[0])
            if item:
                self._assign_fit_target(target, item)
                dialog.destroy()

        def selected_item() -> dict[str, Any] | None:
            selection = chooser.selection()
            return visible.get(selection[0]) if selection else None

        def open_details() -> None:
            item = selected_item()
            if item:
                dialog.destroy()
                self._open_item_record(item)

        def show_sellers() -> None:
            item = selected_item()
            if item:
                dialog.destroy()
                self._show_item_sellers_on_map(item)

        search_var.trace_add("write", populate)
        chooser.bind("<Double-1>", apply_choice)
        controls = tk.Frame(dialog, bg=BG, padx=12, pady=12)
        controls.grid(row=3, column=0, sticky="ew")
        tk.Button(controls, text="ITEM DETAILS", command=open_details, bg=PANEL_2, fg=CYAN, activebackground=PANEL_3, activeforeground=CYAN, relief="flat", bd=0, padx=14, pady=8, cursor="hand2", font=("Cascadia Mono", 8, "bold")).pack(side="left")
        tk.Button(controls, text="SELLERS ON MAP", command=show_sellers, bg=PANEL_2, fg=MINT, activebackground=PANEL_3, activeforeground=MINT, relief="flat", bd=0, padx=14, pady=8, cursor="hand2", font=("Cascadia Mono", 8, "bold")).pack(side="left", padx=(8, 0))
        tk.Button(controls, text="CANCEL", command=dialog.destroy, bg=PANEL_2, fg=MUTED, activebackground=PANEL_3, activeforeground=TEXT, relief="flat", bd=0, padx=18, pady=8, cursor="hand2", font=("Cascadia Mono", 8, "bold")).pack(side="right")
        tk.Button(controls, text="FIT SELECTED", command=apply_choice, bg=BLUE, fg="#ffffff", activebackground="#55a8ff", activeforeground="#ffffff", relief="flat", bd=0, padx=18, pady=8, cursor="hand2", font=("Cascadia Mono", 8, "bold")).pack(side="right", padx=(0, 8))
        populate()
        search.focus_set()

    def _fit_for_calculator(self, source: dict[str, Any] | None = None) -> dict[str, Any]:
        state = source if isinstance(source, dict) else self.fit_state
        return {
            "hull": state.get("hull"),
            "engine": state.get("engine"),
            "shield": state.get("shield"),
            "energy": state.get("energy"),
            "weapons": [item for item in state.get("weaponSlots") or [] if isinstance(item, dict)],
            "plugins": [item for item in state.get("pluginSlotsList") or [] if isinstance(item, dict)],
            "equipment": [entry.get("item") for entry in state.get("aux") or [] if isinstance(entry, dict) and isinstance(entry.get("item"), dict)],
            "maxWeaponSlots": int(state.get("maxWeaponSlots", 0) or 0),
            "pluginSlots": int(state.get("pluginSlots", 0) or 0),
        }

    def _server_fit_metrics(self, specs: dict[str, Any]) -> dict[str, float]:
        ship = specs.get("ship") if isinstance(specs.get("ship"), dict) else {}
        engine = specs.get("engine") if isinstance(specs.get("engine"), dict) else {}
        shield = specs.get("shield") if isinstance(specs.get("shield"), dict) else {}
        energy = specs.get("energy") if isinstance(specs.get("energy"), dict) else {}
        weapons = specs.get("weapons") if isinstance(specs.get("weapons"), list) else []
        weapon_draw = sum(
            fitting.number(weapon.get("energy_cost")) * fitting.number(weapon.get("fire_rate"))
            for weapon in weapons if isinstance(weapon, dict)
        )
        regen_cost = fitting.number(shield.get("regen_energy_cost"))
        if regen_cost <= 0:
            regen_cost = fitting.stat(self.current_fit_state.get("shield"), "Regen Energy Cost")
        shield_draw = fitting.number(shield.get("recharge_rate")) * regen_cost
        return {
            "dps": sum(fitting.number(weapon.get("dps")) for weapon in weapons if isinstance(weapon, dict)),
            "shield": fitting.number(shield.get("max_shields")),
            "energy": fitting.number(energy.get("energy_output")) - weapon_draw - shield_draw,
            "speed": fitting.number(engine.get("max_speed")),
            "mass": fitting.number(ship.get("effective_mass")),
            "cargo": fitting.number(ship.get("cargo_capacity")),
        }

    def _calibrate_fit_projection(
        self,
        projection: dict[str, Any],
        current: dict[str, Any],
        specs: dict[str, Any],
    ) -> None:
        """Anchor hidden server formula ordering to the captured current fit."""
        ship = specs.get("ship") if isinstance(specs.get("ship"), dict) else {}
        engine = specs.get("engine") if isinstance(specs.get("engine"), dict) else {}
        shield = specs.get("shield") if isinstance(specs.get("shield"), dict) else {}
        energy = specs.get("energy") if isinstance(specs.get("energy"), dict) else {}

        def scale(section: str, key: str, observed: Any) -> None:
            server_value = fitting.number(observed)
            current_value = fitting.number(current.get(section, {}).get(key))
            if server_value > 0 and current_value > 0:
                projection[section][key] = fitting.number(projection[section].get(key)) * server_value / current_value

        scale("ship", "mass", ship.get("effective_mass"))
        scale("ship", "cargoCapacity", ship.get("cargo_capacity"))
        scale("ship", "maxSpeed", engine.get("max_speed"))
        scale("engine", "thrust", engine.get("thrust"))
        scale("engine", "turning", engine.get("turning"))
        scale("engine", "acceleration", engine.get("accel"))
        scale("engine", "turnRate", engine.get("turn_rate"))
        original_recharge = fitting.number(projection["shield"].get("recharge"))
        original_shield_draw = fitting.number(projection["energy"].get("shieldDraw"))
        scale("shield", "bank", shield.get("max_shields"))
        scale("shield", "recharge", shield.get("recharge_rate"))
        scale("energy", "bank", energy.get("max_energy"))
        scale("energy", "output", energy.get("energy_output"))

        recharge = fitting.number(projection["shield"].get("recharge"))
        if original_recharge > 0:
            projection["energy"]["shieldDraw"] = original_shield_draw * recharge / original_recharge
        total_draw = fitting.number(projection["energy"].get("weaponDraw")) + fitting.number(projection["energy"].get("shieldDraw"))
        projection["energy"]["totalDraw"] = total_draw
        margin = fitting.number(projection["energy"].get("output")) - total_draw
        projection["energy"]["margin"] = margin
        projection["energy"]["depletionSeconds"] = (
            fitting.number(projection["energy"].get("bank")) / -margin
            if margin < 0 and fitting.number(projection["energy"].get("bank")) > 0
            else None
        )
        for damage_type, resistance in projection["ship"]["resistances"].items():
            projection["ship"]["effectiveShields"][damage_type] = projection["shield"]["bank"] / max(0.05, 1.0 - resistance)

    def _update_fit_metric(self, key: str, projected: float, server: float, suffix: str = "", lower_is_better: bool = False) -> None:
        value_label, delta_label = self.fit_metric_labels[key]
        value_label.configure(text=f"{format_number(projected)}{suffix}")
        delta = projected - server
        tolerance = max(0.01, abs(server) * 0.0001)
        if abs(delta) <= tolerance:
            delta_label.configure(text="MATCHES SERVER", fg=MUTED)
            return
        good = delta < 0 if lower_is_better else delta > 0
        sign = "+" if delta > 0 else ""
        delta_label.configure(text=f"Δ {sign}{format_number(delta)} VS SERVER", fg=MINT if good else RED)

    def _render_fit_simulation(self) -> None:
        widget = self.ship_specs_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if not self.fit_state or not self.fit_state.get("hull"):
            widget.insert("end", "No captured ship fit is available yet.\n\nOpen Ship Specs in game, then refresh the archive.", "value")
            widget.configure(state="disabled")
            return

        specs = (self.data.get("ship") or {}).get("specs")
        baseline = specs if isinstance(specs, dict) else {}
        skills = (self.data.get("player") or {}).get("skills")
        projection = fitting.simulate_fit(
            self._fit_for_calculator(),
            skills if isinstance(skills, list) else [],
            apply_skills=self.fit_apply_skills_var.get(),
            baseline_specs=baseline,
        )
        current_projection = fitting.simulate_fit(
            self._fit_for_calculator(self.current_fit_state),
            skills if isinstance(skills, list) else [],
            apply_skills=True,
            baseline_specs=baseline,
        )
        self._calibrate_fit_projection(projection, current_projection, baseline)
        server = self._server_fit_metrics(baseline)
        projected_metrics = {
            "dps": projection["damage"]["dps"],
            "shield": projection["shield"]["bank"],
            "energy": projection["energy"]["margin"],
            "speed": projection["ship"]["maxSpeed"],
            "mass": projection["ship"]["mass"],
            "cargo": projection["ship"]["cargoCapacity"],
        }
        self._update_fit_metric("dps", projected_metrics["dps"], server["dps"])
        self._update_fit_metric("shield", projected_metrics["shield"], server["shield"])
        self._update_fit_metric("energy", projected_metrics["energy"], server["energy"], "/s")
        self._update_fit_metric("speed", projected_metrics["speed"], server["speed"])
        self._update_fit_metric("mass", projected_metrics["mass"], server["mass"], lower_is_better=True)
        self._update_fit_metric("cargo", projected_metrics["cargo"], server["cargo"])

        hull = self.fit_state.get("hull") or {}
        current_hull = self.current_fit_state.get("hull") or {}
        projected_name = str(hull.get("name") or hull.get("display_name") or "Projected Ship")
        changed_hull = hull.get("id") != current_hull.get("id")
        self.ship_name_label.configure(text=projected_name + ("  [PROJECTED]" if changed_hull else ""))
        classification = fitting.stat_text(hull, "Classification") or "Unclassified hull"
        self.ship_class_label.configure(text=f"{classification}  •  LOCAL FIT SIMULATION\nServer snapshot remains unchanged")
        image = self._load_item_image(hull, (300, 250))
        self.current_ship_photo = ImageTk.PhotoImage(image)
        self.ship_art_label.configure(image=self.current_ship_photo, text="")

        status = "ON" if self.fit_apply_skills_var.get() else "OFF"
        widget.insert("end", f"SERVER-CALIBRATED PROJECTION  •  CAPTURED SKILLS {status}\n", "section")
        widget.insert("end", "The current fit is anchored to its server snapshot; item swaps are calculated locally.\n", "label")

        widget.insert("end", "\nDAMAGE\n", "section")
        self._insert_pair(widget, "Total DPS", format_number(projection["damage"]["dps"]))
        self._insert_pair(widget, "Alpha volley", format_number(projection["damage"]["alpha"]))
        self._insert_pair(widget, "Weapon energy draw", f"{format_number(projection['energy']['weaponDraw'])} PU/s")

        widget.insert("end", "\nSHIELDS / ENERGY\n", "section")
        self._insert_pair(widget, "Shield bank", f"{format_number(projection['shield']['bank'])} HP")
        self._insert_pair(widget, "Shield recharge", f"{format_number(projection['shield']['recharge'])} HP/s")
        self._insert_pair(widget, "Shield energy draw", f"{format_number(projection['energy']['shieldDraw'])} PU/s")
        self._insert_pair(widget, "Energy bank", f"{format_number(projection['energy']['bank'])} PU")
        self._insert_pair(widget, "Energy output", f"{format_number(projection['energy']['output'])} PU/s")
        margin_tag = "good" if projection["energy"]["margin"] >= 0 else "bad"
        widget.insert("end", "Sustained margin: ", "label")
        widget.insert("end", f"{format_number(projection['energy']['margin'])} PU/s\n", margin_tag)
        if projection["energy"]["depletionSeconds"] is not None:
            self._insert_pair(widget, "Bank depletion", f"{format_number(projection['energy']['depletionSeconds'])} s")

        widget.insert("end", "\nPROPULSION / HULL\n", "section")
        self._insert_pair(widget, "Effective mass", format_number(projection["ship"]["mass"]))
        self._insert_pair(widget, "Hull capacity", format_number(projection["ship"]["cargoCapacity"]))
        self._insert_pair(widget, "Maximum speed", format_number(projection["ship"]["maxSpeed"]))
        self._insert_pair(widget, "Thrust", format_number(projection["engine"]["thrust"]))
        self._insert_pair(widget, "Turning", format_number(projection["engine"]["turning"]))
        self._insert_pair(widget, "Acceleration", format_number(projection["engine"]["acceleration"]))
        self._insert_pair(widget, "Turn rate", format_number(projection["engine"]["turnRate"]))

        widget.insert("end", "\nDAMAGE RESISTANCE / SHIELD EHP\n", "section")
        for damage_type, resistance in projection["ship"]["resistances"].items():
            ehp = projection["ship"]["effectiveShields"][damage_type]
            self._insert_pair(widget, damage_type.title(), f"{resistance * 100:.1f}%  •  {format_number(ehp)} effective HP")

        widget.insert("end", f"\nWEAPONS ({len(projection['weapons'])})\n", "section")
        if not projection["weapons"]:
            widget.insert("end", "No weapons fitted.\n", "label")
        for weapon in projection["weapons"]:
            widget.insert("end", f"{weapon['name']}\n", "good")
            widget.insert(
                "end",
                f"  {format_number(weapon['dps'])} DPS  •  {format_number(weapon['damage'])} {weapon['damageType']} damage\n"
                f"  {format_number(weapon['fireRate'])}/s  •  {format_number(weapon['range'])} range  •  {format_number(weapon['energyPerSecond'])} PU/s\n",
                "value",
            )

        percent_bonuses = projection["bonuses"].get("percent") or {}
        flat_bonuses = projection["bonuses"].get("flat") or {}
        widget.insert("end", "\nAPPLIED BONUSES\n", "section")
        if not percent_bonuses and not flat_bonuses:
            widget.insert("end", "No captured percentage or flat bonuses apply.\n", "label")
        for key, value in sorted(percent_bonuses.items()):
            self._insert_pair(widget, key.replace("_", " ").title(), f"{value * 100:+.1f}%")
        for key, value in sorted(flat_bonuses.items()):
            self._insert_pair(widget, key.replace("_", " ").title(), f"{value:+,.2f}")

        if projection["warnings"]:
            widget.insert("end", "\nFIT WARNINGS\n", "section")
            for warning in projection["warnings"]:
                widget.insert("end", f"• {warning}\n", "warning")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _render_ship_specs(self, specs: dict[str, Any]) -> None:
        widget = self.ship_specs_text
        ship = specs.get("ship") if isinstance(specs.get("ship"), dict) else {}
        engine = specs.get("engine") if isinstance(specs.get("engine"), dict) else {}
        shield = specs.get("shield") if isinstance(specs.get("shield"), dict) else {}
        energy = specs.get("energy") if isinstance(specs.get("energy"), dict) else {}

        widget.insert("end", "HULL\n", "section")
        self._insert_pair(widget, "Flat mitigation", format_number(ship.get("flat_damage_mitigation")))
        base_resistance = ship.get("resistance", 0) or 0
        for damage_type in ("kinetic", "laser", "thermal", "biogenic", "mining", "energy"):
            value = ship.get(f"{damage_type}_resistance", base_resistance) or 0
            try:
                formatted = f"{float(value) * 100:.1f}%"
            except (TypeError, ValueError):
                formatted = format_number(value)
            self._insert_pair(widget, f"{damage_type.title()} resist", formatted)

        widget.insert("end", "\nPROPULSION\n", "section")
        self._insert_pair(widget, "Engine", str(engine.get("display_name") or "-"))
        self._insert_pair(widget, "Maximum speed", f"{format_number(engine.get('max_speed'))} px/s")
        self._insert_pair(widget, "Acceleration", f"{format_number(engine.get('accel'))} px/s²")
        self._insert_pair(widget, "Turn rate", f"{format_number(engine.get('turn_rate'))} deg/s²")
        self._insert_pair(widget, "Thrust", format_number(engine.get("thrust")))
        self._insert_pair(widget, "Turning", format_number(engine.get("turning")))

        widget.insert("end", "\nSHIELDS / ENERGY\n", "section")
        self._insert_pair(widget, "Shield", str(shield.get("display_name") or "-"))
        self._insert_pair(widget, "Shield bank", f"{format_number(shield.get('current_shields'), '0')} / {format_number(shield.get('max_shields'), '0')}")
        self._insert_pair(widget, "Shield recharge", f"{format_number(shield.get('recharge_rate'))} HP/s")
        self._insert_pair(widget, "Energy", str(energy.get("display_name") or "-"))
        self._insert_pair(widget, "Energy bank", f"{format_number(energy.get('current_energy'), '0')} / {format_number(energy.get('max_energy'), '0')}")
        self._insert_pair(widget, "Energy output", f"{format_number(energy.get('energy_output'))} PU/s")

        weapons = specs.get("weapons") if isinstance(specs.get("weapons"), list) else []
        widget.insert("end", f"\nWEAPONS ({len(weapons)})\n", "section")
        if not weapons:
            widget.insert("end", "No weapons equipped\n", "value")
        for weapon in weapons:
            if not isinstance(weapon, dict):
                continue
            name = str(weapon.get("display_name") or weapon.get("weapon_type") or "Weapon")
            widget.insert("end", f"{name}\n", "good")
            widget.insert(
                "end",
                "  DPS {dps}   damage {damage}   rate {rate}\n"
                "  range {range}   energy {energy}\n".format(
                    dps=format_number(weapon.get("dps"), "0"),
                    damage=format_number(weapon.get("damage"), "0"),
                    rate=format_number(weapon.get("fire_rate"), "0"),
                    range=format_number(weapon.get("max_range"), "0"),
                    energy=format_number(weapon.get("energy_cost"), "0"),
                ),
                "value",
            )

        plugins = specs.get("plugins") if isinstance(specs.get("plugins"), list) else []
        widget.insert("end", f"\nACTIVE PLUGINS ({len(plugins)})\n", "section")
        if not plugins:
            widget.insert("end", "No plugins installed\n", "value")
        for plugin in plugins:
            if isinstance(plugin, dict):
                widget.insert("end", f"• {plugin.get('display_name') or plugin.get('plugin_id') or 'Plugin'}\n", "value")

    def _ship_art_image(self, ship: dict[str, Any], size: tuple[int, int]) -> Image.Image:
        candidates = [
            str(ship.get("ship_type") or ""),
            str(ship.get("type") or ""),
            str(ship.get("display_name") or ""),
        ]
        catalog_ships = [item for item in self.items if item.get("category") == "ship"]
        normalised = {app._normalise(candidate) for candidate in candidates if candidate}
        item = next(
            (
                entry for entry in catalog_ships
                if app._normalise(entry.get("type")) in normalised or app._normalise(entry.get("name")) in normalised
            ),
            None,
        )
        if item:
            return self._load_item_image(item, size)
        fallback = {
            "name": ship.get("display_name") or "Current Ship",
            "type": ship.get("ship_type") or ship.get("display_name") or "current_ship",
            "category": "ship",
            "colour": [55, 148, 255],
            "art": None,
        }
        return self._generated_art(fallback, size, labels=True)

    def _skill_effect_label(self, key: Any, compact: bool = False) -> str:
        labels = {
            "max_speed": ("Maximum Speed", "Speed"),
            "ship_speed": ("Ship Speed", "Speed"),
            "_shield_recharge_rate": ("Shield Recharge", "Shield Regen"),
            "shield_regen": ("Shield Recharge", "Shield Regen"),
            "max_shields": ("Shield Bank", "Shields"),
            "shield_bank": ("Shield Bank", "Shields"),
            "energy_output": ("Energy Output", "Energy Output"),
            "energy_recharge": ("Energy Recharge", "Energy Regen"),
            "max_energy": ("Energy Bank", "Energy Bank"),
            "energy_bank": ("Energy Bank", "Energy Bank"),
            "cargo_capacity": ("Hull Capacity", "Cargo"),
            "hull_capacity": ("Hull Capacity", "Cargo"),
            "weapon_damage": ("Weapon Damage", "Damage"),
            "weapon_range": ("Weapon Range", "Range"),
            "fire_rate": ("Rate of Fire", "Fire Rate"),
            "proj_speed": ("Projectile Speed", "Proj Speed"),
            "proj_tracking": ("Projectile Tracking", "Tracking"),
            "transference_power": ("Transference Power", "Transfer"),
            "flat_damage_mitigation": ("Flat Damage Mitigation", "Mitigation"),
            "mass": ("Effective Mass", "Mass"),
            "thrust": ("Engine Thrust", "Thrust"),
            "turning": ("Engine Turning", "Turning"),
            "turn_rate": ("Turn Rate", "Turn Rate"),
        }
        normalised = str(key).strip().casefold()
        full, short = labels.get(normalised, (str(key).replace("_", " ").title(), str(key).replace("_", " ").title()))
        return short if compact else full

    def _skill_effect_entries(self, skill: dict[str, Any], current_rank: bool, compact: bool = False) -> list[str]:
        level = fitting.number(skill.get("level"))
        multiplier = level if current_rank else 1.0
        if current_rank and multiplier <= 0:
            return []

        entries: list[str] = []
        flat_values = skill.get("display_stat_bonus") or skill.get("stat_bonus")
        if isinstance(flat_values, dict):
            flat_units = {
                "max_speed": " px/s",
                "_shield_recharge_rate": " HP/s",
                "shield_regen": " HP/s",
                "max_shields": " HP",
                "shield_bank": " HP",
                "energy_output": " PU/s",
                "energy_recharge": " PU/s",
                "max_energy": " PU",
                "energy_bank": " PU",
                "cargo_capacity": " space",
                "hull_capacity": " space",
            }
            for key, value in flat_values.items():
                amount = fitting.number(value) * multiplier
                sign = "+" if amount > 0 else ""
                label = self._skill_effect_label(key, compact)
                entries.append(f"{label} {sign}{format_number(amount, '0')}{flat_units.get(str(key).casefold(), '')}")

        percent_values = skill.get("pct_bonus")
        if isinstance(percent_values, dict):
            for key, value in percent_values.items():
                amount = fitting.number(value) * multiplier * 100.0
                sign = "+" if amount > 0 else ""
                label = self._skill_effect_label(key, compact)
                entries.append(f"{label} {sign}{format_number(amount, '0')}%")
        elif percent_values not in (None, "", [], {}):
            amount = fitting.number(percent_values) * multiplier * 100.0
            sign = "+" if amount > 0 else ""
            entries.append(f"Bonus {sign}{format_number(amount, '0')}%")
        return entries

    def _skill_bonus_text(self, skill: dict[str, Any]) -> str:
        level = fitting.number(skill.get("level"))
        entries = self._skill_effect_entries(skill, current_rank=level > 0, compact=True)
        if not entries:
            return "No stat increase"
        visible = entries[:3]
        if len(entries) > len(visible):
            visible.append(f"+{len(entries) - len(visible)} more")
        prefix = "" if level > 0 else "Per rank: "
        return prefix + "  ·  ".join(visible)

    def _mastery_lines(self, value: Any, depth: int = 0) -> list[str]:
        if depth > 4 or value in (None, "", [], {}):
            return []
        if isinstance(value, list):
            lines: list[str] = []
            for entry in value:
                lines.extend(self._mastery_lines(entry, depth))
            return lines
        if not isinstance(value, dict):
            return [("  " * depth) + str(value)]
        label = value.get("display_name") or value.get("name") or value.get("title") or value.get("id") or value.get("mastery_id")
        lines = []
        if label:
            suffix = ""
            if value.get("level") is not None:
                suffix = f"  [rank {value.get('level')}]"
            lines.append(("  " * depth) + f"• {label}{suffix}")
            depth += 1
        for key in ("branches", "nodes", "children", "masteries", "skills"):
            if key in value:
                lines.extend(self._mastery_lines(value[key], depth))
        return lines

    def _format_duration(self, seconds: Any) -> str:
        try:
            total = max(0, int(seconds or 0))
        except (TypeError, ValueError):
            return "-"
        if not total:
            return "-"
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"

    def _insert_pair(self, widget: tk.Text, label: str, value: str) -> None:
        widget.insert("end", f"{label}: ", "label")
        widget.insert("end", value + "\n", "value")

    def _insert_resource_yield_entries(
        self,
        widget: tk.Text,
        entries: list[tuple[str, float]],
        indent: str = "  ",
    ) -> None:
        widget.insert("end", indent, "label")
        for index, (resource, amount) in enumerate(entries):
            if index:
                widget.insert("end", "  ·  ", "label")
            widget.insert("end", f"{resource}: ", "good")
            widget.insert("end", format_number(amount), "value")
        widget.insert("end", "\n", "value")

    def _insert_extractor_slot_entries(
        self,
        widget: tk.Text,
        entries: list[tuple[str, int | None, float]],
        tier_counts: dict[str, dict[int, int]] | None = None,
        indent: str = "",
    ) -> None:
        """Write private observed/possible slots and their known tier mixes."""
        widget.insert("end", indent, "label")
        for index, (resource, used, possible) in enumerate(entries):
            if index:
                widget.insert("end", "\n", "value")
            widget.insert("end", f"{resource}: ", "good")
            widget.insert("end", format_number(used, "—"), "value")
            widget.insert("end", " / ", "label")
            widget.insert("end", format_number(possible), "value")
            resource_key = resource.casefold().replace(" ", "_")
            tier_text = extractor_tier_summary((tier_counts or {}).get(resource_key))
            if tier_text:
                widget.insert("end", "\n  TIERS: ", "label")
                widget.insert("end", tier_text, "value")
        widget.insert("end", "\n", "value")

    def _insert_system_extraction_entries(
        self,
        widget: tk.Text,
        entries: list[dict[str, Any]],
    ) -> None:
        """Render detailed capacity without presenting unknown use as free."""
        for index, entry in enumerate(entries):
            if index:
                widget.insert("end", "\n", "value")
            widget.insert("end", f"{entry['resource']}\n", "good")
            widget.insert("end", "  KNOWN USED: ", "label")
            used = entry.get("used")
            maximum = entry.get("maximum")
            widget.insert("end", "—" if used is None else format_number(used), "value")
            widget.insert("end", f" / {format_number(maximum)} slots", "label")
            percent = entry.get("usedPercent")
            if percent is not None:
                widget.insert("end", f" · {float(percent):.1f}% OF MAX", "value")
            widget.insert("end", "\n", "value")
            remaining = entry.get("remaining")
            if remaining is not None:
                if float(remaining) >= 0:
                    widget.insert("end", "  NOT LOCALLY OBSERVED AS USED: ", "label")
                    widget.insert("end", f"{format_number(remaining)} slots\n", "value")
                else:
                    widget.insert("end", "  ABOVE SCANNED MAX: ", "warning")
                    widget.insert("end", f"{format_number(-float(remaining))} slots\n", "warning")
            tier_summary = str(entry.get("tierSummary") or "")
            if tier_summary:
                widget.insert("end", "  OBSERVED TIERS: ", "label")
                widget.insert("end", tier_summary + "\n", "value")

    def _format_detail_value(self, value: Any) -> str:
        if isinstance(value, dict):
            return ", ".join(f"{key}: {format_number(item)}" for key, item in value.items())
        if isinstance(value, list):
            return ", ".join(format_number(item) for item in value) or "-"
        return format_number(value)

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _load_item_image(self, item: dict[str, Any], size: tuple[int, int]) -> Image.Image:
        art = item.get("art")
        if isinstance(art, dict) and art.get("folder") and art.get("filename"):
            path = app.ASSET_ROOT / str(art["folder"]) / str(art["filename"])
            try:
                with Image.open(path) as source:
                    converted = source.convert("RGBA")
                    contained = ImageOps.contain(converted, (size[0] - 24, size[1] - 34), Image.Resampling.LANCZOS)
                    canvas = self._art_background(item, size)
                    x = (size[0] - contained.width) // 2
                    y = (size[1] - contained.height) // 2 - 4
                    canvas.alpha_composite(contained, (x, y))
                    draw = ImageDraw.Draw(canvas, "RGBA")
                    draw.rounded_rectangle((10, size[1] - 27, 119, size[1] - 8), radius=5, fill=(5, 11, 20, 205), outline=(111, 244, 189, 180))
                    draw.text((18, size[1] - 24), "OFFICIAL GAME ART", font=self._pil_font(10, bold=True), fill=(111, 244, 189, 255))
                    return canvas.convert("RGB")
            except (OSError, ValueError):
                pass
        return self._generated_art(item, size, labels=True)

    def _art_background(self, item: dict[str, Any], size: tuple[int, int]) -> Image.Image:
        seed = hashlib.sha256(f"{item.get('category')}:{item.get('type')}".encode("utf-8", errors="replace")).digest()
        colour = item.get("colour")
        if isinstance(colour, list) and len(colour) >= 3:
            accent = tuple(max(45, min(225, int(value))) for value in colour[:3])
        else:
            accent = (53, 160 + seed[0] % 80, 210 + seed[1] % 40)
        image = Image.new("RGBA", size, (4, 10, 18, 255))
        pixels = image.load()
        width, height = size
        for y in range(height):
            glow = max(0.0, 1.0 - abs(y - height * 0.45) / (height * 0.72))
            for x in range(width):
                radial = max(0.0, 1.0 - (((x - width * 0.52) / width) ** 2 + ((y - height * 0.45) / height) ** 2) * 4.2)
                strength = glow * radial * 0.24
                pixels[x, y] = (
                    int(4 + accent[0] * strength),
                    int(10 + accent[1] * strength),
                    int(18 + accent[2] * strength),
                    255,
                )
        draw = ImageDraw.Draw(image, "RGBA")
        for index in range(18):
            x = (seed[index % len(seed)] * 37 + index * 71) % width
            y = (seed[(index + 8) % len(seed)] * 19 + index * 31) % max(1, height - 28)
            radius = 1 + seed[(index + 3) % len(seed)] % 2
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(180, 225, 255, 80 + seed[index] % 130))
        for x in range(0, width, max(24, width // 12)):
            draw.line((x, 0, x, height), fill=(71, 151, 199, 18))
        for y in range(0, height, max(24, height // 8)):
            draw.line((0, y, width, y), fill=(71, 151, 199, 18))
        return image

    def _generated_art(self, item: dict[str, Any], size: tuple[int, int], labels: bool) -> Image.Image:
        image = self._art_background(item, size)
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = size
        category = str(item.get("category") or "unknown")
        seed = hashlib.sha256(f"{category}:{item.get('type')}".encode("utf-8", errors="replace")).digest()
        colour = item.get("colour")
        if isinstance(colour, list) and len(colour) >= 3:
            accent = tuple(max(70, min(255, int(value))) for value in colour[:3])
        else:
            accent = (53, 216, 255)
        cx = width // 2
        cy = height // 2 - (12 if labels else 0)
        scale = min(width / 420, height / 270)
        outline = (*accent, 245)
        fill = (*accent, 62)
        bright = tuple(min(255, value + 42) for value in accent) + (245,)

        def point(dx: float, dy: float) -> tuple[int, int]:
            return int(cx + dx * scale), int(cy + dy * scale)

        if category in {"weapon", "turret_upgrade", "launch_tube", "missile"}:
            draw.polygon([point(-128, -20), point(45, -20), point(135, 0), point(45, 20), point(-128, 20)], fill=fill, outline=outline)
            draw.rounded_rectangle((*point(-72, 19), *point(-15, 63)), radius=max(2, int(7 * scale)), fill=fill, outline=outline, width=max(1, int(3 * scale)))
            draw.line((*point(-112, 0), *point(94, 0)), fill=bright, width=max(1, int(3 * scale)))
        elif category in {"shield", "shield_charger"}:
            draw.polygon([point(0, -102), point(90, -66), point(75, 45), point(0, 108), point(-75, 45), point(-90, -66)], fill=fill, outline=outline)
            draw.arc((*point(-64, -56), *point(64, 75)), 205, 335, fill=bright, width=max(1, int(5 * scale)))
        elif category in {"engine", "slipstream", "scoop", "tractor"}:
            draw.polygon([point(-120, -54), point(38, -54), point(128, 0), point(38, 54), point(-120, 54), point(-72, 0)], fill=fill, outline=outline)
            for offset in (-26, 0, 26):
                draw.line((*point(-105, offset), *point(-170, offset * 0.6)), fill=bright, width=max(1, int(4 * scale)))
        elif category in {"energy", "resource"}:
            draw.ellipse((*point(-82, -82), *point(82, 82)), fill=fill, outline=outline, width=max(1, int(4 * scale)))
            draw.polygon([point(-10, -70), point(42, -15), point(8, -4), point(25, 68), point(-45, 12), point(-8, 1)], fill=bright)
        elif category in {"station", "station_plugin"}:
            draw.polygon([point(0, -100), point(88, -52), point(88, 52), point(0, 100), point(-88, 52), point(-88, -52)], fill=fill, outline=outline)
            draw.ellipse((*point(-38, -38), *point(38, 38)), outline=bright, width=max(1, int(5 * scale)))
            draw.line((*point(-150, 0), *point(150, 0)), fill=outline, width=max(1, int(4 * scale)))
        elif category == "planet":
            draw.ellipse((*point(-94, -94), *point(94, 94)), fill=fill, outline=outline, width=max(1, int(4 * scale)))
            draw.arc((*point(-142, -50), *point(142, 50)), 5, 175, fill=bright, width=max(1, int(5 * scale)))
            draw.arc((*point(-142, -50), *point(142, 50)), 185, 355, fill=bright, width=max(1, int(5 * scale)))
            for index in range(5):
                px = -54 + (seed[index] % 110)
                py = -52 + (seed[index + 5] % 100)
                radius = 5 + seed[index + 10] % 14
                draw.ellipse((*point(px - radius, py - radius), *point(px + radius, py + radius)), fill=(*accent, 35))
        elif category in {"drone", "ship", "hangar", "fabricator"}:
            draw.polygon([point(0, -95), point(50, -28), point(135, 0), point(58, 35), point(28, 94), point(0, 58), point(-28, 94), point(-58, 35), point(-135, 0), point(-50, -28)], fill=fill, outline=outline)
            draw.ellipse((*point(-24, -24), *point(24, 24)), fill=bright)
        elif category in {"scanner", "sensor"}:
            draw.ellipse((*point(-94, -94), *point(94, 94)), outline=outline, width=max(1, int(5 * scale)))
            draw.pieslice((*point(-84, -84), *point(84, 84)), -36, 24, fill=fill, outline=bright)
            draw.line((*point(-130, 0), *point(130, 0)), fill=outline, width=max(1, int(2 * scale)))
            draw.line((*point(0, -130), *point(0, 130)), fill=outline, width=max(1, int(2 * scale)))
        else:
            draw.rounded_rectangle((*point(-94, -72), *point(94, 72)), radius=max(2, int(15 * scale)), fill=fill, outline=outline, width=max(1, int(4 * scale)))
            draw.polygon([point(0, -58), point(18, -18), point(62, 0), point(18, 18), point(0, 58), point(-18, 18), point(-62, 0), point(-18, -18)], fill=bright)

        if labels and width >= 220 and height >= 130:
            name = str(item.get("name") or item.get("type") or "Unknown item")
            category_label = app.CATEGORY_LABELS.get(category, category.replace("_", " ").title())
            title_font = self._pil_font(max(13, min(18, width // 23)), bold=True)
            small_font = self._pil_font(max(9, min(11, width // 34)), bold=True)
            max_chars = max(16, width // 10)
            title = textwrap.shorten(name, width=max_chars, placeholder="...")
            draw.rounded_rectangle((10, 9, min(width - 10, 143), 31), radius=5, fill=(5, 11, 20, 205), outline=(*accent, 155))
            draw.text((17, 12), category_label.upper()[:21], font=small_font, fill=bright)
            title_box = draw.textbbox((0, 0), title, font=title_font)
            title_width = title_box[2] - title_box[0]
            draw.text(((width - title_width) // 2, height - 50), title, font=title_font, fill=(224, 240, 255, 255))
            badge = "GENERATED INTEL ART"
            badge_box = draw.textbbox((0, 0), badge, font=small_font)
            badge_width = badge_box[2] - badge_box[0]
            draw.text(((width - badge_width) // 2, height - 27), badge, font=small_font, fill=(120, 151, 180, 255))
        return image.convert("RGB")

    def _pil_font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        font_name = "seguisb.ttf" if bold else "segoeui.ttf"
        candidates = (Path("C:/Windows/Fonts") / font_name, Path("C:/Windows/Fonts/arial.ttf"))
        for path in candidates:
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def close(self) -> None:
        try:
            self._save_item_table_layout()
            self._save_scan_table_layout()
            self._save_station_table_layout()
            self._save_station_item_table_layout()
            self._save_training_table_layout()
            self._save_player_skill_table_layout()
            self._save_map_result_layout()
            self._save_map_view()
        except OSError:
            pass
        self.root.destroy()


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    StarEmpireDesktop(root)
    root.mainloop()


if __name__ == "__main__":
    main()
