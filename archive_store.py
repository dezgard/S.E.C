from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any


ARCHIVE_VERSION = 1


def default_archive_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "StarEmpireCompanion" / "archive.json"


def _record_key(record: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def _newer(left: dict[str, Any], right: dict[str, Any], field: str) -> dict[str, Any]:
    return right if str(right.get(field) or "") >= str(left.get(field) or "") else left


def _merge_market(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    winner = _newer(old, new, "observedAt")
    merged = copy.deepcopy(winner)
    if not merged.get("systemName"):
        merged["systemName"] = old.get("systemName") or new.get("systemName")
    if not merged.get("stationName"):
        merged["stationName"] = old.get("stationName") or new.get("stationName")
    return merged


def _merge_items(previous: list[Any], current: list[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw_item in [*previous, *current]:
        if not isinstance(raw_item, dict):
            continue
        item_id = _record_key(raw_item, "id", "type", "name")
        if not item_id:
            continue
        incoming = copy.deepcopy(raw_item)
        existing = merged.get(item_id)
        if existing is None:
            merged[item_id] = incoming
            continue
        combined = copy.deepcopy(existing)
        combined.update(incoming)
        combined["stats"] = {**(existing.get("stats") or {}), **(incoming.get("stats") or {})}
        markets: dict[str, dict[str, Any]] = {}
        for market in [*(existing.get("markets") or []), *(incoming.get("markets") or [])]:
            if not isinstance(market, dict):
                continue
            market_key = f"{market.get('stationId') or ''}|{market.get('source') or ''}"
            markets[market_key] = _merge_market(markets[market_key], market) if market_key in markets else copy.deepcopy(market)
        combined["markets"] = sorted(
            markets.values(),
            key=lambda row: (str(row.get("stationName") or "").casefold(), str(row.get("source") or "").casefold()),
        )
        flags = dict(existing.get("flags") or {})
        flags.update(incoming.get("flags") or {})
        flags["hasPrice"] = any(
            isinstance(market.get(field), (int, float)) and market.get(field) > 0
            for market in combined["markets"]
            for field in ("buyPrice", "sellPrice")
        )
        combined["flags"] = flags
        merged[item_id] = combined
    return sorted(merged.values(), key=lambda item: str(item.get("name") or "").casefold())


def _station_name_score(name: Any) -> tuple[int, int]:
    text = str(name or "").strip()
    generic = text.casefold().startswith("your station ·") or not text
    return (0 if generic else 1, len(text))


def _merge_stations(
    previous: list[Any],
    current: list[Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stations: dict[str, dict[str, Any]] = {}
    for raw_station in [*previous, *current]:
        if not isinstance(raw_station, dict):
            continue
        station_id = _record_key(raw_station, "id", "name")
        if not station_id:
            continue
        incoming = copy.deepcopy(raw_station)
        existing = stations.get(station_id)
        if existing is None:
            stations[station_id] = incoming
            continue
        winner = _newer(existing, incoming, "lastSeen")
        combined = copy.deepcopy(winner)
        names = [existing.get("name"), incoming.get("name")]
        combined["name"] = max(names, key=_station_name_score)
        combined["sources"] = sorted({*(existing.get("sources") or []), *(incoming.get("sources") or [])})
        known_systems = {
            *(existing.get("knownSystems") or []),
            *(incoming.get("knownSystems") or []),
            *([existing.get("systemName")] if existing.get("systemName") else []),
            *([incoming.get("systemName")] if incoming.get("systemName") else []),
        }
        combined["knownSystems"] = sorted(str(value) for value in known_systems if value)
        combined["systemName"] = incoming.get("systemName") or existing.get("systemName")
        combined["isMine"] = bool(existing.get("isMine") or incoming.get("isMine"))
        combined["lastSeen"] = max(str(existing.get("lastSeen") or ""), str(incoming.get("lastSeen") or "")) or None
        stations[station_id] = combined

    item_ids_by_station: dict[str, set[str]] = {}
    priced_ids_by_station: dict[str, set[str]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        for market in item.get("markets") or []:
            station_id = str(market.get("stationId") or "")
            if not station_id:
                continue
            item_ids_by_station.setdefault(station_id, set()).add(item_id)
            if any(isinstance(market.get(field), (int, float)) and market.get(field) > 0 for field in ("buyPrice", "sellPrice")):
                priced_ids_by_station.setdefault(station_id, set()).add(item_id)
            if station_id not in stations:
                stations[station_id] = {
                    "id": station_id,
                    "name": market.get("stationName") or f"Station {station_id}",
                    "sources": [market.get("sourceLabel") or market.get("source") or "Unknown"],
                    "systemName": market.get("systemName"),
                    "knownSystems": [market.get("systemName")] if market.get("systemName") else [],
                    "isMine": False,
                    "lastSeen": market.get("observedAt"),
                }

    results = []
    for station_id, station in stations.items():
        item_ids = item_ids_by_station.get(station_id, set()) | set(station.get("itemIds") or [])
        priced_ids = priced_ids_by_station.get(station_id, set())
        station["itemIds"] = sorted(item_ids)
        station["itemCount"] = len(item_ids)
        station["pricedItemCount"] = len(priced_ids) if priced_ids else int(station.get("pricedItemCount") or 0)
        results.append(station)
    return sorted(results, key=lambda station: str(station.get("name") or "").casefold())


def _merge_private_extractor_usage(previous: list[Any], current: list[Any]) -> list[dict[str, Any]]:
    """Keep the newest private extractor observation for each docked station."""
    merged: dict[str, dict[str, Any]] = {}
    for raw_record in [*previous, *current]:
        if not isinstance(raw_record, dict):
            continue
        station_id = _record_key(raw_record, "stationId")
        system_name = str(raw_record.get("systemName") or "").strip()
        resource_slots = raw_record.get("resourceSlots")
        if not station_id or not system_name or not isinstance(resource_slots, dict):
            continue
        cleaned_slots: dict[str, int] = {}
        for resource, raw_quantity in resource_slots.items():
            try:
                quantity = int(raw_quantity)
            except (TypeError, ValueError):
                continue
            if quantity > 0:
                cleaned_slots[str(resource)] = quantity
        if not cleaned_slots:
            continue
        incoming = copy.deepcopy(raw_record)
        incoming["stationId"] = station_id
        incoming["systemName"] = system_name
        incoming["resourceSlots"] = cleaned_slots
        existing = merged.get(station_id)
        if existing is None:
            merged[station_id] = incoming
            continue
        winner = _newer(existing, incoming, "observedAt")
        combined = copy.deepcopy(winner)
        for field in ("stationName", "planetId", "planetName"):
            if not combined.get(field):
                combined[field] = existing.get(field) or incoming.get(field)
        merged[station_id] = combined
    return sorted(
        merged.values(),
        key=lambda record: (
            str(record.get("systemName") or "").casefold(),
            str(record.get("stationId") or ""),
        ),
    )


def _merge_scans(previous: list[Any], current: list[Any]) -> list[dict[str, Any]]:
    scans: dict[str, dict[str, Any]] = {}
    for raw_scan in [*previous, *current]:
        if not isinstance(raw_scan, dict):
            continue
        scan_id = _record_key(raw_scan, "planet_id", "planet_name")
        if not scan_id:
            continue
        incoming = copy.deepcopy(raw_scan)
        existing = scans.get(scan_id)
        if existing is None:
            scans[scan_id] = incoming
            continue
        winner = copy.deepcopy(_newer(existing, incoming, "observedAt"))
        if not winner.get("system_name"):
            winner["system_name"] = existing.get("system_name") or incoming.get("system_name")
        scans[scan_id] = winner
    return sorted(scans.values(), key=lambda scan: str(scan.get("planet_name") or "").casefold())


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _merge_training(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    player: dict[str, Any],
    ship: dict[str, Any],
) -> dict[str, Any]:
    offers: dict[str, dict[str, Any]] = {}
    for training in (previous or {}, current or {}):
        for raw_offer in training.get("offers") or []:
            if not isinstance(raw_offer, dict):
                continue
            key = f"{raw_offer.get('stationId') or ''}|{raw_offer.get('skillId') or ''}"
            if key == "|":
                continue
            incoming = copy.deepcopy(raw_offer)
            existing = offers.get(key)
            if existing is None:
                offers[key] = incoming
                continue
            winner = copy.deepcopy(_newer(existing, incoming, "observedAt"))
            if not winner.get("systemName"):
                winner["systemName"] = existing.get("systemName") or incoming.get("systemName")
            if not winner.get("stationName"):
                winner["stationName"] = existing.get("stationName") or incoming.get("stationName")
            offers[key] = winner

    skills = player.get("skills") if isinstance(player.get("skills"), list) else []
    skill_by_id = {
        str(skill.get("skill_id")): skill
        for skill in skills
        if isinstance(skill, dict) and skill.get("skill_id")
    }
    total_points = _number((player.get("xp") or {}).get("skillPoints"))
    spent_points = sum(_number(skill.get("cost_paid")) for skill in skills if isinstance(skill, dict))
    available_points = max(0, int(total_points - spent_points))
    available_credits = _number(player.get("credits"))

    owned: dict[tuple[str, str], int] = {}
    inventory = ship.get("inventory") if isinstance(ship.get("inventory"), list) else []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        item_class = str(item.get("item_class") or "")
        if item_class == "cargo":
            category = "resource"
            item_type = str(item.get("resource") or item.get("item_type") or "")
            amount = int(_number(item.get("amount")))
        else:
            category = str(item.get("item_category") or item_class)
            item_type = str(item.get("item_type") or "")
            amount = int(_number(item.get("amount"), 1))
        if category and item_type:
            owned[(category, item_type)] = owned.get((category, item_type), 0) + amount

    results = []
    for offer in offers.values():
        skill = skill_by_id.get(str(offer.get("skillId") or ""), {})
        current_level = int(_number(skill.get("level"), _number(offer.get("currentLevel"))))
        offered_max = int(_number(offer.get("offeredMax")))
        next_sp_cost = skill.get("next_cost", offer.get("nextSpCost"))
        next_credit_cost = skill.get("next_credit_cost", offer.get("nextCreditCost"))
        item_class = str(offer.get("itemCostClass") or "")
        item_type = str(offer.get("itemCostType") or "")
        base_cost = int(_number(offer.get("itemCostAmount")))
        needed = base_cost * (current_level + 1) if base_cost and offer.get("itemCostScale") == "linear" else base_cost
        item_owned = owned.get((item_class, item_type), 0)
        at_cap = offered_max > 0 and current_level >= offered_max
        can_sp = next_sp_cost is not None and available_points >= _number(next_sp_cost)
        can_credits = next_credit_cost is None or available_credits >= _number(next_credit_cost)
        can_item = not needed or item_owned >= needed
        offer.update(
            {
                "displayName": str(skill.get("display_name") or offer.get("displayName") or str(offer.get("skillId") or "").replace("_", " ").title()),
                "description": str(skill.get("description") or offer.get("description") or ""),
                "currentLevel": current_level,
                "globalMax": skill.get("max_level", offer.get("globalMax")),
                "nextSpCost": next_sp_cost,
                "nextCreditCost": next_credit_cost,
                "statBonus": skill.get("stat_bonus") if isinstance(skill.get("stat_bonus"), dict) else offer.get("statBonus") or {},
                "pctBonus": skill.get("pct_bonus") if isinstance(skill.get("pct_bonus"), dict) else offer.get("pctBonus") or {},
                "availableSkillPoints": available_points,
                "itemCostNeeded": needed,
                "itemOwned": item_owned,
                "atStationCap": at_cap,
                "canAffordSp": can_sp,
                "canAffordCredits": can_credits,
                "canAffordItem": can_item,
                "canTrainNow": not at_cap and can_sp and can_credits and can_item,
            }
        )
        results.append(offer)
    results.sort(key=lambda row: (str(row.get("displayName") or "").casefold(), str(row.get("stationName") or "").casefold()))
    return {
        "offers": results,
        "offerCount": len(results),
        "skillCount": len({str(row.get("skillId")) for row in results}),
        "stationCount": len({str(row.get("stationId")) for row in results}),
    }


def _resolve_station_system(station: dict[str, Any], system_names: list[str]) -> str | None:
    explicit = str(station.get("systemName") or "").strip()
    canonical = {name.casefold(): name for name in system_names}
    if explicit:
        return canonical.get(explicit.casefold(), explicit)
    station_name = str(station.get("name") or "").casefold()
    for system_name in sorted(system_names, key=len, reverse=True):
        folded = system_name.casefold()
        start = station_name.find(folded)
        while start >= 0:
            end = start + len(folded)
            if (start == 0 or not station_name[start - 1].isalnum()) and (end == len(station_name) or not station_name[end].isalnum()):
                return system_name
            start = station_name.find(folded, start + 1)
    return None


def _merge_galaxy_maps(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    map_authoritative: bool = True,
) -> dict[str, Any]:
    old_map = previous if isinstance(previous, dict) else {}
    new_map = current if isinstance(current, dict) else {}
    if not new_map.get("hasData"):
        return copy.deepcopy(old_map if old_map.get("hasData") else new_map)

    result = copy.deepcopy(new_map)
    systems: dict[str, dict[str, Any]] = {}
    for source in (old_map.get("systems") or [], new_map.get("systems") or []):
        for raw_system in source:
            if not isinstance(raw_system, dict):
                continue
            incoming = copy.deepcopy(raw_system)
            # The explored snapshot can replace a system's provisional name-id
            # with its numeric game id.  System names are the stable galaxy-map
            # identity, so keying by id retained duplicate rows for one place.
            key = str(incoming.get("name") or incoming.get("id") or "").strip().casefold()
            if not key:
                continue
            existing = systems.get(key)
            if existing is None:
                systems[key] = incoming
                continue
            combined = copy.deepcopy(incoming)
            for field, value in existing.items():
                if field not in combined or combined[field] is None or combined[field] == "":
                    combined[field] = copy.deepcopy(value)
            if not map_authoritative:
                # Community intel may add exploration and hazard observations,
                # but it must not move locally authoritative static map nodes.
                for field in ("x", "y", "hasPosition", "ownership", "npcStationCount"):
                    if field in existing:
                        combined[field] = copy.deepcopy(existing[field])
            systems[key] = combined

    result["systems"] = sorted(
        systems.values(), key=lambda system: str(system.get("name") or "").casefold()
    )

    edge_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for source_map in (old_map, new_map):
        for raw_edge in source_map.get("edges") or []:
            if not isinstance(raw_edge, dict):
                continue
            source = str(raw_edge.get("source") or "").strip()
            target = str(raw_edge.get("target") or "").strip()
            if not source or not target or source.casefold() == target.casefold():
                continue
            key = tuple(sorted((source.casefold(), target.casefold())))
            edge_rows[key] = {"source": source, "target": target}
    result["edges"] = sorted(
        edge_rows.values(),
        key=lambda edge: (edge["source"].casefold(), edge["target"].casefold()),
    )

    # A non-empty current position snapshot proves GALAXY_STATIC was observed.
    # In that case its territory map is authoritative even when empty (all
    # claims may have been removed).  Older territory is retained only when a
    # partial import has no static positions at all.
    current_positions = new_map.get("territoryPositions")
    old_positions = old_map.get("territoryPositions")
    old_territory = old_map.get("territory")
    accept_current_authority = (
        isinstance(current_positions, dict)
        and bool(current_positions)
        and (map_authoritative or not (isinstance(old_positions, dict) and old_positions))
    )
    if accept_current_authority:
        result["territoryPositions"] = copy.deepcopy(current_positions)
        current_territory = new_map.get("territory")
        result["territory"] = copy.deepcopy(
            current_territory if isinstance(current_territory, dict) else {}
        )
    else:
        if isinstance(old_positions, dict) and old_positions:
            result["territoryPositions"] = copy.deepcopy(old_positions)
        if isinstance(old_territory, dict):
            result["territory"] = copy.deepcopy(old_territory)
    return result


def merge_catalog(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    map_authoritative: bool = True,
) -> dict[str, Any]:
    if not previous:
        merged = copy.deepcopy(current)
    else:
        merged = copy.deepcopy(current)
        merged["items"] = _merge_items(previous.get("items") or [], current.get("items") or [])
        merged["scans"] = _merge_scans(previous.get("scans") or [], current.get("scans") or [])
        merged["stations"] = _merge_stations(previous.get("stations") or [], current.get("stations") or [], merged["items"])
        merged["privateExtractorUsage"] = _merge_private_extractor_usage(
            previous.get("privateExtractorUsage") or [],
            current.get("privateExtractorUsage") or [],
        )
        for section in ("player", "ship"):
            if not (merged.get(section) or {}).get("hasData") and (previous.get(section) or {}).get("hasData"):
                merged[section] = copy.deepcopy(previous[section])
        merged["map"] = _merge_galaxy_maps(
            previous.get("map"),
            current.get("map"),
            map_authoritative=map_authoritative,
        )

    merged["training"] = _merge_training(
        previous.get("training") if previous else None,
        current.get("training"),
        merged.get("player") or {},
        merged.get("ship") or {},
    )
    merged["privateExtractorUsage"] = _merge_private_extractor_usage(
        (previous or {}).get("privateExtractorUsage") or [],
        merged.get("privateExtractorUsage") or [],
    )

    items = merged.get("items") or []
    scans = merged.get("scans") or []
    stations = _merge_stations([], merged.get("stations") or [], items)
    merged["stations"] = stations
    counts = Counter(str(item.get("category") or "unknown") for item in items)
    labels = {str(item.get("category") or "unknown"): str(item.get("categoryLabel") or "Unknown") for item in items}
    merged["categories"] = [
        {"id": category, "label": labels.get(category, category.replace("_", " ").title()), "count": count}
        for category, count in sorted(counts.items(), key=lambda pair: (-pair[1], labels.get(pair[0], pair[0]).casefold()))
    ]

    galaxy = copy.deepcopy(merged.get("map") or {})
    system_names = [str(system.get("name") or "") for system in galaxy.get("systems") or [] if system.get("name")]
    mapped_stations = []
    unmapped_stations = []
    station_systems: dict[str, str] = {}
    for station in stations:
        located = copy.deepcopy(station)
        system_name = _resolve_station_system(located, system_names)
        located["systemName"] = system_name
        if system_name:
            station_systems[str(station.get("id") or "")] = system_name
            mapped_stations.append(located)
        else:
            unmapped_stations.append(located)
    for item in items:
        for market in item.get("markets") or []:
            if not market.get("systemName"):
                market["systemName"] = station_systems.get(str(market.get("stationId") or ""))
    galaxy["stations"] = mapped_stations
    galaxy["unmappedStations"] = unmapped_stations
    galaxy["systemCount"] = len(galaxy.get("systems") or [])
    galaxy["edgeCount"] = len(galaxy.get("edges") or [])
    galaxy["territoryCount"] = len(galaxy.get("territory") or {})
    galaxy["mappedStationCount"] = len(mapped_stations)
    galaxy["unmappedStationCount"] = len(unmapped_stations)
    merged["map"] = galaxy

    meta = dict(merged.get("meta") or {})
    meta.update(
        {
            "itemCount": len(items),
            "categoryCount": len(merged["categories"]),
            "stationCount": len(stations),
            "scanCount": len(scans),
            "extractorStationCount": len(merged["privateExtractorUsage"]),
            "trainingOfferCount": merged["training"]["offerCount"],
            "trainingSkillCount": merged["training"]["skillCount"],
            "mapSystemCount": galaxy.get("systemCount", 0),
            "mapEdgeCount": galaxy.get("edgeCount", 0),
            "mapTerritorySystemCount": galaxy.get("territoryCount", 0),
            "mappedStationCount": galaxy.get("mappedStationCount", 0),
            "withStats": sum(bool(item.get("stats")) for item in items),
            "withPrice": sum(bool((item.get("flags") or {}).get("hasPrice")) for item in items),
            "withArt": sum(bool(item.get("art")) for item in items),
            "illustrated": len(items),
            "archiveVersion": ARCHIVE_VERSION,
        }
    )
    merged["meta"] = meta
    return merged


class ArchiveStore:
    """Durable local catalog that survives game-log rotation."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_archive_path()
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != ARCHIVE_VERSION:
            return None
        catalog = payload.get("catalog")
        return catalog if isinstance(catalog, dict) else None

    def merge(
        self,
        current: dict[str, Any],
        *,
        map_authoritative: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            merged = merge_catalog(
                self.load(), current, map_authoritative=map_authoritative
            )
            merged["meta"]["archivePath"] = str(self.path)
            self._write_atomic({"version": ARCHIVE_VERSION, "catalog": merged})
            return merged

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
