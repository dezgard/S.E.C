from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
TECH_RE = re.compile(r"T\s*(\d+)", re.IGNORECASE)

PERCENT_LABELS = {
    "weapon damage": "weapon_damage",
    "weapon range": "weapon_range",
    "fire rate": "fire_rate",
    "rate of fire": "fire_rate",
    "shield bonus": "shield_bank",
    "shield bank": "shield_bank",
    "shield recharge": "shield_regen",
    "shield regen": "shield_regen",
    "energy bank": "energy_bank",
    "energy recharge": "energy_recharge",
    "energy regen": "energy_recharge",
    "hull capacity": "hull_capacity",
    "cargo capacity": "hull_capacity",
    "thrust bonus": "thrust",
    "turning bonus": "turning",
    "speed bonus": "max_speed",
    "max speed": "max_speed",
    "effective mass": "mass",
    "mass": "mass",
    "flat damage mitigation": "flat_damage_mitigation",
    "kinetic resist": "kinetic_resistance",
    "kinetic resistance": "kinetic_resistance",
    "laser resist": "laser_resistance",
    "laser resistance": "laser_resistance",
    "thermal resist": "thermal_resistance",
    "thermal resistance": "thermal_resistance",
    "biogenic resist": "biogenic_resistance",
    "biogenic resistance": "biogenic_resistance",
    "mining resist": "mining_resistance",
    "mining resistance": "mining_resistance",
    "energy resist": "energy_resistance",
    "energy resistance": "energy_resistance",
}

SKILL_KEYS = {
    "ship_speed": "max_speed",
    "max_speed": "max_speed",
    "energy_output": "energy_recharge",
    "energy_recharge": "energy_recharge",
    "max_energy": "energy_bank",
    "energy_bank": "energy_bank",
    "max_shields": "shield_bank",
    "shield_bank": "shield_bank",
    "_shield_recharge_rate": "shield_regen",
    "shield_regen": "shield_regen",
    "cargo_capacity": "hull_capacity",
    "hull_capacity": "hull_capacity",
    "_accel": "accel",
    "turn_rate": "turn_rate",
    "thrust": "thrust",
    "turning": "turning",
    "mass": "mass",
    "weapon_damage": "weapon_damage",
    "weapon_range": "weapon_range",
    "fire_rate": "fire_rate",
    "flat_damage_mitigation": "flat_damage_mitigation",
}

DAMAGE_TYPES = ("kinetic", "laser", "thermal", "biogenic", "mining", "energy")


def number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER_RE.search(str(value))
    if not match:
        return default
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return default


def percentage(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value)
    parsed = number(value, default)
    return parsed / 100.0 if "%" in text else parsed


def stats(item: dict[str, Any] | None) -> dict[str, Any]:
    value = (item or {}).get("stats")
    return value if isinstance(value, dict) else {}


def stat(item: dict[str, Any] | None, *labels: str, default: float = 0.0) -> float:
    values = stats(item)
    folded = {str(key).casefold(): value for key, value in values.items()}
    for label in labels:
        if label.casefold() in folded:
            return number(folded[label.casefold()], default)
    return default


def stat_text(item: dict[str, Any] | None, *labels: str) -> str:
    values = stats(item)
    folded = {str(key).casefold(): value for key, value in values.items()}
    for label in labels:
        if label.casefold() in folded:
            return str(folded[label.casefold()])
    return ""


def _iter_items(fit: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("hull", "engine", "shield", "energy"):
        item = fit.get(key)
        if isinstance(item, dict) and item:
            yield item
    for key in ("weapons", "plugins", "equipment"):
        value = fit.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item:
                    yield item


def item_percent_bonuses(items: Iterable[dict[str, Any]]) -> dict[str, float]:
    bonuses: defaultdict[str, float] = defaultdict(float)
    for item in items:
        for label, value in stats(item).items():
            key = PERCENT_LABELS.get(str(label).casefold())
            if (
                key
                and key.endswith("_resistance")
                and str(item.get("category") or item.get("item_category") or "").casefold() == "ship"
            ):
                # A hull's resistance rows are its base values. Resistance
                # rows on plugins/equipment are actual additive bonuses.
                continue
            if key and "%" in str(value):
                bonuses[key] += percentage(value)
    return dict(bonuses)


def skill_bonuses(skills: list[dict[str, Any]] | None) -> tuple[dict[str, float], dict[str, float]]:
    pct: defaultdict[str, float] = defaultdict(float)
    flat: defaultdict[str, float] = defaultdict(float)
    for skill in skills or []:
        if not isinstance(skill, dict):
            continue
        level = number(skill.get("level"), 0.0)
        if level <= 0:
            continue
        pct_values = skill.get("pct_bonus")
        if isinstance(pct_values, dict):
            for raw_key, value in pct_values.items():
                key = SKILL_KEYS.get(str(raw_key), str(raw_key))
                pct[key] += number(value) * level
        # Engines exposes its true pre-mass thrust/turning increments through
        # display_stat_bonus; ordinary skills use stat_bonus. Prefer the
        # explicit display path exactly as the game UI does.
        flat_values = skill.get("display_stat_bonus") or skill.get("stat_bonus")
        if isinstance(flat_values, dict):
            for raw_key, value in flat_values.items():
                key = SKILL_KEYS.get(str(raw_key), str(raw_key))
                flat[key] += number(value) * level
    return dict(pct), dict(flat)


def _effective(base: float, key: str, pct: dict[str, float], flat: dict[str, float]) -> float:
    return max(0.0, (base + flat.get(key, 0.0)) * (1.0 + pct.get(key, 0.0)))


def _weapon_projection(
    weapon: dict[str, Any],
    pct: dict[str, float],
) -> dict[str, Any]:
    base_damage = stat(weapon, "Damage")
    base_rate = stat(weapon, "Fire Rate")
    base_range = stat(weapon, "Range")
    damage = max(0.0, base_damage * (1.0 + pct.get("weapon_damage", 0.0)))
    fire_rate = max(0.0, base_rate * (1.0 + pct.get("fire_rate", 0.0)))
    weapon_range = max(0.0, base_range * (1.0 + pct.get("weapon_range", 0.0)))
    energy_cost = stat(weapon, "Energy Cost")
    return {
        "id": weapon.get("id") or weapon.get("type"),
        "name": weapon.get("name") or weapon.get("display_name") or weapon.get("type") or "Weapon",
        "damageType": stat_text(weapon, "Damage Type") or "Unknown",
        "damage": damage,
        "fireRate": fire_rate,
        "dps": damage * fire_rate,
        "range": weapon_range,
        "energyPerShot": energy_cost,
        "energyPerSecond": energy_cost * fire_rate,
    }


def _calibration(baseline: dict[str, Any] | None, field: str, force_field: str) -> float | None:
    if not isinstance(baseline, dict):
        return None
    ship = baseline.get("ship") if isinstance(baseline.get("ship"), dict) else {}
    engine = baseline.get("engine") if isinstance(baseline.get("engine"), dict) else {}
    mass = number(ship.get("effective_mass"))
    force = number(engine.get(force_field))
    observed = number(engine.get(field))
    if mass <= 0 or force <= 0 or observed <= 0:
        return None
    return observed * mass / force


def simulate_fit(
    fit: dict[str, Any],
    skills: list[dict[str, Any]] | None = None,
    *,
    apply_skills: bool = True,
    baseline_specs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hull = fit.get("hull") if isinstance(fit.get("hull"), dict) else {}
    engine = fit.get("engine") if isinstance(fit.get("engine"), dict) else {}
    shield = fit.get("shield") if isinstance(fit.get("shield"), dict) else {}
    energy = fit.get("energy") if isinstance(fit.get("energy"), dict) else {}
    weapons = [item for item in fit.get("weapons", []) if isinstance(item, dict)]
    plugins = [item for item in fit.get("plugins", []) if isinstance(item, dict)]
    equipment = [item for item in fit.get("equipment", []) if isinstance(item, dict)]

    hull_and_plugins = [hull, *plugins]
    pct = item_percent_bonuses(hull_and_plugins)
    flat: dict[str, float] = {}
    skill_pct, skill_flat = skill_bonuses(skills if apply_skills else [])
    for key, value in skill_pct.items():
        pct[key] = pct.get(key, 0.0) + value
    flat.update(skill_flat)

    all_items = list(_iter_items({
        "hull": hull,
        "engine": engine,
        "shield": shield,
        "energy": energy,
        "weapons": weapons,
        "plugins": plugins,
        "equipment": equipment,
    }))
    raw_mass = sum(stat(item, "Mass") for item in all_items)
    effective_mass = _effective(raw_mass, "mass", pct, flat)
    cargo_capacity = _effective(stat(hull, "Cargo Cap", "Cargo Capacity"), "hull_capacity", pct, flat)
    max_speed = _effective(stat(hull, "Speed", "Max Speed"), "max_speed", pct, flat)

    thrust = _effective(stat(engine, "Thrust"), "thrust", pct, flat)
    turning = _effective(stat(engine, "Turning"), "turning", pct, flat)
    accel_scale = _calibration(baseline_specs, "accel", "thrust")
    turn_scale = _calibration(baseline_specs, "turn_rate", "turning")
    acceleration = (thrust / effective_mass * accel_scale) if accel_scale and effective_mass else None
    turn_rate = (turning / effective_mass * turn_scale) if turn_scale and effective_mass else None
    if acceleration is not None:
        acceleration = _effective(acceleration, "accel", {}, flat)
    if turn_rate is not None:
        turn_rate = _effective(turn_rate, "turn_rate", {}, flat)

    shield_bank = _effective(stat(shield, "Shield Bank"), "shield_bank", pct, flat)
    shield_regen = _effective(stat(shield, "Recharge Rate"), "shield_regen", pct, flat)
    regen_cost_text = stat_text(shield, "Regen Energy Cost")
    regen_cost_per_hp = number(regen_cost_text)
    shield_energy_per_second = shield_regen * regen_cost_per_hp

    energy_bank = _effective(stat(energy, "Capacity", "Energy Bank"), "energy_bank", pct, flat)
    energy_output = _effective(stat(energy, "Output", "Energy Output"), "energy_recharge", pct, flat)

    weapon_rows = [_weapon_projection(weapon, pct) for weapon in weapons]
    total_dps = sum(row["dps"] for row in weapon_rows)
    alpha = sum(row["damage"] for row in weapon_rows)
    weapon_energy_per_second = sum(row["energyPerSecond"] for row in weapon_rows)
    total_draw = weapon_energy_per_second + shield_energy_per_second
    energy_margin = energy_output - total_draw
    depletion_seconds = None
    if energy_margin < 0 and energy_bank > 0:
        depletion_seconds = energy_bank / -energy_margin

    resistances: dict[str, float] = {}
    effective_shields: dict[str, float] = {}
    for damage_type in DAMAGE_TYPES:
        key = f"{damage_type}_resistance"
        base = percentage(stat_text(hull, f"{damage_type.title()} Resist"))
        resistance = min(0.95, max(-0.95, base + pct.get(key, 0.0)))
        resistances[damage_type] = resistance
        effective_shields[damage_type] = shield_bank / max(0.05, 1.0 - resistance)

    flat_mitigation = _effective(
        stat(hull, "Flat Damage Mitigation"),
        "flat_damage_mitigation",
        pct,
        flat,
    )

    warnings = validate_fit(fit)
    return {
        "projected": True,
        "skillsApplied": bool(apply_skills),
        "bonuses": {"percent": pct, "flat": flat},
        "ship": {
            "name": hull.get("name") or hull.get("display_name") or hull.get("type") or "Ship",
            "mass": effective_mass,
            "cargoCapacity": cargo_capacity,
            "maxSpeed": max_speed,
            "flatDamageMitigation": flat_mitigation,
            "resistances": resistances,
            "effectiveShields": effective_shields,
        },
        "engine": {
            "name": engine.get("name") or engine.get("display_name") or engine.get("type") or "-",
            "thrust": thrust,
            "turning": turning,
            "acceleration": acceleration,
            "turnRate": turn_rate,
        },
        "shield": {
            "name": shield.get("name") or shield.get("display_name") or shield.get("type") or "-",
            "bank": shield_bank,
            "recharge": shield_regen,
            "regenEnergyPerSecond": shield_energy_per_second,
        },
        "energy": {
            "name": energy.get("name") or energy.get("display_name") or energy.get("type") or "-",
            "bank": energy_bank,
            "output": energy_output,
            "weaponDraw": weapon_energy_per_second,
            "shieldDraw": shield_energy_per_second,
            "totalDraw": total_draw,
            "margin": energy_margin,
            "depletionSeconds": depletion_seconds,
        },
        "weapons": weapon_rows,
        "damage": {"alpha": alpha, "dps": total_dps},
        "warnings": warnings,
    }


def validate_fit(fit: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    hull = fit.get("hull") if isinstance(fit.get("hull"), dict) else {}
    hull_tech = int(number(hull.get("tech"), stat(hull, "Tech")))
    max_weapons = fit.get("maxWeaponSlots")
    weapons = [item for item in fit.get("weapons", []) if isinstance(item, dict)]
    if isinstance(max_weapons, int) and len(weapons) > max_weapons:
        warnings.append(f"{len(weapons)} weapons fitted but this snapshot exposes {max_weapons} weapon slots.")
    plugin_slots = fit.get("pluginSlots")
    plugins = [item for item in fit.get("plugins", []) if isinstance(item, dict)]
    if isinstance(plugin_slots, int) and len(plugins) > plugin_slots:
        warnings.append(f"{len(plugins)} plugins fitted but the hull has {plugin_slots} plugin slots.")
    for item in _iter_items(fit):
        requirement = stat_text(item, "Ship Tech Req")
        match = TECH_RE.search(requirement)
        if match and hull_tech < int(match.group(1)):
            name = item.get("name") or item.get("display_name") or item.get("type") or "Item"
            warnings.append(f"{name} requires a T{match.group(1)}+ ship; selected hull is T{hull_tech}.")
    return warnings
