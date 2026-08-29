"""Pure geometry helpers for coalition territory on the S.E.C. galaxy map.

Only the server-provided territory assignments are rendered.  Every known
system still participates as a Voronoi clipping site, including systems hidden
from the visual node layer because no jump connection has been recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


DEFAULT_TERRITORY_COLOR = (126, 141, 168)
_EPSILON = 1.0e-9


@dataclass(frozen=True)
class TerritoryCell:
    """One claimed system's clipped map region in galaxy coordinates."""

    system_name: str
    coalition_id: int
    coalition_name: str
    color: tuple[int, int, int]
    polygon: tuple[tuple[float, float], ...]
    boundary_segments: tuple[
        tuple[tuple[float, float], tuple[float, float]], ...
    ]
    same_coalition_neighbors: tuple[str, ...]


@dataclass(frozen=True)
class TerritoryLabelRegion:
    """One visually connected coalition region and its label metrics."""

    coalition_id: int
    coalition_name: str
    color: tuple[int, int, int]
    system_names: tuple[str, ...]
    area: float
    anchor: tuple[float, float]
    bounds: tuple[float, float, float, float]
    label_path: tuple[tuple[float, float], ...]


def _polygon_area_centroid(
    polygon: tuple[tuple[float, float], ...],
) -> tuple[float, tuple[float, float]]:
    cross_sum = 0.0
    centroid_x = 0.0
    centroid_y = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        cross = x1 * y2 - x2 * y1
        cross_sum += cross
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross
    if abs(cross_sum) <= _EPSILON:
        if not polygon:
            return 0.0, (0.0, 0.0)
        return 0.0, (
            sum(point[0] for point in polygon) / len(polygon),
            sum(point[1] for point in polygon) / len(polygon),
        )
    return abs(cross_sum) * 0.5, (
        centroid_x / (3.0 * cross_sum),
        centroid_y / (3.0 * cross_sum),
    )


def _point_in_polygon(point: tuple[float, float], polygon) -> bool:
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


def _principal_label_axis(
    points: list[tuple[float, float]],
    anchor: tuple[float, float],
) -> tuple[float, float]:
    """Return the deterministic long axis of a territory region."""
    xx = xy = yy = 0.0
    for x, y in points:
        dx = x - anchor[0]
        dy = y - anchor[1]
        xx += dx * dx
        xy += dx * dy
        yy += dy * dy
    if xx + yy <= _EPSILON:
        return (1.0, 0.0)
    angle = 0.5 * math.atan2(2.0 * xy, xx - yy)
    axis = (math.cos(angle), math.sin(angle))
    if axis[0] < -_EPSILON or (abs(axis[0]) <= _EPSILON and axis[1] < 0.0):
        axis = (-axis[0], -axis[1])
    return axis


def _section_intervals(
    cells: list[TerritoryCell],
    anchor: tuple[float, float],
    axis: tuple[float, float],
    normal: tuple[float, float],
    station: float,
) -> list[tuple[float, float]]:
    """Return merged territory intervals across one long-axis station."""
    intervals: list[tuple[float, float]] = []
    for cell in cells:
        projected = [
            (
                (point[0] - anchor[0]) * axis[0]
                + (point[1] - anchor[1]) * axis[1],
                (point[0] - anchor[0]) * normal[0]
                + (point[1] - anchor[1]) * normal[1],
            )
            for point in cell.polygon
        ]
        intersections: list[float] = []
        for index, (s1, t1) in enumerate(projected):
            s2, t2 = projected[(index + 1) % len(projected)]
            delta = s2 - s1
            if abs(delta) <= _EPSILON:
                if abs(station - s1) <= _EPSILON:
                    intersections.extend((t1, t2))
                continue
            if station < min(s1, s2) - _EPSILON or station > max(s1, s2) + _EPSILON:
                continue
            amount = (station - s1) / delta
            intersections.append(t1 + (t2 - t1) * amount)
        if len(intersections) >= 2:
            intervals.append((min(intersections), max(intersections)))

    merged: list[list[float]] = []
    for low, high in sorted(intervals):
        if not merged or low > merged[-1][1] + 1.0e-7:
            merged.append([low, high])
        else:
            merged[-1][1] = max(merged[-1][1], high)
    return [(low, high) for low, high in merged]


def _interval_near(
    intervals: list[tuple[float, float]],
    target: float,
) -> tuple[float, float] | None:
    if not intervals:
        return None
    return min(
        intervals,
        key=lambda interval: (
            0.0
            if interval[0] <= target <= interval[1]
            else min(abs(target - interval[0]), abs(target - interval[1])),
            abs((interval[0] + interval[1]) * 0.5 - target),
            interval,
        ),
    )


def _point_at_polyline_distance(
    points: list[tuple[float, float]],
    cumulative: list[float],
    distance: float,
) -> tuple[float, float]:
    target = max(0.0, min(cumulative[-1], distance))
    for index in range(1, len(points)):
        if cumulative[index] + _EPSILON < target:
            continue
        span = cumulative[index] - cumulative[index - 1]
        if span <= _EPSILON:
            return points[index]
        amount = (target - cumulative[index - 1]) / span
        return (
            points[index - 1][0]
            + (points[index][0] - points[index - 1][0]) * amount,
            points[index - 1][1]
            + (points[index][1] - points[index - 1][1]) * amount,
        )
    return points[-1]


def _trim_path_around_anchor(
    points: list[tuple[float, float]],
    anchor_index: int,
) -> tuple[tuple[float, float], ...]:
    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(first, second))
    left = cumulative[anchor_index]
    right = cumulative[-1] - left
    arm = min(left, right)
    if arm <= _EPSILON:
        return (points[anchor_index],)
    start_distance = left - arm
    end_distance = left + arm
    trimmed = [_point_at_polyline_distance(points, cumulative, start_distance)]
    trimmed.extend(
        point
        for point, distance in zip(points, cumulative)
        if start_distance + _EPSILON < distance < end_distance - _EPSILON
    )
    trimmed.append(_point_at_polyline_distance(points, cumulative, end_distance))
    return tuple(trimmed)


def _build_centered_label_path(
    cells: list[TerritoryCell],
    anchor: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    """Trace a smoothed, shape-following path centred on the area anchor."""
    all_points = [point for cell in cells for point in cell.polygon]
    if not all_points:
        return (anchor,)
    axis = _principal_label_axis(all_points, anchor)
    normal = (-axis[1], axis[0])
    projected_stations = [
        (point[0] - anchor[0]) * axis[0]
        + (point[1] - anchor[1]) * axis[1]
        for point in all_points
    ]
    minimum = min(projected_stations)
    maximum = max(projected_stations)
    span = maximum - minimum
    if span <= _EPSILON:
        return (anchor,)
    inset = span * 0.10
    start = min(minimum + inset, -_EPSILON)
    end = max(maximum - inset, _EPSILON)
    if start >= 0.0:
        start = minimum
    if end <= 0.0:
        end = maximum

    arm_samples = 8
    stations = [start + (0.0 - start) * index / arm_samples
                for index in range(arm_samples)]
    stations.append(0.0)
    stations.extend(end * index / arm_samples for index in range(1, arm_samples + 1))
    centre_index = arm_samples
    sections = [
        _section_intervals(cells, anchor, axis, normal, station)
        for station in stations
    ]
    selected: list[tuple[float, float] | None] = [None] * len(stations)
    selected[centre_index] = _interval_near(sections[centre_index], 0.0)
    if selected[centre_index] is None:
        return (anchor,)

    transverse = [0.0] * len(stations)
    for direction in (-1, 1):
        previous = 0.0
        indexes = (
            range(centre_index - 1, -1, -1)
            if direction < 0
            else range(centre_index + 1, len(stations))
        )
        for index in indexes:
            interval = _interval_near(sections[index], previous)
            if interval is None:
                transverse[index] = previous
                continue
            selected[index] = interval
            transverse[index] = (interval[0] + interval[1]) * 0.5
            previous = transverse[index]

    # The text midpoint stays on the existing visual centroid. Neighbouring
    # cross-section midpoints supply option 2's bend without pulling the name
    # into a fat lobe or toward the territory edge.
    transverse[centre_index] = 0.0
    for _pass in range(2):
        smoothed = list(transverse)
        for index in range(1, len(transverse) - 1):
            if index == centre_index or selected[index] is None:
                continue
            candidate = (
                transverse[index - 1] * 0.25
                + transverse[index] * 0.5
                + transverse[index + 1] * 0.25
            )
            low, high = selected[index]
            margin = min((high - low) * 0.18, (high - low) * 0.49)
            smoothed[index] = max(low + margin, min(high - margin, candidate))
        transverse = smoothed
        transverse[centre_index] = 0.0

    # Prevent an extreme narrow turn from rotating successive letters too far.
    max_slope = math.tan(math.radians(40.0))
    for indexes in (
        range(centre_index + 1, len(stations)),
        range(centre_index - 1, -1, -1),
    ):
        previous_index = centre_index
        for index in indexes:
            station_step = abs(stations[index] - stations[previous_index])
            limit = station_step * max_slope
            transverse[index] = max(
                transverse[previous_index] - limit,
                min(transverse[previous_index] + limit, transverse[index]),
            )
            if selected[index] is not None:
                low, high = selected[index]
                transverse[index] = max(low, min(high, transverse[index]))
            previous_index = index

    points = [
        (
            anchor[0] + axis[0] * station + normal[0] * offset,
            anchor[1] + axis[1] * station + normal[1] * offset,
        )
        for station, offset in zip(stations, transverse)
    ]
    points[centre_index] = anchor
    return _trim_path_around_anchor(points, centre_index)


def build_territory_label_regions(
    cells: tuple[TerritoryCell, ...],
) -> tuple[TerritoryLabelRegion, ...]:
    """Aggregate mutually connected cells into coalition label regions."""
    by_name = {cell.system_name.casefold(): cell for cell in cells}
    adjacency: dict[str, set[str]] = {key: set() for key in by_name}
    for key, cell in by_name.items():
        own_neighbors = {name.casefold() for name in cell.same_coalition_neighbors}
        for neighbor_key in own_neighbors:
            neighbor = by_name.get(neighbor_key)
            if (
                neighbor is None
                or neighbor.coalition_id != cell.coalition_id
                or key
                not in {name.casefold() for name in neighbor.same_coalition_neighbors}
            ):
                continue
            adjacency[key].add(neighbor_key)
            adjacency[neighbor_key].add(key)

    regions: list[TerritoryLabelRegion] = []
    unseen = set(by_name)
    while unseen:
        first = min(unseen)
        coalition_id = by_name[first].coalition_id
        component = set()
        frontier = [first]
        while frontier:
            key = frontier.pop()
            if key not in unseen or by_name[key].coalition_id != coalition_id:
                continue
            unseen.remove(key)
            component.add(key)
            frontier.extend(adjacency[key] - component)

        component_cells = [by_name[key] for key in sorted(component)]
        metrics = [
            (*_polygon_area_centroid(cell.polygon), cell)
            for cell in component_cells
        ]
        total_area = sum(area for area, _centroid, _cell in metrics)
        if total_area > _EPSILON:
            anchor = (
                sum(area * centroid[0] for area, centroid, _cell in metrics)
                / total_area,
                sum(area * centroid[1] for area, centroid, _cell in metrics)
                / total_area,
            )
        else:
            anchor = metrics[0][1]
        if not any(_point_in_polygon(anchor, cell.polygon) for cell in component_cells):
            _area, anchor, _cell = max(metrics, key=lambda row: row[0])

        all_points = [point for cell in component_cells for point in cell.polygon]
        regions.append(
            TerritoryLabelRegion(
                coalition_id=coalition_id,
                coalition_name=component_cells[0].coalition_name,
                color=component_cells[0].color,
                system_names=tuple(
                    sorted(
                        (cell.system_name for cell in component_cells),
                        key=str.casefold,
                    )
                ),
                area=total_area,
                anchor=anchor,
                bounds=(
                    min(point[0] for point in all_points),
                    min(point[1] for point in all_points),
                    max(point[0] for point in all_points),
                    max(point[1] for point in all_points),
                ),
                label_path=_build_centered_label_path(component_cells, anchor),
            )
        )
    regions.sort(key=lambda region: (region.coalition_name.casefold(), region.system_names))
    return tuple(regions)


def territory_label_font_pixels(area: float, map_scale: float) -> int:
    """Scale label type by the region's on-screen linear footprint."""
    linear_pixels = math.sqrt(max(0.0, float(area))) * max(0.0, float(map_scale))
    return max(10, min(42, int(round(8.0 + linear_pixels * 0.08))))


def _normalise_hex_color(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if not text.startswith("#"):
        text = "#" + text
    if len(text) != 7 or any(char not in "0123456789abcdef" for char in text[1:]):
        return None
    return text


def territory_rgb(entry: Mapping | None) -> tuple[int, int, int]:
    """Resolve a public coalition colour with a neutral fallback."""
    color = _normalise_hex_color(entry.get("color") if entry else None)
    if color is None:
        return DEFAULT_TERRITORY_COLOR
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def normalize_territory_snapshot(raw) -> dict[str, dict]:
    """Validate the compact global territory payload received from the game."""
    if not isinstance(raw, dict):
        return {}
    snapshot: dict[str, dict] = {}
    seen: set[str] = set()
    for raw_name, raw_entry in raw.items():
        if not isinstance(raw_entry, dict):
            continue
        name = str(raw_name).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        try:
            coalition_id = int(raw_entry.get("coalition_id"))
        except (TypeError, ValueError):
            continue
        if coalition_id <= 0:
            continue
        seen.add(key)
        snapshot[name] = {
            "coalition_id": coalition_id,
            "coalition_name": str(raw_entry.get("coalition_name") or "").strip(),
            "color": _normalise_hex_color(raw_entry.get("color")),
        }
    return snapshot


def normalize_positions_snapshot(raw) -> dict[str, dict[str, float]]:
    """Retain only finite public galaxy coordinates, keyed by system name."""
    if not isinstance(raw, Mapping):
        return {}
    positions: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for raw_name, raw_position in raw.items():
        if not isinstance(raw_position, Mapping):
            continue
        name = str(raw_name).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        try:
            x = float(raw_position.get("coord_x"))
            y = float(raw_position.get("coord_y"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        seen.add(key)
        positions[name] = {"coord_x": x, "coord_y": y}
    return positions


def _clip_half_plane(
    polygon: list[tuple[float, float]],
    a: float,
    b: float,
    c: float,
) -> list[tuple[float, float]]:
    """Clip polygon to a*x + b*y <= c (Sutherland-Hodgman)."""
    if not polygon:
        return []
    result: list[tuple[float, float]] = []
    previous = polygon[-1]
    previous_value = a * previous[0] + b * previous[1] - c
    previous_inside = previous_value <= _EPSILON
    for current in polygon:
        current_value = a * current[0] + b * current[1] - c
        current_inside = current_value <= _EPSILON
        if current_inside != previous_inside:
            denominator = previous_value - current_value
            if abs(denominator) > _EPSILON:
                amount = previous_value / denominator
                result.append(
                    (
                        previous[0] + (current[0] - previous[0]) * amount,
                        previous[1] + (current[1] - previous[1]) * amount,
                    )
                )
        if current_inside:
            result.append(current)
        previous = current
        previous_value = current_value
        previous_inside = current_inside
    return result


def _position_rows(positions: Mapping) -> list[tuple[str, float, float]]:
    rows: list[tuple[str, float, float]] = []
    for raw_name, raw_position in positions.items():
        if not isinstance(raw_position, Mapping):
            continue
        try:
            x = float(raw_position.get("coord_x", 0.0))
            y = float(raw_position.get("coord_y", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        name = str(raw_name).strip()
        if name:
            rows.append((name, x, y))
    rows.sort(key=lambda row: (row[0].casefold(), row[0]))
    return rows


def _convex_hull(raw_points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    points = sorted(set(raw_points))
    if len(points) <= 2:
        return points

    def cross(origin, point_a, point_b):
        return (
            (point_a[0] - origin[0]) * (point_b[1] - origin[1])
            - (point_a[1] - origin[1]) * (point_b[0] - origin[0])
        )

    lower = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= _EPSILON:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= _EPSILON:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _clip_to_convex_envelope(
    polygon: list[tuple[float, float]],
    envelope: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    result = polygon
    for index, (x1, y1) in enumerate(envelope):
        x2, y2 = envelope[(index + 1) % len(envelope)]
        edge_x = x2 - x1
        edge_y = y2 - y1
        result = _clip_half_plane(
            result,
            edge_y,
            -edge_x,
            edge_y * x1 - edge_x * y1,
        )
        if len(result) < 3:
            return []
    return result


def build_territory_cells(
    positions: Mapping[str, Mapping],
    territory: Mapping[str, Mapping],
) -> tuple[TerritoryCell, ...]:
    """Build non-overlapping claimed-system cells in galaxy coordinates."""
    points = _position_rows(positions)
    entries = normalize_territory_snapshot(dict(territory))
    if not points or not entries:
        return ()

    xs = [row[1] for row in points]
    ys = [row[2] for row in points]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    span = max(span_x, span_y, 1.0)
    density_spacing = span / max(2.0, math.sqrt(len(points)))
    padding = max(0.25, density_spacing * 0.8)
    min_x = min(xs) - padding
    min_y = min(ys) - padding
    max_x = max(xs) + padding
    max_y = max(ys) + padding
    entries_by_key = {name.casefold(): entry for name, entry in entries.items()}

    claimed_by_coalition: dict[int, list[tuple[int, str, float, float]]] = {}
    for point_index, (system_name, px, py) in enumerate(points):
        entry = entries_by_key.get(system_name.casefold())
        if entry is not None:
            claimed_by_coalition.setdefault(int(entry["coalition_id"]), []).append(
                (point_index, system_name, px, py)
            )

    envelope_by_coalition: dict[int, list[tuple[float, float]]] = {}
    cap_sides = 24
    for coalition_id, claimed_rows in claimed_by_coalition.items():
        envelope_points = []
        for point_index, _system_name, px, py in claimed_rows:
            nearest_distance_sq = min(
                (
                    (qx - px) ** 2 + (qy - py) ** 2
                    for other_index, (_name, qx, qy) in enumerate(points)
                    if other_index != point_index
                    and (qx - px) ** 2 + (qy - py) ** 2 > _EPSILON
                ),
                default=max(density_spacing, 1.0e-3) ** 2,
            )
            reach = max(1.0e-4, math.sqrt(nearest_distance_sq) * 0.9)
            envelope_points.extend(
                (
                    px + reach * math.cos(2.0 * math.pi * side / cap_sides),
                    py + reach * math.sin(2.0 * math.pi * side / cap_sides),
                )
                for side in range(cap_sides)
            )
        envelope_by_coalition[coalition_id] = _convex_hull(envelope_points)

    cells: list[TerritoryCell] = []
    for point_index, (system_name, px, py) in enumerate(points):
        entry = entries_by_key.get(system_name.casefold())
        if entry is None:
            continue

        others = sorted(
            (
                (
                    other_index,
                    other_name,
                    qx,
                    qy,
                    (qx - px) ** 2 + (qy - py) ** 2,
                )
                for other_index, (other_name, qx, qy) in enumerate(points)
                if other_index != point_index
            ),
            key=lambda row: row[4],
        )
        polygon = [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]
        duplicate_lost = False
        processed_others: list[tuple[str, float, float]] = []
        max_radius_sq = max((x - px) ** 2 + (y - py) ** 2 for x, y in polygon)
        for other_index, other_name, qx, qy, distance_sq in others:
            if distance_sq <= _EPSILON:
                if other_index < point_index:
                    duplicate_lost = True
                    polygon = []
                    break
                continue
            if distance_sq > 4.0 * max_radius_sq + _EPSILON:
                break
            processed_others.append((other_name, qx, qy))
            a = qx - px
            b = qy - py
            c = (qx * qx + qy * qy - px * px - py * py) * 0.5
            polygon = _clip_half_plane(polygon, a, b, c)
            if len(polygon) < 3:
                polygon = []
                break
            max_radius_sq = max((x - px) ** 2 + (y - py) ** 2 for x, y in polygon)
        if duplicate_lost or len(polygon) < 3:
            continue

        coalition_id = int(entry["coalition_id"])

        def bisecting_other(point_a, point_b, tolerance: float):
            for candidate in processed_others:
                _, qx, qy = candidate
                delta_a = abs(
                    (point_a[0] - px) ** 2
                    + (point_a[1] - py) ** 2
                    - (point_a[0] - qx) ** 2
                    - (point_a[1] - qy) ** 2
                )
                delta_b = abs(
                    (point_b[0] - px) ** 2
                    + (point_b[1] - py) ** 2
                    - (point_b[0] - qx) ** 2
                    - (point_b[1] - qy) ** 2
                )
                if delta_a <= tolerance and delta_b <= tolerance:
                    return candidate
            return None

        polygon = _clip_to_convex_envelope(
            polygon, envelope_by_coalition[coalition_id]
        )
        if len(polygon) < 3:
            continue

        boundary_segments = []
        same_coalition_neighbors = set()
        cell_radius_sq = max((x - px) ** 2 + (y - py) ** 2 for x, y in polygon)
        bisector_tolerance = max(1.0e-10, cell_radius_sq * 1.0e-7)
        for edge_index, point_a in enumerate(polygon):
            point_b = polygon[(edge_index + 1) % len(polygon)]
            nearest_other = bisecting_other(point_a, point_b, bisector_tolerance)
            if nearest_other is None:
                boundary_segments.append((point_a, point_b))
                continue
            other_entry = entries_by_key.get(nearest_other[0].casefold())
            other_coalition_id = (
                int(other_entry["coalition_id"]) if other_entry is not None else None
            )
            if other_coalition_id != coalition_id:
                boundary_segments.append((point_a, point_b))
            else:
                same_coalition_neighbors.add(nearest_other[0])

        cells.append(
            TerritoryCell(
                system_name=system_name,
                coalition_id=coalition_id,
                coalition_name=str(entry.get("coalition_name") or ""),
                color=territory_rgb(entry),
                polygon=tuple(polygon),
                boundary_segments=tuple(boundary_segments),
                same_coalition_neighbors=tuple(
                    sorted(same_coalition_neighbors, key=str.casefold)
                ),
            )
        )
    return tuple(cells)
