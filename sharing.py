from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archive_store import ArchiveStore
from map_territory import normalize_positions_snapshot, normalize_territory_snapshot


BUNDLE_FORMAT = "star-empire-companion-shared-intel"
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 30 * 1024 * 1024


class SharedIntelError(ValueError):
    """A selected shared-intel file is not safe or compatible to import."""


ITEM_FIELDS = (
    "id", "type", "name", "displayName", "category", "categoryLabel",
    "tech", "rarity", "description", "cargoSize", "stats", "flags",
    "art", "illustration",
)
MARKET_FIELDS = (
    "stationId", "stationName", "source", "sourceLabel", "systemName",
    "buyPrice", "sellPrice", "stock", "minimum", "maximum", "noSell",
    "observedAt",
)
STATION_FIELDS = (
    "id", "name", "sources", "systemName", "knownSystems", "itemIds",
    "itemCount", "pricedItemCount", "lastSeen",
)
SCAN_FIELDS = (
    "ok", "planet_id", "planet_name", "planet_type", "system_name",
    "is_moon", "isScanned",
    "resources", "extractors", "colonization", "scan_range", "scan_radius",
    "orbital_period", "gravity", "atmosphere", "temperature", "water",
    "observedAt",
)
TRAINING_FIELDS = (
    "skillId", "stationId", "stationName", "systemName", "source",
    "offeredMax", "itemCostClass", "itemCostType", "itemCostDisplay",
    "itemCostAmount", "itemCostScale", "observedAt", "displayName",
    "description", "globalMax", "statBonus", "pctBonus",
)
SYSTEM_FIELDS = (
    "id", "name", "x", "y", "hasPosition", "explored", "hazard",
    "hazardKnown", "planetTypes", "moonCount", "ownership", "npcStationCount",
)
EDGE_FIELDS = ("source", "target")


def _fields(record: Any, names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {
        name: copy.deepcopy(record[name])
        for name in names
        if name in record and record[name] is not None
    }


def _records(records: Any, sanitise) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [result for result in (sanitise(record) for record in records) if result]


def _item(record: Any) -> dict[str, Any]:
    item = _fields(record, ITEM_FIELDS)
    if not item.get("id") and not item.get("type") and not item.get("name"):
        return {}
    item["markets"] = _records(
        record.get("markets") if isinstance(record, dict) else [],
        lambda market: _fields(market, MARKET_FIELDS),
    )
    return item


def _station(record: Any) -> dict[str, Any]:
    station = _fields(record, STATION_FIELDS)
    return station if station.get("id") or station.get("name") else {}


def _scan(record: Any) -> dict[str, Any]:
    scan = _fields(record, SCAN_FIELDS)
    if not scan.get("planet_id") and not scan.get("planet_name"):
        return {}
    # An entry roster may identify a body, but contains no scan result. Keep
    # that distinction trustworthy even when importing a manually edited file.
    scanned = bool(scan.get("isScanned")) if "isScanned" in scan else scan.get("ok") is not False
    scan["isScanned"] = scanned
    if not scanned:
        for field in (
            "resources", "extractors", "colonization", "scan_range",
            "scan_radius", "orbital_period", "gravity", "atmosphere",
            "temperature", "water", "ok",
        ):
            scan.pop(field, None)
    return scan


def _training(record: Any) -> dict[str, Any]:
    offer = _fields(record, TRAINING_FIELDS)
    return offer if offer.get("skillId") and offer.get("stationId") else {}


def _system(record: Any) -> dict[str, Any]:
    system = _fields(record, SYSTEM_FIELDS)
    raw_counts = record.get("stationCounts") if isinstance(record, dict) else None
    if isinstance(raw_counts, dict):
        # Station IDs and personal/mine counts are private.  Coalition and
        # other-station totals are public map observations and let shared maps
        # retain their public control context.
        public_counts = {
            key: value
            for key, value in raw_counts.items()
            if key in {"coalition", "others"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        }
        if public_counts:
            system["stationCounts"] = public_counts
    return system if system.get("id") or system.get("name") else {}


def _edge(record: Any) -> dict[str, Any]:
    edge = _fields(record, EDGE_FIELDS)
    return edge if edge.get("source") and edge.get("target") else {}


def sanitise_catalog(catalog: dict[str, Any] | None) -> dict[str, Any]:
    """Return only observation data that is appropriate for community sharing.

    This boundary deliberately excludes player, ship, inventory, account,
    personal annotation, and ownership-status fields. It is applied to both
    outgoing and incoming bundles so an edited import cannot add private data
    to the local archive.
    """
    source = catalog if isinstance(catalog, dict) else {}
    map_source = source.get("map") if isinstance(source.get("map"), dict) else {}
    training_source = source.get("training") if isinstance(source.get("training"), dict) else {}
    systems = _records(map_source.get("systems"), _system)
    edges = _records(map_source.get("edges"), _edge)
    territory = normalize_territory_snapshot(map_source.get("territory"))
    territory_positions = normalize_positions_snapshot(
        map_source.get("territoryPositions")
    )
    map_data = {
        "hasData": bool(systems or territory),
        "observedAt": map_source.get("observedAt"),
        "systems": systems,
        "edges": edges,
        "territory": territory,
        "territoryPositions": territory_positions,
        "territoryCount": len(territory),
    }
    return {
        "meta": {
            "sharedIntel": True,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "items": _records(source.get("items"), _item),
        "stations": _records(source.get("stations"), _station),
        "scans": _records(source.get("scans"), _scan),
        "training": {"offers": _records(training_source.get("offers"), _training)},
        "player": {"hasData": False},
        "ship": {"hasData": False},
        "map": map_data,
    }


def bundle_summary(catalog: dict[str, Any]) -> dict[str, int]:
    map_data = catalog.get("map") if isinstance(catalog.get("map"), dict) else {}
    training = catalog.get("training") if isinstance(catalog.get("training"), dict) else {}
    return {
        "items": len(catalog.get("items") or []),
        "stations": len(catalog.get("stations") or []),
        "scans": len(catalog.get("scans") or []),
        "trainingOffers": len(training.get("offers") or []),
        "systems": len(map_data.get("systems") or []),
        "edges": len(map_data.get("edges") or []),
        "territorySystems": len(map_data.get("territory") or {}),
    }


def create_bundle(catalog: dict[str, Any] | None) -> dict[str, Any]:
    shared_catalog = sanitise_catalog(catalog)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": bundle_summary(shared_catalog),
        "catalog": shared_catalog,
    }


def write_bundle(path: Path, catalog: dict[str, Any] | None) -> dict[str, Any]:
    """Create a user-selected, portable shared-intel JSON file atomically."""
    destination = Path(path)
    bundle = create_bundle(catalog)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(bundle, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return bundle


def read_bundle(path: Path) -> dict[str, Any]:
    source = Path(path)
    try:
        if source.stat().st_size > MAX_BUNDLE_BYTES:
            raise SharedIntelError("The shared-intel file is larger than 30 MB.")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except SharedIntelError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SharedIntelError(f"Could not read the shared-intel file: {error}") from error
    if not isinstance(payload, dict):
        raise SharedIntelError("The shared-intel file must contain a JSON object.")
    if payload.get("format") != BUNDLE_FORMAT:
        raise SharedIntelError("This is not a Star Empire Companion shared-intel file.")
    if payload.get("version") != BUNDLE_VERSION:
        raise SharedIntelError(
            f"Unsupported shared-intel version: {payload.get('version')!r}."
        )
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict):
        raise SharedIntelError("The shared-intel file does not contain an intel catalog.")
    clean = sanitise_catalog(catalog)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "createdAt": str(payload.get("createdAt") or ""),
        "summary": bundle_summary(clean),
        "catalog": clean,
    }


def import_bundle(path: Path, archive: ArchiveStore) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a bundle and merge its safe observations into the local archive."""
    bundle = read_bundle(path)
    merged = archive.merge(bundle["catalog"], map_authoritative=False)
    return bundle, merged
