from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


STATE_VERSION = 1


def default_state_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "StarEmpireCompanion" / "user_data.json"


def scan_annotation_key(scan: dict[str, Any]) -> str:
    value = scan.get("planet_id") or scan.get("planet_name") or "unknown-planet"
    return str(value).strip() or "unknown-planet"


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "settings": {}, "scanAnnotations": {}, "tableLayouts": {}, "recordAnnotations": {}, "savedFittings": {}, "mapView": {}, "savedSearches": {}, "extractionSystems": []}


def _clean_settings(value: Any) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    game_directory = str(source.get("gameDirectory") or "").replace("\r", " ").replace("\n", " ").strip()
    return {"gameDirectory": game_directory[:2048]}


def _clean_annotation(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    try:
        base_count = max(0, int(source.get("baseCount") or 0))
    except (TypeError, ValueError):
        base_count = 0
    has_base = bool(source.get("hasBase")) or base_count > 0
    system_name = str(source.get("systemName") or "").strip()
    return {
        "systemName": system_name,
        "hasBase": has_base,
        "baseCount": base_count,
    }


def _clean_table_layout(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    columns: list[str] = []
    seen: set[str] = set()
    raw_columns = source.get("columns")
    if isinstance(raw_columns, list):
        for raw_column in raw_columns[:64]:
            column = str(raw_column or "").strip()[:80]
            if column and column not in seen:
                seen.add(column)
                columns.append(column)

    widths: dict[str, int] = {}
    raw_widths = source.get("widths")
    if isinstance(raw_widths, dict):
        for raw_column, raw_width in list(raw_widths.items())[:64]:
            column = str(raw_column or "").strip()[:80]
            if not column:
                continue
            try:
                width = int(raw_width)
            except (TypeError, ValueError):
                continue
            widths[column] = min(2000, max(32, width))

    sort_column = str(source.get("sortColumn") or "").strip()[:80]
    try:
        xview = float(source.get("xview") or 0.0)
    except (TypeError, ValueError):
        xview = 0.0
    return {
        "columns": columns,
        "widths": widths,
        "sortColumn": sort_column,
        "sortDescending": bool(source.get("sortDescending")),
        "xview": min(1.0, max(0.0, xview)),
    }


def record_annotation_key(record_type: str, record_id: Any) -> str:
    kind = str(record_type or "record").strip().casefold()[:40] or "record"
    identifier = str(record_id or "unknown").strip()[:240] or "unknown"
    return f"{kind}::{identifier}"


def _clean_record_annotation(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    tags: list[str] = []
    seen: set[str] = set()
    raw_tags = source.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.split(",")
    if isinstance(raw_tags, list):
        for raw_tag in raw_tags[:40]:
            tag = str(raw_tag or "").strip()[:60]
            folded = tag.casefold()
            if tag and folded not in seen:
                seen.add(folded)
                tags.append(tag)
    return {
        "favorite": bool(source.get("favorite")),
        "watchlist": bool(source.get("watchlist")),
        "category": str(source.get("category") or "").strip()[:80],
        "tags": tags,
        "note": str(source.get("note") or "").strip()[:4000],
    }


def _clean_fit_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    stats = value.get("stats") if isinstance(value.get("stats"), dict) else {}
    cleaned_stats = {
        str(key)[:80]: raw_value
        for key, raw_value in list(stats.items())[:100]
        if isinstance(raw_value, (str, int, float, bool)) or raw_value is None
    }
    return {
        "id": str(value.get("id") or "")[:240],
        "type": str(value.get("type") or "")[:160],
        "category": str(value.get("category") or "equipment")[:80],
        "name": str(value.get("name") or value.get("display_name") or "Unknown item")[:200],
        "tech": value.get("tech") if isinstance(value.get("tech"), (int, float)) else None,
        "cargoSize": value.get("cargoSize") if isinstance(value.get("cargoSize"), (int, float)) else None,
        "stats": cleaned_stats,
    }


def _clean_saved_fit_state(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    def slot_count(raw: Any) -> int:
        try:
            return min(64, max(0, int(raw or 0)))
        except (TypeError, ValueError):
            return 0

    weapons = source.get("weaponSlots") if isinstance(source.get("weaponSlots"), list) else []
    plugins = source.get("pluginSlotsList") if isinstance(source.get("pluginSlotsList"), list) else []
    auxiliary = source.get("aux") if isinstance(source.get("aux"), list) else []
    cleaned_auxiliary = []
    for entry in auxiliary[:64]:
        if not isinstance(entry, dict):
            continue
        cleaned_auxiliary.append(
            {
                "slot": str(entry.get("slot") or "auxiliary")[:80],
                "category": str(entry.get("category") or "equipment")[:80],
                "item": _clean_fit_item(entry.get("item")),
                "locked": bool(entry.get("locked")),
            }
        )
    return {
        "hull": _clean_fit_item(source.get("hull")),
        "engine": _clean_fit_item(source.get("engine")),
        "shield": _clean_fit_item(source.get("shield")),
        "energy": _clean_fit_item(source.get("energy")),
        "weaponSlots": [_clean_fit_item(item) for item in weapons[:64]],
        "pluginSlotsList": [_clean_fit_item(item) for item in plugins[:64]],
        "aux": cleaned_auxiliary,
        "maxWeaponSlots": slot_count(source.get("maxWeaponSlots")),
        "pluginSlots": slot_count(source.get("pluginSlots")),
        "applySkills": bool(source.get("applySkills", True)),
    }


def _clean_saved_fitting(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "name": str(source.get("name") or "Saved fit").strip()[:120] or "Saved fit",
        "updatedAt": str(source.get("updatedAt") or "")[:40],
        "state": _clean_saved_fit_state(source.get("state")),
    }


def _clean_map_view(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    def bounded_float(raw: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            return min(maximum, max(minimum, float(raw)))
        except (TypeError, ValueError):
            return default

    positions: dict[str, list[int]] = {}
    raw_positions = source.get("overlayPositions")
    if isinstance(raw_positions, dict):
        for key, pair in raw_positions.items():
            if key not in {"controls", "search", "detail", "legend"} or not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                positions[key] = [max(-10000, min(10000, int(pair[0]))), max(-10000, min(10000, int(pair[1])))]
            except (TypeError, ValueError):
                continue
    sizes: dict[str, list[int]] = {}
    raw_sizes = source.get("overlaySizes")
    if isinstance(raw_sizes, dict):
        pair = raw_sizes.get("detail")
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            try:
                sizes["detail"] = [max(285, min(3000, int(pair[0]))), max(240, min(3000, int(pair[1])))]
            except (TypeError, ValueError):
                pass
    mode = str(source.get("mode") or "Everything")[:40]
    return {
        "zoom": bounded_float(source.get("zoom"), 1.0, 0.25, 64.0),
        "panX": bounded_float(source.get("panX"), 0.0, -10000000.0, 10000000.0),
        "panY": bounded_float(source.get("panY"), 0.0, -10000000.0, 10000000.0),
        "showNames": bool(source.get("showNames", True)),
        "showCoalitionControl": bool(source.get("showCoalitionControl", True)),
        "selectedSystem": str(source.get("selectedSystem") or "")[:160],
        "mode": mode,
        "overlayPositions": positions,
        "overlaySizes": sizes,
    }


def _clean_extraction_systems(value: Any) -> list[str]:
    """Keep an ordered, local-only list of systems saved for extraction review."""
    rows = value if isinstance(value, list) else []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_name in rows[:512]:
        name = str(raw_name or "").replace("\r", " ").replace("\n", " ").strip()[:160]
        folded = name.casefold()
        if name and folded not in seen:
            seen.add(folded)
            cleaned.append(name)
    return cleaned


def _clean_saved_search(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "name": str(source.get("name") or "Saved search").strip()[:120] or "Saved search",
        "scope": str(source.get("scope") or "Everything").strip()[:40] or "Everything",
        "query": str(source.get("query") or "").strip()[:500],
        "updatedAt": str(source.get("updatedAt") or "")[:40],
    }


class UserStateStore:
    """Local preferences, annotations, saved fits, and saved searches."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_path()
        self._lock = threading.RLock()
        self._state: dict[str, Any] | None = None

    def load(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._state is not None and not force:
                return self._state
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                payload = _empty_state()

            annotations = payload.get("scanAnnotations") if isinstance(payload, dict) else None
            cleaned = {
                str(key): _clean_annotation(value)
                for key, value in annotations.items()
            } if isinstance(annotations, dict) else {}
            layouts = payload.get("tableLayouts") if isinstance(payload, dict) else None
            cleaned_layouts = {
                str(key): _clean_table_layout(value)
                for key, value in layouts.items()
                if str(key).strip()
            } if isinstance(layouts, dict) else {}
            records = payload.get("recordAnnotations") if isinstance(payload, dict) else None
            cleaned_records = {
                str(key): _clean_record_annotation(value)
                for key, value in records.items()
                if str(key).strip()
            } if isinstance(records, dict) else {}
            fittings = payload.get("savedFittings") if isinstance(payload, dict) else None
            cleaned_fittings = {
                str(key): _clean_saved_fitting(value)
                for key, value in fittings.items()
                if str(key).strip()
            } if isinstance(fittings, dict) else {}
            map_view = _clean_map_view(payload.get("mapView") if isinstance(payload, dict) else None)
            searches = payload.get("savedSearches") if isinstance(payload, dict) else None
            cleaned_searches = {
                str(key): _clean_saved_search(value)
                for key, value in searches.items()
                if str(key).strip()
            } if isinstance(searches, dict) else {}
            settings = _clean_settings(payload.get("settings") if isinstance(payload, dict) else None)
            extraction_systems = _clean_extraction_systems(payload.get("extractionSystems") if isinstance(payload, dict) else None)
            self._state = {
                "version": STATE_VERSION,
                "settings": settings,
                "scanAnnotations": cleaned,
                "tableLayouts": cleaned_layouts,
                "recordAnnotations": cleaned_records,
                "savedFittings": cleaned_fittings,
                "mapView": map_view,
                "savedSearches": cleaned_searches,
                "extractionSystems": extraction_systems,
            }
            return self._state

    def game_directory(self) -> str:
        with self._lock:
            return _clean_settings(self.load().get("settings"))["gameDirectory"]

    def set_game_directory(self, value: str | Path) -> str:
        game_directory = _clean_settings({"gameDirectory": str(value)})["gameDirectory"]
        with self._lock:
            state = self.load()
            state["settings"] = {"gameDirectory": game_directory}
            self._write_atomic(state)
        return game_directory

    def scan_annotation(self, scan: dict[str, Any] | str) -> dict[str, Any]:
        key = scan if isinstance(scan, str) else scan_annotation_key(scan)
        with self._lock:
            annotation = self.load()["scanAnnotations"].get(str(key))
            return dict(_clean_annotation(annotation))

    def set_scan_annotation(
        self,
        scan: dict[str, Any] | str,
        *,
        system_name: str = "",
        has_base: bool = False,
        base_count: int = 0,
    ) -> dict[str, Any]:
        key = scan if isinstance(scan, str) else scan_annotation_key(scan)
        annotation = _clean_annotation(
            {
                "systemName": system_name,
                "hasBase": has_base,
                "baseCount": base_count,
            }
        )
        with self._lock:
            state = self.load()
            state["scanAnnotations"][str(key)] = annotation
            self._write_atomic(state)
        return dict(annotation)

    def table_layout(self, name: str) -> dict[str, Any]:
        with self._lock:
            layout = self.load()["tableLayouts"].get(str(name))
            cleaned = _clean_table_layout(layout)
            return {
                **cleaned,
                "columns": list(cleaned["columns"]),
                "widths": dict(cleaned["widths"]),
            }

    def set_table_layout(
        self,
        name: str,
        *,
        columns: list[str],
        widths: dict[str, int],
        sort_column: str = "",
        sort_descending: bool = False,
        xview: float = 0.0,
    ) -> dict[str, Any]:
        key = str(name or "").strip()
        if not key:
            raise ValueError("Table layout name cannot be empty")
        layout = _clean_table_layout(
            {
                "columns": columns,
                "widths": widths,
                "sortColumn": sort_column,
                "sortDescending": sort_descending,
                "xview": xview,
            }
        )
        with self._lock:
            state = self.load()
            state["tableLayouts"][key] = layout
            self._write_atomic(state)
        return {
            **layout,
            "columns": list(layout["columns"]),
            "widths": dict(layout["widths"]),
        }

    def record_annotation(self, record_type: str, record_id: Any) -> dict[str, Any]:
        key = record_annotation_key(record_type, record_id)
        with self._lock:
            return dict(_clean_record_annotation(self.load()["recordAnnotations"].get(key)))

    def set_record_annotation(
        self,
        record_type: str,
        record_id: Any,
        *,
        favorite: bool = False,
        watchlist: bool = False,
        category: str = "",
        tags: list[str] | str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        key = record_annotation_key(record_type, record_id)
        annotation = _clean_record_annotation(
            {
                "favorite": favorite,
                "watchlist": watchlist,
                "category": category,
                "tags": tags if tags is not None else [],
                "note": note,
            }
        )
        with self._lock:
            state = self.load()
            state["recordAnnotations"][key] = annotation
            self._write_atomic(state)
        return {**annotation, "tags": list(annotation["tags"])}

    def personal_categories(self, record_type: str | None = None) -> list[str]:
        prefix = f"{str(record_type).strip().casefold()}::" if record_type else ""
        with self._lock:
            categories = {
                str(value.get("category") or "").strip()
                for key, value in self.load()["recordAnnotations"].items()
                if (not prefix or key.startswith(prefix)) and str(value.get("category") or "").strip()
            }
        return sorted(categories, key=str.casefold)

    def saved_fittings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                {"id": key, **_clean_saved_fitting(value)}
                for key, value in self.load()["savedFittings"].items()
            ]
        rows.sort(key=lambda row: (str(row.get("name") or "").casefold(), str(row.get("id") or "")))
        return rows

    def save_fitting(self, name: str, state: dict[str, Any], fitting_id: str | None = None) -> dict[str, Any]:
        identifier = str(fitting_id or uuid.uuid4().hex).strip()[:120]
        if not identifier:
            raise ValueError("Saved fitting ID cannot be empty")
        fitting = _clean_saved_fitting(
            {
                "name": name,
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "state": state,
            }
        )
        with self._lock:
            current = self.load()
            current["savedFittings"][identifier] = fitting
            self._write_atomic(current)
        return {"id": identifier, **_clean_saved_fitting(fitting)}

    def delete_fitting(self, fitting_id: str) -> bool:
        identifier = str(fitting_id or "").strip()
        with self._lock:
            state = self.load()
            if identifier not in state["savedFittings"]:
                return False
            del state["savedFittings"][identifier]
            self._write_atomic(state)
        return True

    def map_view(self) -> dict[str, Any]:
        with self._lock:
            cleaned = _clean_map_view(self.load().get("mapView"))
        return {
            **cleaned,
            "overlayPositions": {key: list(value) for key, value in cleaned["overlayPositions"].items()},
            "overlaySizes": {key: list(value) for key, value in cleaned["overlaySizes"].items()},
        }

    def set_map_view(
        self,
        *,
        zoom: float,
        pan_x: float,
        pan_y: float,
        show_names: bool,
        show_coalition_control: bool,
        selected_system: str = "",
        mode: str = "Everything",
        overlay_positions: dict[str, tuple[int, int]] | None = None,
        overlay_sizes: dict[str, tuple[int, int]] | None = None,
    ) -> dict[str, Any]:
        view = _clean_map_view(
            {
                "zoom": zoom,
                "panX": pan_x,
                "panY": pan_y,
                "showNames": show_names,
                "showCoalitionControl": show_coalition_control,
                "selectedSystem": selected_system,
                "mode": mode,
                "overlayPositions": overlay_positions or {},
                "overlaySizes": overlay_sizes or {},
            }
        )
        with self._lock:
            state = self.load()
            state["mapView"] = view
            self._write_atomic(state)
        return {
            **view,
            "overlayPositions": {key: list(value) for key, value in view["overlayPositions"].items()},
            "overlaySizes": {key: list(value) for key, value in view["overlaySizes"].items()},
        }

    def extraction_systems(self) -> list[str]:
        with self._lock:
            return list(_clean_extraction_systems(self.load().get("extractionSystems")))

    def add_extraction_system(self, system_name: str) -> bool:
        names = _clean_extraction_systems([system_name])
        if not names:
            return False
        name = names[0]
        with self._lock:
            state = self.load()
            current = _clean_extraction_systems(state.get("extractionSystems"))
            if any(existing.casefold() == name.casefold() for existing in current):
                return False
            state["extractionSystems"] = [*current, name]
            self._write_atomic(state)
        return True

    def remove_extraction_system(self, system_name: str) -> bool:
        target = str(system_name or "").strip().casefold()
        if not target:
            return False
        with self._lock:
            state = self.load()
            current = _clean_extraction_systems(state.get("extractionSystems"))
            updated = [name for name in current if name.casefold() != target]
            if len(updated) == len(current):
                return False
            state["extractionSystems"] = updated
            self._write_atomic(state)
        return True

    def saved_searches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                {"id": key, **_clean_saved_search(value)}
                for key, value in self.load()["savedSearches"].items()
            ]
        rows.sort(key=lambda row: (str(row.get("name") or "").casefold(), str(row.get("id") or "")))
        return rows

    def save_search(self, name: str, scope: str, query: str, search_id: str | None = None) -> dict[str, Any]:
        identifier = str(search_id or uuid.uuid4().hex).strip()[:120]
        if not identifier:
            raise ValueError("Saved search ID cannot be empty")
        search = _clean_saved_search(
            {
                "name": name,
                "scope": scope,
                "query": query,
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        with self._lock:
            state = self.load()
            state["savedSearches"][identifier] = search
            self._write_atomic(state)
        return {"id": identifier, **_clean_saved_search(search)}

    def delete_search(self, search_id: str) -> bool:
        identifier = str(search_id or "").strip()
        with self._lock:
            state = self.load()
            if identifier not in state["savedSearches"]:
                return False
            del state["savedSearches"][identifier]
            self._write_atomic(state)
        return True

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_atomic(self, state: dict[str, Any]) -> None:
        self._write_json_atomic(self.path, state)
