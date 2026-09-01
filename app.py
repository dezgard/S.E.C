from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from archive_store import ArchiveStore
from map_territory import normalize_positions_snapshot, normalize_territory_snapshot

PROJECT_ROOT = Path(__file__).resolve().parent


def _default_game_root() -> Path:
    starts = [PROJECT_ROOT]
    if getattr(sys, "frozen", False):
        starts.insert(0, Path(sys.executable).resolve().parent)
    for start in starts:
        for parent in (start, *start.parents):
            candidate = parent / "Test_folder_for_current_build_of_StarEmpire" / "game"
            if (candidate / "Client.exe").is_file():
                return candidate
    legacy = Path(r"C:\Users\Dezgard\Desktop\Test")
    if (legacy / "Client.exe").is_file():
        return legacy
    return legacy


GAME_ROOT = Path(os.environ.get("STAR_EMPIRE_GAME_DIR", str(_default_game_root())))
LOG_PATH = Path(os.environ.get("STAR_EMPIRE_LOG", str(GAME_ROOT / "star_empire_client.log")))
ASSET_ROOT = GAME_ROOT / "_internal"


def normalise_game_root(value: str | os.PathLike[str]) -> Path:
    """Return the actual game folder, accepting either it or its parent."""

    text = os.path.expandvars(os.fspath(value)).strip()
    if not text:
        raise ValueError("Choose the Star Empire game folder first.")
    root = Path(text).expanduser().resolve(strict=False)
    nested = root / "game"
    if not (root / "Client.exe").is_file() and (nested / "Client.exe").is_file():
        root = nested.resolve(strict=False)
    return root


def validate_game_root(value: str | os.PathLike[str]) -> Path:
    root = normalise_game_root(value)
    if not root.is_dir():
        raise ValueError(f"The selected folder does not exist:\n{root}")
    if not (root / "Client.exe").is_file():
        raise ValueError(f"Client.exe was not found in:\n{root}")
    if not (root / "_internal").is_dir():
        raise ValueError(f"The game's _internal folder was not found in:\n{root}")
    return root


def configure_game_root(
    value: str | os.PathLike[str],
    *,
    require_valid: bool = True,
    log_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Switch all runtime readers to one game folder without modifying it."""

    root = validate_game_root(value) if require_valid else normalise_game_root(value)
    selected_log = Path(log_path).expanduser().resolve(strict=False) if log_path else root / "star_empire_client.log"

    global GAME_ROOT, LOG_PATH, ASSET_ROOT, STORE
    GAME_ROOT = root
    LOG_PATH = selected_log
    ASSET_ROOT = root / "_internal"

    data_store_type = globals().get("DataStore")
    existing_store = globals().get("STORE")
    if data_store_type is not None:
        archive_store = getattr(existing_store, "_archive_store", None)
        STORE = data_store_type(archive_store)
    return root

SHOP_MARKER = "SHOP_CATALOG "
TRAINING_MARKER = "TRAINING_CATALOG "
SCAN_MARKER = "PLANET_SCAN_RESULT "
SYSTEM_BODY_ROSTER_MARKER = "SYSTEM_BODY_ROSTER "
STATION_EXTRACTOR_MARKER = "STATION_EXTRACTOR_SNAPSHOT "
COLONY_ECONOMY_MARKER = "COLONY_ECONOMY_SNAPSHOT "
PRODUCTION_PER_DAY_RE = re.compile(r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*/\s*day\b", re.IGNORECASE)
RATION_PROCESSOR_TYPE = "ration_processor"
RATION_PROCESSOR_CYCLE_PART_RE = re.compile(
    r"^\s*(?P<amount>[\d,]+(?:\.\d+)?)\s+(?P<resource>[A-Za-z][A-Za-z _-]*)\s*$",
)
RATION_PROCESSOR_CYCLE_SECONDS_RE = re.compile(
    r"(?P<seconds>[\d,]+(?:\.\d+)?)\s*seconds?",
    re.IGNORECASE,
)
SECONDS_PER_DAY = 86_400
SNAPSHOT_MARKERS = {
    "GALAXY_STATIC_SNAPSHOT ": "galaxyStatic",
    "GALAXY_MAP_SNAPSHOT ": "galaxyMap",
    "EXPLORED_SYSTEMS_SNAPSHOT ": "exploredSystems",
    "MY_STATIONS_SNAPSHOT ": "myStations",
    "PLAYER_CREDITS_SNAPSHOT ": "credits",
    "PLAYER_XP_SNAPSHOT ": "xp",
    "PLAYER_SKILLS_SNAPSHOT ": "skills",
    # The longer hangar marker must precede SHIP_INVENTORY_SNAPSHOT because it
    # contains that shorter marker as a suffix.
    "HANGAR_SHIP_INVENTORY_SNAPSHOT ": "hangarInventory",
    "SHIP_INVENTORY_SNAPSHOT ": "inventory",
    "SHIP_PLUGIN_SNAPSHOT ": "plugins",
    "SHIP_SPECS_SNAPSHOT ": "specs",
}
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
WARP_SYSTEM_RE = re.compile(
    r"\bWARP-TIMING\b.*?\bsid\s+\d+\s*(?:->|→)\s*(\d+)\b",
    re.IGNORECASE,
)
EXTRACTOR_RESOURCE_BY_MODULE = {
    "metal_drill": "metal_ore",
    "advanced_metal_drill": "metal_ore",
    "industrial_metal_drill": "metal_ore",
    "silicon_drill": "silicon",
    "advanced_silicon_drill": "silicon",
    "industrial_silicon_drill": "silicon",
    "copper_extractor": "copper",
    "titanium_extractor": "titanium",
    "gold_extractor": "gold",
    "oil_drill": "oil",
    "wood_cutter": "wood",
    "advanced_wood_cutter": "wood",
    "industrial_wood_cutter": "wood",
    "harvester": "space_corn",
    "advanced_harvester": "space_corn",
    "industrial_harvester": "space_corn",
}
# These are the three production tiers exposed by the station module family,
# rather than item-catalogue tech values.  The logger already keeps only these
# known extractor module IDs, so this remains private, passive local data.
EXTRACTOR_TIER_BY_MODULE = {
    "metal_drill": 3,
    "advanced_metal_drill": 6,
    "industrial_metal_drill": 9,
    "silicon_drill": 3,
    "advanced_silicon_drill": 6,
    "industrial_silicon_drill": 9,
    "copper_extractor": 3,
    "titanium_extractor": 3,
    "gold_extractor": 3,
    "oil_drill": 3,
    "wood_cutter": 3,
    "advanced_wood_cutter": 6,
    "industrial_wood_cutter": 9,
    "harvester": 3,
    "advanced_harvester": 6,
    "industrial_harvester": 9,
}
PROCESSOR_MODULE_TYPES = frozenset({
    "furniture_factory",
    "metal_foundry",
    "microchip_fabricator",
    "ration_processor",
})
PRODUCTION_MODULE_TYPES = frozenset({*EXTRACTOR_RESOURCE_BY_MODULE, *PROCESSOR_MODULE_TYPES})
SAFE_ASSET_FOLDERS = {
    "Ships",
    "Drones",
    "Stations",
    "OrbitalBodies",
    "Weapons",
    "Missiles",
    "Images",
}
IMAGE_FOLDER_BY_CATEGORY = {
    "ship": "Ships",
    "drone": "Drones",
    "station": "Stations",
    "weapon": "Weapons",
    "missile": "Missiles",
}
CATEGORY_LABELS = {
    "weapon": "Weapons",
    "shield": "Shields",
    "ship_plugin": "Ship plugins",
    "energy": "Energies",
    "station": "Stations",
    "engine": "Engines",
    "station_plugin": "Station plugins",
    "turret_upgrade": "Turret upgrades",
    "resource": "Resources",
    "ship": "Ships",
    "scoop": "Scoops",
    "hangar": "Hangars",
    "drone": "Drones",
    "controller": "Controllers",
    "launch_tube": "Launch tubes",
    "slipstream": "Slipstreams",
    "tractor": "Tractors",
    "cloak": "Cloaks",
    "scanner": "Scanners",
    "sensor": "Sensors",
    "blueprint": "Blueprints",
    "expander": "Hull expanders",
    "missile": "Missiles",
    "shield_charger": "Shield equipment",
    "trade_bot": "Trade bots",
    "fabricator": "Fabricators",
}
CATEGORY_ALIASES = {
    "weapons": "weapon",
    "shields": "shield",
    "energies": "energy",
    "engines": "engine",
    "resources": "resource",
    "ships": "ship",
    "drones": "drone",
    "stations": "station",
    "ship_plugins": "ship_plugin",
    "station_plugins": "station_plugin",
    "turret_upgrades": "turret_upgrade",
    "tractors": "tractor",
    "cloaks": "cloak",
    "sensors": "sensor",
}


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _canonical_category(record: dict[str, Any]) -> str:
    item = record.get("item") or {}
    raw = str(item.get("item_category") or item.get("buy_cat") or record.get("category") or "unknown")
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return CATEGORY_ALIASES.get(key, key)


def _as_number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _normalise_extractor_snapshot(record: Any) -> dict[str, Any] | None:
    """Return one minimal, private docked-station extractor observation."""
    if not isinstance(record, dict):
        return None
    station_id = str(record.get("station_id") or record.get("stationId") or "").strip()
    system_name = str(record.get("system_name") or record.get("systemName") or "").strip()
    raw_counts = record.get("equipped_module_counts", record.get("moduleCounts"))
    if not station_id or not system_name or not isinstance(raw_counts, dict):
        return None
    module_counts: dict[str, int] = {}
    resource_slots: dict[str, int] = {}
    for raw_type, raw_quantity in raw_counts.items():
        module_type = str(raw_type or "").strip()
        if module_type not in PRODUCTION_MODULE_TYPES:
            continue
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        module_counts[module_type] = quantity
        resource = EXTRACTOR_RESOURCE_BY_MODULE.get(module_type)
        if resource:
            resource_slots[resource] = resource_slots.get(resource, 0) + quantity
    if not module_counts:
        return None
    return {
        "stationId": station_id,
        "stationName": str(record.get("station_name") or record.get("stationName") or "").strip() or None,
        "systemName": system_name,
        "planetId": str(record.get("planet_id") or record.get("planetId") or "").strip() or None,
        "planetName": str(record.get("planet_name") or record.get("planetName") or "").strip() or None,
        "moduleCounts": module_counts,
        "resourceSlots": resource_slots,
        "observedAt": str(record.get("observedAt") or "") or None,
    }


def _normalise_colony_economy_snapshot(record: Any) -> dict[str, Any] | None:
    """Keep only the passive Colony-tab values used for local estimates."""
    if not isinstance(record, dict):
        return None
    station_id = str(record.get("station_id") or record.get("stationId") or "").strip()
    system_name = str(record.get("system_name") or record.get("systemName") or "").strip()
    tick_seconds = _as_number(record.get("tick_interval_seconds", record.get("tickIntervalSeconds")))
    raw_basket = record.get("basket")
    if not station_id or not system_name or not isinstance(tick_seconds, (int, float)) or not math.isfinite(tick_seconds) or tick_seconds <= 0 or not isinstance(raw_basket, list):
        return None
    basket: list[dict[str, float | str]] = []
    seen_resources: set[str] = set()
    for raw_entry in raw_basket:
        if not isinstance(raw_entry, dict):
            continue
        resource = str(raw_entry.get("resource") or "").strip()
        per_capita = _as_number(raw_entry.get("per_capita", raw_entry.get("perCapita")))
        if not resource or resource in seen_resources or not isinstance(per_capita, (int, float)) or not math.isfinite(per_capita) or per_capita <= 0:
            continue
        basket.append({"resource": resource, "perCapita": float(per_capita)})
        seen_resources.add(resource)
    if not basket:
        return None
    population = _as_number(record.get("population"))
    return {
        "stationId": station_id,
        "stationName": str(record.get("station_name") or record.get("stationName") or "").strip() or None,
        "systemName": system_name,
        "planetId": str(record.get("planet_id") or record.get("planetId") or "").strip() or None,
        "planetName": str(record.get("planet_name") or record.get("planetName") or "").strip() or None,
        "tickIntervalSeconds": float(tick_seconds),
        "population": float(population) if isinstance(population, (int, float)) and math.isfinite(population) and population >= 0 else None,
        "basket": basket,
        "observedAt": str(record.get("observedAt") or "") or None,
    }


def extractor_module_production_per_day(catalog_items: list[dict[str, Any]] | None) -> dict[str, float]:
    """Read each observed extractor's own catalogued ``Production`` value."""
    production: dict[str, float] = {}
    for item in catalog_items or []:
        if not isinstance(item, dict):
            continue
        module = str(item.get("type") or "").strip()
        if module not in EXTRACTOR_RESOURCE_BY_MODULE or module in production:
            continue
        stats = item.get("stats")
        if not isinstance(stats, dict):
            continue
        raw_production = next((value for key, value in stats.items() if str(key or "").strip().casefold() == "production"), None)
        match = PRODUCTION_PER_DAY_RE.search(str(raw_production or ""))
        if not match:
            continue
        try:
            amount = float(match.group("amount").replace(",", ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(amount) and amount > 0:
            production[module] = amount
    return production


def _cycle_resource_key(value: Any) -> str:
    """Normalise the small set of resource labels used in processor stats."""
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return {"ration": "rations"}.get(key, key)


def _cycle_resource_amounts(value: Any) -> dict[str, float]:
    """Parse a logged processor input/output line into normalised resources."""
    amounts: dict[str, float] = {}
    for part in str(value or "").split("+"):
        match = RATION_PROCESSOR_CYCLE_PART_RE.match(part)
        if not match:
            continue
        try:
            amount = float(match.group("amount").replace(",", ""))
        except (TypeError, ValueError):
            continue
        resource = _cycle_resource_key(match.group("resource"))
        if resource and math.isfinite(amount) and amount > 0:
            amounts[resource] = amounts.get(resource, 0.0) + amount
    return amounts


def processor_module_cycle_profiles(catalog_items: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Read the logged full-load recipes for supported production modules."""
    profiles: dict[str, dict[str, Any]] = {}
    for item in catalog_items or []:
        if not isinstance(item, dict):
            continue
        module = str(item.get("type") or "").strip()
        if module not in PROCESSOR_MODULE_TYPES or module in profiles:
            continue
        stats = item.get("stats")
        if not isinstance(stats, dict):
            continue
        values = {str(key or "").strip().casefold(): str(value or "") for key, value in stats.items()}
        inputs = _cycle_resource_amounts(values.get("cycle input"))
        outputs = _cycle_resource_amounts(values.get("cycle output"))
        cycle_match = RATION_PROCESSOR_CYCLE_SECONDS_RE.search(values.get("cycle time", ""))
        if not inputs or not outputs or not cycle_match:
            continue
        try:
            cycle_seconds = float(cycle_match.group("seconds").replace(",", ""))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cycle_seconds) or cycle_seconds <= 0:
            continue
        profiles[module] = {"inputs": inputs, "outputs": outputs, "cycleSeconds": cycle_seconds}
    return profiles


def ration_processor_profile(catalog_items: list[dict[str, Any]] | None) -> dict[str, float] | None:
    """Read the logged Ration Processor recipe without assuming its rates."""
    for item in catalog_items or []:
        if not isinstance(item, dict) or str(item.get("type") or "").strip() != RATION_PROCESSOR_TYPE:
            continue
        stats = item.get("stats")
        if not isinstance(stats, dict):
            continue
        values: dict[str, str] = {
            str(key or "").strip().casefold(): str(value or "")
            for key, value in stats.items()
        }
        inputs: dict[str, float] = {}
        for part in values.get("cycle input", "").split("+"):
            match = RATION_PROCESSOR_CYCLE_PART_RE.match(part)
            if not match:
                continue
            try:
                amount = float(match.group("amount").replace(",", ""))
            except (TypeError, ValueError):
                continue
            resource = _cycle_resource_key(match.group("resource"))
            if resource and math.isfinite(amount) and amount > 0:
                inputs[resource] = inputs.get(resource, 0.0) + amount
        output_match = RATION_PROCESSOR_CYCLE_PART_RE.match(values.get("cycle output", ""))
        cycle_match = RATION_PROCESSOR_CYCLE_SECONDS_RE.search(values.get("cycle time", ""))
        if not output_match or not cycle_match:
            continue
        try:
            rations = float(output_match.group("amount").replace(",", ""))
            seconds = float(cycle_match.group("seconds").replace(",", ""))
        except (TypeError, ValueError):
            continue
        if (
            _cycle_resource_key(output_match.group("resource")) != "rations"
            or not all(math.isfinite(value) and value > 0 for value in (rations, seconds))
            or not isinstance(inputs.get("space_corn"), float)
            or inputs["space_corn"] <= 0
        ):
            continue
        credits = inputs.get("credits", 0.0)
        if not isinstance(credits, float) or not math.isfinite(credits) or credits < 0:
            continue
        return {
            "spaceCornPerCycle": inputs["space_corn"],
            "creditsPerCycle": credits,
            "rationsPerCycle": rations,
            "cycleSeconds": seconds,
        }
    return None


def ration_projection(
    space_corn_per_tick: Any,
    tick_interval_seconds: Any,
    catalog_items: list[dict[str, Any]] | None,
    rations_per_capita: Any = None,
) -> dict[str, Any]:
    """Project corn-limited Ration output from the observed processor recipe."""
    corn = _as_number(space_corn_per_tick)
    tick_seconds = _as_number(tick_interval_seconds)
    profile = ration_processor_profile(catalog_items)
    if (
        not profile
        or not isinstance(corn, (int, float))
        or not isinstance(tick_seconds, (int, float))
        or not math.isfinite(corn)
        or not math.isfinite(tick_seconds)
        or corn < 0
        or tick_seconds <= 0
    ):
        return {"profile": profile, "rationsPerTick": None, "processorsRequired": None, "creditsPerTick": None, "sustainablePopulation": None}
    rations = float(corn) * profile["rationsPerCycle"] / profile["spaceCornPerCycle"]
    per_processor = float(tick_seconds) * profile["rationsPerCycle"] / profile["cycleSeconds"]
    credits = rations * profile["creditsPerCycle"] / profile["rationsPerCycle"]
    per_capita = _as_number(rations_per_capita)
    sustainable = (
        math.floor(rations / float(per_capita))
        if isinstance(per_capita, (int, float)) and math.isfinite(per_capita) and per_capita > 0
        else None
    )
    return {
        "profile": profile,
        "rationsPerTick": rations,
        "processorsRequired": math.ceil(rations / per_processor) if per_processor > 0 else None,
        "creditsPerTick": credits,
        "sustainablePopulation": sustainable,
    }


def extractor_record_output_per_tick(
    record: dict[str, Any] | None,
    catalog_items: list[dict[str, Any]] | None,
    tick_interval_seconds: Any,
) -> dict[str, float]:
    """Calculate observed raw extractor volume for the server's actual tick."""
    tick_seconds = _as_number(tick_interval_seconds)
    module_counts = record.get("moduleCounts") if isinstance(record, dict) else None
    if not isinstance(tick_seconds, (int, float)) or not math.isfinite(tick_seconds) or tick_seconds <= 0 or not isinstance(module_counts, dict):
        return {}
    rates_per_day = extractor_module_production_per_day(catalog_items)
    output: dict[str, float] = {}
    for raw_module, raw_quantity in module_counts.items():
        module = str(raw_module or "").strip()
        resource = EXTRACTOR_RESOURCE_BY_MODULE.get(module)
        rate_per_day = rates_per_day.get(module)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if not resource or quantity <= 0 or rate_per_day is None:
            continue
        output[resource] = output.get(resource, 0.0) + quantity * rate_per_day * float(tick_seconds) / SECONDS_PER_DAY
    return {resource: value for resource, value in output.items() if math.isfinite(value) and value > 0}


def equipped_module_production_per_tick(
    record: dict[str, Any] | None,
    catalog_items: list[dict[str, Any]] | None,
    tick_interval_seconds: Any,
) -> dict[str, dict[str, float]]:
    """Calculate full-load output, input demand, and credit cost from equipped modules.

    This uses only the station's passively observed equipped module counts and
    the public catalogue rates. Processor requirements remain separate because
    cargo and reserves are not captured, so actual throughput can be lower when
    required inputs are unavailable.
    """
    tick_seconds = _as_number(tick_interval_seconds)
    module_counts = record.get("moduleCounts") if isinstance(record, dict) else None
    result: dict[str, dict[str, float]] = {"outputs": {}, "inputs": {}, "credits": {}}
    if not isinstance(tick_seconds, (int, float)) or not math.isfinite(tick_seconds) or tick_seconds <= 0 or not isinstance(module_counts, dict):
        return result
    result["outputs"].update(extractor_record_output_per_tick(record, catalog_items, tick_seconds))
    profiles = processor_module_cycle_profiles(catalog_items)
    for raw_module, raw_quantity in module_counts.items():
        module = str(raw_module or "").strip()
        profile = profiles.get(module)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if quantity <= 0 or profile is None:
            continue
        cycles = quantity * float(tick_seconds) / float(profile["cycleSeconds"])
        for resource, amount in profile["outputs"].items():
            result["outputs"][resource] = result["outputs"].get(resource, 0.0) + cycles * float(amount)
        for resource, amount in profile["inputs"].items():
            target = "credits" if resource == "credits" else "inputs"
            result[target][resource] = result[target].get(resource, 0.0) + cycles * float(amount)
    return {
        section: {resource: value for resource, value in values.items() if math.isfinite(value) and value > 0}
        for section, values in result.items()
    }


def colony_baseline_support_estimate(
    output_per_tick: dict[str, Any] | None,
    basket: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Estimate direct-extractor colony support from the server basket only."""
    output = output_per_tick if isinstance(output_per_tick, dict) else {}
    supported_by_resource: dict[str, float] = {}
    missing_resources: list[str] = []
    for entry in basket or []:
        if not isinstance(entry, dict):
            continue
        resource = str(entry.get("resource") or "").strip()
        per_capita = _as_number(entry.get("perCapita", entry.get("per_capita")))
        amount = _as_number(output.get(resource))
        if not resource or not isinstance(per_capita, (int, float)) or not math.isfinite(per_capita) or per_capita <= 0:
            continue
        if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            missing_resources.append(resource)
            continue
        supported_by_resource[resource] = float(amount) / float(per_capita)
    missing_resources = sorted(set(missing_resources), key=str.casefold)
    if missing_resources or not supported_by_resource:
        return {"supportedPopulation": None, "limitingResources": [], "missingResources": missing_resources, "supportByResource": supported_by_resource}
    limit = min(supported_by_resource.values())
    limiting = sorted((resource for resource, value in supported_by_resource.items() if math.isclose(value, limit, rel_tol=1e-9, abs_tol=1e-9)), key=str.casefold)
    return {"supportedPopulation": math.floor(limit), "limitingResources": limiting, "missingResources": [], "supportByResource": supported_by_resource}

def system_extractor_slots(records: list[dict[str, Any]] | None, system_name: str) -> dict[str, int]:
    """Sum privately observed equipped extractors for one named system."""
    target = str(system_name or "").strip().casefold()
    totals: dict[str, int] = {}
    if not target:
        return totals
    for record in records or []:
        if not isinstance(record, dict) or str(record.get("systemName") or "").strip().casefold() != target:
            continue
        slots = record.get("resourceSlots")
        if not isinstance(slots, dict):
            continue
        for resource, raw_quantity in slots.items():
            try:
                quantity = int(raw_quantity)
            except (TypeError, ValueError):
                continue
            if quantity > 0:
                totals[str(resource)] = totals.get(str(resource), 0) + quantity
    return totals


def system_extractor_tier_counts(
    records: list[dict[str, Any]] | None,
    system_name: str,
) -> dict[str, dict[int, int]]:
    """Sum observed extractor modules by resource and production tier.

    ``moduleCounts`` is already retained locally alongside each docked-base
    snapshot.  Older snapshots with only aggregate resource slots simply have
    no tier breakdown, rather than inventing one from the total.
    """
    target = str(system_name or "").strip().casefold()
    totals: dict[str, dict[int, int]] = {}
    if not target:
        return totals
    for record in records or []:
        if not isinstance(record, dict) or str(record.get("systemName") or "").strip().casefold() != target:
            continue
        module_counts = record.get("moduleCounts")
        if not isinstance(module_counts, dict):
            continue
        for raw_module, raw_quantity in module_counts.items():
            module = str(raw_module or "").strip()
            resource = EXTRACTOR_RESOURCE_BY_MODULE.get(module)
            tier = EXTRACTOR_TIER_BY_MODULE.get(module)
            if not resource or tier is None:
                continue
            try:
                quantity = int(raw_quantity)
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            resource_totals = totals.setdefault(resource, {})
            resource_totals[tier] = resource_totals.get(tier, 0) + quantity
    return {
        resource: dict(sorted(tiers.items()))
        for resource, tiers in sorted(totals.items())
    }


def _safe_colour(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) < 3:
        return [111, 191, 255]
    colour = []
    for channel in value[:3]:
        try:
            colour.append(max(0, min(255, int(channel))))
        except (TypeError, ValueError):
            colour.append(180)
    return colour


def _record_score(item: dict[str, Any]) -> int:
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    populated = sum(1 for value in item.values() if value not in (None, "", [], {}))
    return len(stats) * 20 + populated * 2 + (12 if item.get("description") else 0)


def _source_label(source: str) -> str:
    return {
        "NPC_STATION_DOCK_OK": "NPC station",
        "PS_TRADE_LISTING": "Player station listing",
        "PS_TRADE_MANAGER": "Trade manager",
    }.get(source, source.replace("_", " ").title())


PLAYER_MARKET_SOURCES = frozenset({"PS_TRADE_LISTING"})


def shopping_rows(items: list[dict[str, Any]],
                  stations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per (item, station) price observation, for the Shopping tab.

    Everything here is a snapshot: PS_TRADE_LISTING and NPC_TRADE_REFRESH are
    both pulls that only fire when the player docks and opens that window, so
    nothing refreshes in the background.  ``observedAt`` therefore rides on
    every row and the UI is expected to show it.

    ``owner`` is decided by the capture source and by the station's ``isMine``
    flag, never by the station's name — an unnamed payload used to be labelled
    "Your station · <id>", which put four NPC stations in the player's own
    column.
    """
    owned = {
        str(station.get("id"))
        for station in (stations or [])
        if station.get("isMine")
    }
    rows: list[dict[str, Any]] = []
    for item in items or []:
        for market in item.get("markets") or []:
            source = str(market.get("source") or "")
            station_id = str(market.get("stationId") or "")
            is_player = source in PLAYER_MARKET_SOURCES
            rows.append({
                "itemId": item.get("id"),
                "item": item.get("name"),
                "category": item.get("categoryLabel") or item.get("category"),
                "rarity": item.get("rarity"),
                "tech": item.get("tech"),
                "cargoSize": item.get("cargoSize"),
                "stationId": station_id,
                "station": market.get("stationName"),
                "system": market.get("systemName"),
                # NPC rows carry buyPrice only; a sellPrice of None must stay
                # None so the UI leaves the cell blank instead of implying a
                # spread that was never observed.
                "buyPrice": market.get("buyPrice"),
                "sellPrice": market.get("sellPrice"),
                "stock": market.get("stock"),
                "noSell": bool(market.get("noSell")),
                "observedAt": market.get("observedAt"),
                "source": source,
                "sourceLabel": market.get("sourceLabel"),
                "owner": "player" if is_player else "npc",
                "isMine": is_player and station_id in owned,
            })
    rows.sort(key=lambda row: (
        str(row["item"] or "").casefold(), str(row["station"] or "").casefold()))
    return rows


def _is_placeholder_station_name(name: str, station_id: str) -> bool:
    """True when a station name was derived from its id rather than observed.

    Also matches the retired "Your station · <id>" fallback, so archives
    written before that was fixed are corrected on the next refresh rather
    than keeping a name that claims the wrong owner.
    """
    name = str(name or "").strip()
    if not name:
        return True
    if name.startswith("Your station ·"):
        return True
    station_id = str(station_id or "")
    return name in {
        f"Station {station_id}",
        station_id.replace("_", " ").title(),
        "Unknown station",
    }


DEFAULT_HAZARD_LIMIT = 100


def system_hops(edges, origin: str, limit: int = 60) -> dict[str, int]:
    """Hops from *origin* to every reachable system, by breadth-first search.

    The warp graph only became usable once a shared team map arrived — before
    that it held 86 edges against 2,000 systems, so distance was unknowable and
    everything had to be ranked on guesswork.
    """
    neighbours: dict[str, set[str]] = {}
    for edge in edges or ():
        if not isinstance(edge, dict):
            continue
        a = str(edge.get("source") or "").strip()
        b = str(edge.get("target") or "").strip()
        if not a or not b:
            continue
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
    origin = str(origin or "").strip()
    if not origin:
        return {}
    hops = {origin: 0}
    frontier = [origin]
    while frontier and len(hops) < 20000:
        nxt = []
        for name in frontier:
            depth = hops[name]
            if depth >= limit:
                continue
            for other in neighbours.get(name, ()):  # noqa: SIM118
                if other not in hops:
                    hops[other] = depth + 1
                    nxt.append(other)
        frontier = nxt
    return hops


def coverage_targets(map_data: dict[str, Any],
                     stations: list[dict[str, Any]] | None = None,
                     origin: str = "",
                     hazard_limit: int = DEFAULT_HAZARD_LIMIT
                     ) -> list[dict[str, Any]]:
    """Systems known to hold something that has never been looked at.

    A shared map says where stations are; it never says what their shops hold.
    Shop contents and planet scans only arrive through ordinary player actions.
    This is the list of places worth the trip for missing shop observations.
    """
    systems = [row for row in (map_data or {}).get("systems") or []
               if isinstance(row, dict)]
    shops_known = {
        str(row.get("id"))
        for row in (stations or (map_data or {}).get("stations") or [])
        if isinstance(row, dict) and row.get("pricedItemCount")
    }
    hops = system_hops((map_data or {}).get("edges"), origin) if origin else {}
    rows: list[dict[str, Any]] = []
    for row in systems:
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        npc_stations = int(row.get("npcStationCount") or 0)
        # A station whose shop we already hold is not a reason to go back.
        unseen_shops = sum(
            1 for sid in (row.get("shopStationIds") or ())
            if str(sid) not in shops_known)
        if not row.get("shopStationIds"):
            unseen_shops = npc_stations if npc_stations else 0
        if not unseen_shops:
            continue
        hazard_known = bool(row.get("hazardKnown"))
        hazard = int(row.get("hazard") or 0)
        rows.append({
            "system": name,
            "hops": hops.get(name),
            "hazard": hazard if hazard_known else None,
            "hazardKnown": hazard_known,
            "npcStations": npc_stations,
            "unseenShops": unseen_shops,
            "explored": bool(row.get("explored")),
            # Unknown or high danger should stay visible as a caution, not be
            # hidden from the player deciding where to travel.
            "reachable": hazard_known and hazard <= int(hazard_limit),
        })
    rows.sort(key=lambda r: (
        not r["reachable"],
        r["hops"] if r["hops"] is not None else 9999,
        -r["unseenShops"],
        r["system"].casefold(),
    ))
    return rows


def _station_identity(record: dict[str, Any]) -> tuple[str, str]:
    """The station id and a display name for a captured payload.

    The fallback used to be "Your station · <id>", which mislabelled every
    NPC_TRADE_REFRESH row as the player's own — that payload carries no
    station_name, and 588 rows across four NPC stations ended up claiming to be
    theirs.  Ownership is decided by ``isMine`` and by the capture source, never
    by a guessed name, so an unnamed station now gets a neutral one derived from
    its id.
    """
    station_id = str(record.get("station_id") or "unknown")
    station_name = str(record.get("station_name") or "").strip()
    if station_name:
        return station_id, station_name
    if station_id == "unknown":
        return station_id, "Unknown station"
    if station_id.isdigit():
        # Player stations are numeric ids and carry no name of their own.
        return station_id, f"Station {station_id}"
    return station_id, station_id.replace("_", " ").title()


def _map_dataset(
    snapshots: dict[str, dict[str, Any]],
    stations: list[dict[str, Any]],
) -> dict[str, Any]:
    static = snapshots.get("galaxyStatic", {}).get("data")
    if not isinstance(static, dict):
        static = {}
    overlay = snapshots.get("galaxyMap", {}).get("data")
    if not isinstance(overlay, dict):
        overlay = {}
    explored = snapshots.get("exploredSystems", {}).get("data")
    if not isinstance(explored, list):
        explored = []

    positions = normalize_positions_snapshot(static.get("positions"))
    territory = normalize_territory_snapshot(static.get("territory"))
    ownership = static.get("ownership") if isinstance(static.get("ownership"), dict) else {}
    npc_stations = static.get("npc_stations") if isinstance(static.get("npc_stations"), dict) else {}
    mine = overlay.get("mine") if isinstance(overlay.get("mine"), dict) else {}
    coalition = overlay.get("coalition") if isinstance(overlay.get("coalition"), dict) else {}
    others = overlay.get("others") if isinstance(overlay.get("others"), dict) else {}

    systems_by_fold: dict[str, dict[str, Any]] = {}
    def ensure(name: Any) -> dict[str, Any] | None:
        display_name = str(name or "").strip()
        if not display_name:
            return None
        key = display_name.casefold()
        if key not in systems_by_fold:
            systems_by_fold[key] = {"name": display_name}
        return systems_by_fold[key]

    for mapping in (positions, territory, ownership, npc_stations, mine, coalition, others):
        for name in mapping:
            ensure(name)
    explored_rows = [entry for entry in explored if isinstance(entry, dict)]
    for entry in explored_rows:
        ensure(entry.get("name"))
        for gate in entry.get("warp_gates") or []:
            if isinstance(gate, dict):
                ensure(gate.get("target_system"))
    for station in stations:
        ensure(station.get("systemName"))
    def folded(mapping: dict[Any, Any]) -> dict[str, Any]:
        return {str(key).casefold(): value for key, value in mapping.items()}

    position_by_fold = folded(positions)
    ownership_by_fold = folded(ownership)
    npc_by_fold = folded(npc_stations)
    mine_by_fold = folded(mine)
    coalition_by_fold = folded(coalition)
    others_by_fold = folded(others)
    explored_by_fold = {
        str(entry.get("name") or "").strip().casefold(): entry
        for entry in explored_rows
        if str(entry.get("name") or "").strip()
    }
    stations_by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for station in stations:
        system_name = str(station.get("systemName") or "").strip()
        if system_name:
            stations_by_system[system_name.casefold()].append(station)

    system_results: list[dict[str, Any]] = []
    for fold_name, base in systems_by_fold.items():
        name = base["name"]
        position = position_by_fold.get(fold_name)
        if not isinstance(position, dict):
            position = {}
        explored_entry = explored_by_fold.get(fold_name)
        if not isinstance(explored_entry, dict):
            explored_entry = {}
        planet_types = explored_entry.get("planet_types")
        if not isinstance(planet_types, dict):
            planet_types = {}
        system_stations = stations_by_system.get(fold_name, [])
        station_ids = [station["id"] for station in system_stations]
        shop_station_ids = [station["id"] for station in system_stations if station.get("itemCount", 0)]
        explored_hazard = _as_number(explored_entry.get("hazard_rating"))
        hazard = _as_number(explored_entry.get("hazard_rating"))
        system_results.append(
            {
                "id": str(explored_entry.get("system_id") or name),
                "name": name,
                "x": _as_number(position.get("coord_x")),
                "y": _as_number(position.get("coord_y")),
                "hasPosition": _as_number(position.get("coord_x")) is not None and _as_number(position.get("coord_y")) is not None,
                "explored": bool(explored_entry),
                "hazard": hazard,
                "hazardKnown": hazard is not None,
                "planetTypes": planet_types,
                "moonCount": _as_number(explored_entry.get("moon_count")) or 0,
                "ownership": ownership_by_fold.get(fold_name),
                "npcStationCount": _as_number(npc_by_fold.get(fold_name)) or 0,
                "stationCounts": {
                    "mine": _as_number(mine_by_fold.get(fold_name)) or 0,
                    "coalition": _as_number(coalition_by_fold.get(fold_name)) or 0,
                    "others": _as_number(others_by_fold.get(fold_name)) or 0,
                },
                "stationIds": station_ids,
                "shopStationIds": shop_station_ids,
            }
        )
    system_results.sort(key=lambda system: system["name"].casefold())

    canonical_names = {system["name"].casefold(): system["name"] for system in system_results}
    edge_keys: set[tuple[str, str]] = set()
    edges: list[dict[str, str]] = []
    for entry in explored_rows:
        source = canonical_names.get(str(entry.get("name") or "").strip().casefold())
        if not source:
            continue
        for gate in entry.get("warp_gates") or []:
            if not isinstance(gate, dict):
                continue
            target = canonical_names.get(str(gate.get("target_system") or "").strip().casefold())
            if not target or target == source:
                continue
            key = tuple(sorted((source.casefold(), target.casefold())))
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edges.append({"source": source, "target": target})
    edges.sort(key=lambda edge: (edge["source"].casefold(), edge["target"].casefold()))

    mapped_stations = [station for station in stations if station.get("systemName")]
    unmapped_stations = [station for station in stations if not station.get("systemName")]
    observations = [
        snapshots.get(name, {}).get("observedAt", "")
        for name in ("galaxyStatic", "galaxyMap", "exploredSystems", "myStations")
    ]
    return {
        "hasData": bool(system_results),
        "observedAt": max(observations, default="") or None,
        "systems": system_results,
        "edges": edges,
        "territory": territory,
        "territoryPositions": positions,
        "stations": [dict(station) for station in stations],
        "unmappedStations": [station["id"] for station in unmapped_stations],
        "systemCount": len(system_results),
        "edgeCount": len(edges),
        "territoryCount": len(territory),
        "mappedStationCount": len(mapped_stations),
        "unmappedStationCount": len(unmapped_stations),
    }


class DataStore:
    def __init__(self, archive_store: ArchiveStore | None = None) -> None:
        self._lock = threading.Lock()
        self._archive_store = archive_store or ArchiveStore()
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._data: dict[str, Any] | None = None
        self._asset_signature: tuple[tuple[str, int], ...] | None = None
        self._assets: dict[str, dict[str, str]] = {}

    def _asset_manifest(self) -> dict[str, dict[str, str]]:
        signature: list[tuple[str, int]] = []
        for folder in sorted(SAFE_ASSET_FOLDERS):
            path = ASSET_ROOT / folder
            try:
                signature.append((folder, path.stat().st_mtime_ns))
            except OSError:
                signature.append((folder, 0))
        current = tuple(signature)
        if current == self._asset_signature:
            return self._assets

        assets: dict[str, dict[str, str]] = {}
        for folder in SAFE_ASSET_FOLDERS:
            entries: dict[str, str] = {}
            path = ASSET_ROOT / folder
            if path.is_dir():
                for image in path.iterdir():
                    if image.is_file() and image.suffix.lower() in {".png", ".gif", ".bmp"}:
                        entries[_normalise(image.stem)] = image.name
            assets[folder] = entries
        self._asset_signature = current
        self._assets = assets
        return assets

    def _log_files(self) -> list[Path]:
        """Return every local generation of the client log, oldest first."""
        candidates: dict[str, Path] = {}
        if LOG_PATH.parent.is_dir():
            for path in LOG_PATH.parent.glob(f"{LOG_PATH.name}*"):
                if path.is_file():
                    candidates[str(path.resolve()).casefold()] = path
        internal_log = ASSET_ROOT / LOG_PATH.name
        if internal_log.is_file():
            candidates[str(internal_log.resolve()).casefold()] = internal_log
        if LOG_PATH.is_file():
            candidates[str(LOG_PATH.resolve()).casefold()] = LOG_PATH

        def age(path: Path) -> tuple[int, str]:
            try:
                return path.stat().st_mtime_ns, str(path).casefold()
            except OSError:
                return 0, str(path).casefold()

        return sorted(candidates.values(), key=age)

    def _item_art(self, category: str, item: dict[str, Any]) -> dict[str, Any] | None:
        folder = IMAGE_FOLDER_BY_CATEGORY.get(category)
        if not folder:
            return None
        entries = self._asset_manifest().get(folder, {})
        for match_kind, candidate in (
            ("type", item.get("type")),
            ("name", item.get("display_name")),
        ):
            filename = entries.get(_normalise(candidate))
            if filename:
                return {
                    "folder": folder,
                    "filename": filename,
                    "match": match_kind,
                }
        return None

    def _planet_art(self, planet_type: Any) -> dict[str, Any] | None:
        folder = "OrbitalBodies"
        entries = self._asset_manifest().get(folder, {})
        type_key = _normalise(planet_type)
        candidates = [type_key, f"{type_key}1"]
        for candidate in candidates:
            filename = entries.get(candidate)
            if filename:
                return {
                    "folder": folder,
                    "filename": filename,
                    "match": "planet_type",
                }
        return None

    def get(self, force: bool = False) -> dict[str, Any]:
        log_files = self._log_files()
        signature_parts: list[tuple[str, int, int]] = []
        for path in log_files:
            try:
                stat = path.stat()
                signature_parts.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        signature = tuple(signature_parts)

        with self._lock:
            if not force and self._data is not None and signature == self._signature:
                return self._data
            current = self._build(signature, log_files)
            try:
                self._data = self._archive_store.merge(current)
            except OSError as error:
                current.setdefault("meta", {})["archiveWarning"] = str(error)
                self._data = current
            # Rebuilt from the MERGED catalog, not the freshly parsed one.
            # _build only sees the current log, so shopping rows assembled
            # there covered a few hundred prices instead of every price ever
            # observed -- the whole point of the tab is the archive's memory.
            self._data["shopping"] = shopping_rows(
                self._data.get("items") or [], self._data.get("stations") or [])
            self._signature = signature
            return self._data

    def _build(
        self,
        signature: tuple[tuple[str, int, int], ...],
        log_files: list[Path],
    ) -> dict[str, Any]:
        best_items: dict[str, dict[str, Any]] = {}
        best_scores: dict[str, int] = {}
        stat_sets: dict[str, dict[str, Any]] = defaultdict(dict)
        markets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        training_offers: dict[str, dict[str, Any]] = {}
        scans: dict[str, dict[str, Any]] = {}
        body_rosters: dict[str, dict[str, Any]] = {}
        malformed = 0
        catalog_rows = 0
        training_rows = 0
        scan_rows = 0
        body_roster_rows = 0
        scan_attempts = 0
        failed_scans = 0
        snapshot_rows = 0
        snapshots: dict[str, dict[str, Any]] = {}
        extractor_snapshots: dict[str, dict[str, Any]] = {}
        extractor_snapshot_rows = 0
        colony_economy_snapshots: dict[str, dict[str, Any]] = {}
        colony_economy_snapshot_rows = 0
        newest_timestamp = ""
        current_system_id: str | None = None

        for log_file in log_files:
            with log_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    timestamp_match = TIMESTAMP_RE.match(line)
                    timestamp = timestamp_match.group(1) if timestamp_match else ""
                    if timestamp > newest_timestamp:
                        newest_timestamp = timestamp

                    warp_match = WARP_SYSTEM_RE.search(line)
                    if warp_match:
                        current_system_id = warp_match.group(1)

                    marker_index = line.find(SHOP_MARKER)
                    if marker_index >= 0:
                        try:
                            record = json.loads(line[marker_index + len(SHOP_MARKER) :])
                            item = record.get("item") or {}
                            item_type = str(item.get("type") or "").strip()
                            if not item_type:
                                continue
                            category = _canonical_category(record)
                            item_key = f"{category}:{item_type}"
                            catalog_rows += 1

                            stats = item.get("stats")
                            if isinstance(stats, dict):
                                stat_sets[item_key].update(stats)

                            score = _record_score(item)
                            if item_key not in best_items or score >= best_scores[item_key]:
                                best_items[item_key] = dict(item)
                                best_scores[item_key] = score

                            station_id, station_name = _station_identity(record)
                            source = str(record.get("source") or "UNKNOWN")
                            market_key = f"{station_id}|{source}"
                            markets[item_key][market_key] = {
                                "stationId": station_id,
                                "stationName": station_name,
                                "source": source,
                                "sourceLabel": _source_label(source),
                                "systemName": str(record.get("system_name") or "").strip() or None,
                                "buyPrice": _as_number(item.get("price")),
                                "sellPrice": _as_number(item.get("ps_sell_value")),
                                "stock": _as_number(item.get("stock")),
                                "minimum": _as_number(item.get("min_qty")),
                                "maximum": _as_number(item.get("max_qty")),
                                "noSell": bool(item.get("no_sell", False)),
                                "observedAt": timestamp,
                            }
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1
                        continue

                    marker_index = line.find(TRAINING_MARKER)
                    if marker_index >= 0:
                        try:
                            record = json.loads(line[marker_index + len(TRAINING_MARKER) :])
                            offer = record.get("offer") if isinstance(record, dict) else None
                            if not isinstance(offer, dict) or not offer.get("skill_id"):
                                continue
                            station_id = str(record.get("station_id") or "unknown")
                            station_name = str(record.get("station_name") or "").strip()
                            if not station_name:
                                station_name = f"NPC station · {station_id}" if station_id != "unknown" else "NPC station"
                            skill_id = str(offer["skill_id"])
                            training_offers[f"{station_id}|{skill_id}"] = {
                                "skillId": skill_id,
                                "stationId": station_id,
                                "stationName": station_name,
                                "systemName": str(record.get("system_name") or "").strip() or None,
                                "source": str(record.get("source") or "NPC_STATION_DOCK_OK"),
                                "offeredMax": _as_number(offer.get("max_level")),
                                "itemCostClass": offer.get("item_cost_class"),
                                "itemCostType": offer.get("item_cost_type"),
                                "itemCostDisplay": offer.get("item_cost_display"),
                                "itemCostAmount": _as_number(offer.get("item_cost_amount")),
                                "itemCostScale": str(offer.get("item_cost_scale") or "flat"),
                                "observedAt": timestamp,
                            }
                            training_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1
                        continue

                    marker_index = line.find(SCAN_MARKER)
                    if marker_index >= 0:
                        try:
                            scan = json.loads(line[marker_index + len(SCAN_MARKER) :])
                            scan_attempts += 1
                            if not scan.get("ok") or not scan.get("planet_name") or not scan.get("planet_type"):
                                failed_scans += 1
                                continue
                            planet_id = str(scan.get("planet_id") or scan.get("planet_name"))
                            scan = dict(scan)
                            scan["isScanned"] = True
                            scan["observedAt"] = timestamp
                            if current_system_id and not scan.get("system_name"):
                                scan["_systemIdHint"] = current_system_id
                            scans[planet_id] = scan
                            scan_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1

                    marker_index = line.find(SYSTEM_BODY_ROSTER_MARKER)
                    if marker_index >= 0:
                        try:
                            roster = json.loads(
                                line[marker_index + len(SYSTEM_BODY_ROSTER_MARKER) :]
                            )
                            system_name = str(roster.get("system_name") or "").strip()
                            bodies = roster.get("bodies")
                            if not system_name or not isinstance(bodies, list):
                                raise ValueError("system body roster is incomplete")
                            for raw_body in bodies:
                                if not isinstance(raw_body, dict):
                                    continue
                                planet_id = str(raw_body.get("planet_id") or "").strip()
                                planet_name = str(raw_body.get("planet_name") or "").strip()
                                if not planet_id or not planet_name:
                                    continue
                                is_moon = bool(raw_body.get("is_moon"))
                                planet_type = str(raw_body.get("planet_type") or "").strip()
                                if not planet_type:
                                    planet_type = "Moon" if is_moon else "Unknown"
                                record = {
                                    "planet_id": planet_id,
                                    "planet_name": planet_name,
                                    "planet_type": planet_type,
                                    "system_name": system_name,
                                    "is_moon": is_moon,
                                    "isScanned": False,
                                    "observedAt": timestamp,
                                }
                                existing = body_rosters.get(planet_id)
                                if existing is None or str(record["observedAt"] or "") >= str(existing.get("observedAt") or ""):
                                    body_rosters[planet_id] = record
                                body_roster_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1

                    marker_index = line.find(STATION_EXTRACTOR_MARKER)
                    if marker_index >= 0:
                        try:
                            record = json.loads(line[marker_index + len(STATION_EXTRACTOR_MARKER) :])
                            normalised = _normalise_extractor_snapshot(record)
                            if normalised is not None:
                                normalised["observedAt"] = timestamp or normalised.get("observedAt")
                                station_id = str(normalised["stationId"])
                                existing = extractor_snapshots.get(station_id)
                                if existing is None or str(normalised.get("observedAt") or "") >= str(existing.get("observedAt") or ""):
                                    extractor_snapshots[station_id] = normalised
                                extractor_snapshot_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1
                        continue

                    marker_index = line.find(COLONY_ECONOMY_MARKER)
                    if marker_index >= 0:
                        try:
                            record = json.loads(line[marker_index + len(COLONY_ECONOMY_MARKER) :])
                            normalised = _normalise_colony_economy_snapshot(record)
                            if normalised is not None:
                                normalised["observedAt"] = timestamp or normalised.get("observedAt")
                                station_id = str(normalised["stationId"])
                                existing = colony_economy_snapshots.get(station_id)
                                if existing is None or str(normalised.get("observedAt") or "") >= str(existing.get("observedAt") or ""):
                                    colony_economy_snapshots[station_id] = normalised
                                colony_economy_snapshot_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1
                        continue
                    for marker, snapshot_name in SNAPSHOT_MARKERS.items():
                        marker_index = line.find(marker)
                        if marker_index < 0:
                            continue
                        try:
                            payload = json.loads(line[marker_index + len(marker) :])
                            snapshots[snapshot_name] = {
                                "data": payload,
                                "observedAt": timestamp,
                            }
                            snapshot_rows += 1
                        except (json.JSONDecodeError, TypeError, ValueError):
                            malformed += 1
                        break

        item_results: list[dict[str, Any]] = []
        image_count = 0
        priced_count = 0
        stat_count = 0
        category_counts: dict[str, int] = defaultdict(int)

        for item_key, raw in best_items.items():
            category, item_type = item_key.split(":", 1)
            item_markets = sorted(
                markets[item_key].values(),
                key=lambda market: (market.get("stationName", ""), market.get("sourceLabel", "")),
            )
            art = self._item_art(category, raw)
            if art:
                image_count += 1
                illustration = dict(art)
                illustration["kind"] = "official"
            else:
                illustration = {
                    "kind": "generated",
                    "match": "generated",
                }
            merged_stats = dict(stat_sets[item_key])
            if merged_stats:
                stat_count += 1
            has_price = any(
                (market.get("buyPrice") or 0) > 0 or (market.get("sellPrice") or 0) > 0
                for market in item_markets
            )
            if has_price:
                priced_count += 1
            category_counts[category] += 1
            item_results.append(
                {
                    "id": item_key,
                    "type": item_type,
                    "category": category,
                    "categoryLabel": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
                    "name": str(raw.get("display_name") or item_type.replace("_", " ").title()),
                    "description": str(raw.get("description") or ""),
                    "tech": _as_number(raw.get("tech") if raw.get("tech") is not None else raw.get("tier")),
                    "cargoSize": _as_number(raw.get("cargo_size")),
                    "rarity": str(raw.get("rarity_color") or "common"),
                    "colour": _safe_colour(raw.get("color_rgb")),
                    "stats": merged_stats,
                    "markets": item_markets,
                    "art": art,
                    "illustration": illustration,
                    "flags": {
                        "noSell": bool(raw.get("no_sell", False)),
                        "skillLocked": bool(raw.get("skill_locked", False)),
                        "hasPrice": has_price,
                    },
                }
            )

        item_results.sort(key=lambda item: (item["name"].casefold(), item["category"]))

        station_rollup: dict[str, dict[str, Any]] = {}
        for item in item_results:
            for market in item["markets"]:
                station_id = market["stationId"]
                station = station_rollup.setdefault(
                    station_id,
                    {
                        "id": station_id,
                        "name": market["stationName"],
                        "sources": set(),
                        "itemIds": set(),
                        "pricedItemIds": set(),
                        "systemSeen": {},
                        "isMine": False,
                        "lastSeen": "",
                    },
                )
                station["sources"].add(market["sourceLabel"])
                station["itemIds"].add(item["id"])
                if (market.get("buyPrice") or 0) > 0 or (market.get("sellPrice") or 0) > 0:
                    station["pricedItemIds"].add(item["id"])
                if market.get("observedAt", "") > station["lastSeen"]:
                    station["lastSeen"] = market["observedAt"]
                system_name = str(market.get("systemName") or "").strip()
                if system_name:
                    station["systemSeen"][system_name] = max(
                        station["systemSeen"].get(system_name, ""),
                        market.get("observedAt", ""),
                    )

        my_stations_payload = snapshots.get("myStations", {}).get("data")
        if not isinstance(my_stations_payload, dict):
            my_stations_payload = {}
        my_station_rows = my_stations_payload.get("stations")
        if not isinstance(my_station_rows, list):
            my_station_rows = []
        my_station_observed = snapshots.get("myStations", {}).get("observedAt", "")
        station_name_index = {
            _normalise(station.get("name")): station_id
            for station_id, station in station_rollup.items()
            if _normalise(station.get("name"))
        }
        for index, raw_station in enumerate(my_station_rows):
            if not isinstance(raw_station, dict):
                continue
            station_name = str(
                raw_station.get("display_name")
                or raw_station.get("station_name")
                or raw_station.get("name")
                or f"Your station {index + 1}"
            ).strip()
            raw_id = raw_station.get("station_id", raw_station.get("id", raw_station.get("pid")))
            station_id = str(raw_id).strip() if raw_id not in (None, "") else ""
            if not station_id:
                station_id = station_name_index.get(_normalise(station_name), f"my:{_normalise(station_name) or index}")
            station = station_rollup.setdefault(
                station_id,
                {
                    "id": station_id,
                    "name": station_name,
                    "sources": set(),
                    "itemIds": set(),
                    "pricedItemIds": set(),
                    "systemSeen": {},
                    "isMine": True,
                    "lastSeen": "",
                },
            )
            if _is_placeholder_station_name(station["name"], station_id):
                # A real name from MY_STATIONS always beats one derived from
                # the id by _station_identity.
                station["name"] = station_name
            station["sources"].add("Your station")
            station["isMine"] = True
            system_name = str(raw_station.get("system_name") or "").strip()
            if system_name:
                station["systemSeen"][system_name] = max(
                    station["systemSeen"].get(system_name, ""),
                    my_station_observed,
                )
            if my_station_observed > station["lastSeen"]:
                station["lastSeen"] = my_station_observed

        stations = []
        for station in station_rollup.values():
            known_systems = sorted(station["systemSeen"], key=str.casefold)
            primary_system = max(
                known_systems,
                key=lambda name: station["systemSeen"].get(name, ""),
                default=None,
            )
            stations.append(
                {
                    "id": station["id"],
                    "name": station["name"],
                    "sources": sorted(station["sources"]),
                    "itemCount": len(station["itemIds"]),
                    "pricedItemCount": len(station["pricedItemIds"]),
                    "itemIds": sorted(station["itemIds"]),
                    "systemName": primary_system,
                    "knownSystems": known_systems,
                    "isMine": bool(station["isMine"]),
                    "lastSeen": station["lastSeen"],
                }
            )
        stations.sort(key=lambda station: station["name"].casefold())

        station_systems = {
            station["id"]: station.get("systemName")
            for station in stations
            if station.get("systemName")
        }
        for item in item_results:
            for market in item["markets"]:
                if not market.get("systemName"):
                    market["systemName"] = station_systems.get(market["stationId"])

        explored_payload = snapshots.get("exploredSystems", {}).get("data")
        system_names_by_id = {
            str(system.get("system_id")): str(system.get("name") or "").strip()
            for system in explored_payload
            if isinstance(system, dict) and system.get("system_id") is not None
        } if isinstance(explored_payload, list) else {}

        # A real scanner response always takes precedence over an entry-only
        # roster row from this log. ArchiveStore keeps that same rule when
        # combining current observations with old or shared archive data.
        for planet_id, roster in body_rosters.items():
            scans.setdefault(planet_id, roster)

        scan_results = []
        for scan in scans.values():
            scan = dict(scan)
            system_id_hint = str(scan.pop("_systemIdHint", "") or "")
            if not scan.get("system_name") and system_id_hint:
                scan["system_name"] = system_names_by_id.get(system_id_hint) or None
            scan["art"] = self._planet_art(scan.get("planet_type"))
            scan_results.append(scan)
        scan_results.sort(key=lambda scan: str(scan.get("planet_name") or "").casefold())

        categories = [
            {
                "id": category,
                "label": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
                "count": count,
            }
            for category, count in sorted(
                category_counts.items(),
                key=lambda pair: (-pair[1], CATEGORY_LABELS.get(pair[0], pair[0]).casefold()),
            )
        ]

        log_stats = []
        for path in log_files:
            try:
                log_stats.append(path.stat())
            except OSError:
                continue
        modified = (
            datetime.fromtimestamp(max(stat.st_mtime for stat in log_stats)).isoformat(timespec="seconds")
            if log_stats
            else None
        )
        log_size = sum(stat.st_size for stat in log_stats)

        credits_payload = snapshots.get("credits", {}).get("data")
        if not isinstance(credits_payload, dict):
            credits_payload = {}
        xp_payload = snapshots.get("xp", {}).get("data")
        if not isinstance(xp_payload, dict):
            xp_payload = {}
        skills_payload = snapshots.get("skills", {}).get("data")
        if isinstance(skills_payload, dict):
            skills = skills_payload.get("skills", [])
            mastery_tree = skills_payload.get("mastery_tree")
        elif isinstance(skills_payload, list):
            skills = skills_payload
            mastery_tree = None
        else:
            skills = []
            mastery_tree = None
        if not isinstance(skills, list):
            skills = []

        player_observations = [
            snapshots.get(name, {}).get("observedAt", "")
            for name in ("credits", "xp", "skills")
        ]
        ship_observations = [
            snapshots.get(name, {}).get("observedAt", "")
            for name in ("inventory", "hangarInventory", "plugins", "specs")
        ]
        player = {
            "hasData": any(name in snapshots for name in ("credits", "xp", "skills")),
            "observedAt": max(player_observations, default="") or None,
            "credits": _as_number(credits_payload.get("credits")),
            "creditBreakdown": credits_payload.get("breakdown", []),
            "xp": {
                "current": _as_number(xp_payload.get("xp_current")),
                "needed": _as_number(xp_payload.get("xp_needed")),
                "level": _as_number(xp_payload.get("level")),
                "skillPoints": _as_number(xp_payload.get("skill_points")),
                "enemyKills": _as_number(xp_payload.get("enemy_kills")),
                "playerKills": _as_number(xp_payload.get("player_kills")),
                "playSeconds": _as_number(xp_payload.get("play_seconds")),
            },
            "skills": skills,
            "masteryTree": mastery_tree,
        }
        ship = {
            "hasData": any(name in snapshots for name in ("inventory", "hangarInventory", "plugins", "specs")),
            "observedAt": max(ship_observations, default="") or None,
            "inventory": snapshots.get("inventory", {}).get("data", []),
            "hangarInventory": snapshots.get("hangarInventory", {}).get("data", []),
            "plugins": snapshots.get("plugins", {}).get("data", {}),
            "specs": snapshots.get("specs", {}).get("data", {}),
        }

        skill_by_id = {
            str(skill.get("skill_id")): skill
            for skill in skills
            if isinstance(skill, dict) and skill.get("skill_id")
        }
        spent_skill_points = sum(
            int(_as_number(skill.get("cost_paid")) or 0)
            for skill in skills
            if isinstance(skill, dict)
        )
        available_skill_points = max(
            0,
            int(_as_number(xp_payload.get("skill_points")) or 0) - spent_skill_points,
        )
        available_credits = float(_as_number(credits_payload.get("credits")) or 0)
        owned_training_costs: dict[tuple[str, str], int] = defaultdict(int)
        inventory_rows = ship["inventory"] if isinstance(ship["inventory"], list) else []
        for inventory_item in inventory_rows:
            if not isinstance(inventory_item, dict):
                continue
            item_class = str(inventory_item.get("item_class") or "")
            if item_class == "cargo":
                item_type = str(inventory_item.get("resource") or inventory_item.get("item_type") or "")
                if item_type:
                    owned_training_costs[("resource", item_type)] += int(_as_number(inventory_item.get("amount")) or 0)
                continue
            item_type = str(inventory_item.get("item_type") or "")
            item_category = str(inventory_item.get("item_category") or item_class)
            if item_type and item_category:
                owned_training_costs[(item_category, item_type)] += int(_as_number(inventory_item.get("amount")) or 1)

        training_results = []
        for raw_offer in training_offers.values():
            offer = dict(raw_offer)
            skill = skill_by_id.get(offer["skillId"], {})
            current_level = int(_as_number(skill.get("level")) or 0)
            offered_max = int(_as_number(offer.get("offeredMax")) or 0)
            next_sp_cost = _as_number(skill.get("next_cost"))
            next_credit_cost = _as_number(skill.get("next_credit_cost"))
            item_cost_class = str(offer.get("itemCostClass") or "")
            item_cost_type = str(offer.get("itemCostType") or "")
            base_item_cost = int(_as_number(offer.get("itemCostAmount")) or 0)
            item_cost_needed = (
                base_item_cost * (current_level + 1)
                if base_item_cost and offer.get("itemCostScale") == "linear"
                else base_item_cost
            )
            item_owned = owned_training_costs.get((item_cost_class, item_cost_type), 0)
            at_station_cap = offered_max > 0 and current_level >= offered_max
            can_afford_sp = next_sp_cost is not None and available_skill_points >= float(next_sp_cost)
            can_afford_credits = next_credit_cost is None or available_credits >= float(next_credit_cost)
            can_afford_item = not item_cost_needed or item_owned >= item_cost_needed
            offer.update(
                {
                    "displayName": str(skill.get("display_name") or offer["skillId"].replace("_", " ").title()),
                    "description": str(skill.get("description") or ""),
                    "currentLevel": current_level,
                    "globalMax": _as_number(skill.get("max_level")),
                    "nextSpCost": next_sp_cost,
                    "nextCreditCost": next_credit_cost,
                    "statBonus": skill.get("stat_bonus") if isinstance(skill.get("stat_bonus"), dict) else {},
                    "pctBonus": skill.get("pct_bonus") if isinstance(skill.get("pct_bonus"), dict) else {},
                    "availableSkillPoints": available_skill_points,
                    "itemCostNeeded": item_cost_needed,
                    "itemOwned": item_owned,
                    "atStationCap": at_station_cap,
                    "canAffordSp": can_afford_sp,
                    "canAffordCredits": can_afford_credits,
                    "canAffordItem": can_afford_item,
                    "canTrainNow": not at_station_cap and can_afford_sp and can_afford_credits and can_afford_item,
                }
            )
            training_results.append(offer)
        training_results.sort(
            key=lambda offer: (
                str(offer.get("displayName") or "").casefold(),
                str(offer.get("stationName") or "").casefold(),
            )
        )
        map_data = _map_dataset(snapshots, stations)
        private_extractor_usage = sorted(
            extractor_snapshots.values(),
            key=lambda record: (
                str(record.get("systemName") or "").casefold(),
                str(record.get("stationId") or ""),
            ),
        )
        private_colony_economy = sorted(
            colony_economy_snapshots.values(),
            key=lambda record: (
                str(record.get("systemName") or "").casefold(),
                str(record.get("stationId") or ""),
            ),
        )

        return {
            "meta": {
                "generatedAt": datetime.now().isoformat(timespec="seconds"),
                "latestObservation": newest_timestamp or None,
                "logPath": str(LOG_PATH),
                "logPaths": [str(path) for path in log_files],
                "logCount": len(log_files),
                "logExists": bool(log_files),
                "logModified": modified,
                "logBytes": log_size,
                "catalogRows": catalog_rows,
                "trainingRows": training_rows,
                "trainingOfferCount": len(training_results),
                "trainingSkillCount": len({offer["skillId"] for offer in training_results}),
                "scanRows": scan_rows,
                "bodyRosterRows": body_roster_rows,
                "extractorSnapshotRows": extractor_snapshot_rows,
                "extractorStationCount": len(private_extractor_usage),
                "colonyEconomySnapshotRows": colony_economy_snapshot_rows,
                "colonyEconomyStationCount": len(private_colony_economy),
                "snapshotRows": snapshot_rows,
                "scanAttempts": scan_attempts,
                "failedScans": failed_scans,
                "malformedRows": malformed,
                "itemCount": len(item_results),
                "categoryCount": len(categories),
                "stationCount": len(stations),
                "scanCount": len(scan_results),
                "scannedBodyCount": sum(
                    1
                    for scan in scan_results
                    if bool(scan.get("isScanned"))
                    or ("isScanned" not in scan and scan.get("ok") is not False)
                ),
                "unscannedBodyCount": sum(
                    1
                    for scan in scan_results
                    if not bool(scan.get("isScanned"))
                    and not ("isScanned" not in scan and scan.get("ok") is not False)
                ),
                "mapSystemCount": map_data["systemCount"],
                "mapEdgeCount": map_data["edgeCount"],
                "mappedStationCount": map_data["mappedStationCount"],
                "withStats": stat_count,
                "withPrice": priced_count,
                "withArt": image_count,
                "illustrated": len(item_results),
                "assetRoot": str(ASSET_ROOT),
                "signature": [list(part) for part in signature],
            },
            "categories": categories,
            "items": item_results,
            "stations": stations,
            "shopping": shopping_rows(item_results, stations),
            "scans": scan_results,
            # Dock-authorised module counts are intentionally local-only.
            # sharing.sanitise_catalog is a whitelist and does not export this.
            "privateExtractorUsage": private_extractor_usage,
            # The personal Colony-tab basket stays local and is never shared.
            "privateColonyEconomy": private_colony_economy,
            "training": {
                "offers": training_results,
                "offerCount": len(training_results),
                "skillCount": len({offer["skillId"] for offer in training_results}),
                "stationCount": len({offer["stationId"] for offer in training_results}),
            },
            "player": player,
            "ship": ship,
            "map": map_data,
        }


STORE = DataStore()
