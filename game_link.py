from __future__ import annotations

import dis
import os
import marshal
import re
import shutil
import struct
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import CodeType

try:
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.archive.writers import CArchiveWriter
except ImportError:  # Source-only environments can still inspect/patch protocol.py.
    CArchiveReader = None
    CArchiveWriter = None


PATCH_VERSION = 10
SOURCE_RELATIVE = Path("_internal") / "protocol.py"
TARGET_RELATIVE = Path("Client.exe")

HOOKS = {
    "_on_npc_station_dock_ok": "NPC_STATION_DOCK_OK",
    "_on_npc_trade_refresh": "NPC_TRADE_REFRESH",
    "_on_ps_trade_listing": "PS_TRADE_LISTING",
    "_on_ps_trade_manager": "PS_TRADE_MANAGER",
}

SNAPSHOT_HOOKS = {
    "_on_galaxy_static": (
        "GALAXY_STATIC_SNAPSHOT",
        "data",
        "        if not isinstance(data, dict):",
    ),
    "_on_galaxy_map_data": (
        "GALAXY_MAP_SNAPSHOT",
        "data",
        "        if not isinstance(data, dict):",
    ),
    "_on_explored_systems": (
        "EXPLORED_SYSTEMS_SNAPSHOT",
        "data",
        "        # Legacy format:",
    ),
    "_on_my_stations_data": (
        "MY_STATIONS_SNAPSHOT",
        "data",
        "        sw = self._solar_window",
    ),
    "_on_credits_data": (
        "PLAYER_CREDITS_SNAPSHOT",
        "data",
        "        self._credits       = float(data.get(\"credits\", 0))",
    ),
    "_on_xp_data": (
        "PLAYER_XP_SNAPSHOT",
        "data",
        "        self._xp_current    = int(data.get(\"xp_current\", 0))",
    ),
    "_on_skills_data": (
        "PLAYER_SKILLS_SNAPSHOT",
        "data",
        "        if isinstance(data, dict):",
    ),
    "_on_ship_inventory_data": (
        "SHIP_INVENTORY_SNAPSHOT",
        "self._ship_inventory",
        "        if self._solar_window is not None:",
    ),
    "_on_hangar_ship_inventory_data": (
        "HANGAR_SHIP_INVENTORY_SNAPSHOT",
        "_hsi",
        "        if (self._solar_window is not None",
    ),
    "_on_ship_plugin_data": (
        "SHIP_PLUGIN_SNAPSHOT",
        "data",
        "        if self._solar_window is not None and hasattr(self._solar_window, \"set_ship_plugin_data\"):",
    ),
    "_on_ship_specs_data": (
        "SHIP_SPECS_SNAPSHOT",
        "data",
        "        if self._solar_window is not None and hasattr(self._solar_window, \"set_ship_specs_data\"):",
    ),
}

REQUIRED_METHODS = (
    "_route_game_line",
    "_on_solar_system_data",
    "_on_planet_scan_result",
    "_on_player_station_dock_ok",
    "_on_ps_station_cargo",
    "_on_colony_data",
    *HOOKS.keys(),
    *SNAPSHOT_HOOKS.keys(),
)

SHOP_HELPER = '''    # STAR_EMPIRE_ARCHIVE_LOGGER_V1: passive shop catalogue capture
    def _log_shop_catalogue(self, source: str, data: dict) -> None:
        """Log unique shop rows already delivered by the server.

        This is deliberately passive: callers hand it payloads they already
        received while docking or refreshing a trade window.  Only catalogue
        rows and their category/station context are persisted; the surrounding
        account payload is never dumped.  Exact duplicate rows are suppressed
        for the lifetime of the client process to keep the normal log compact.
        """
        if not isinstance(data, dict):
            return
        try:
            station_id = str(data.get(
                "station_id", data.get("attached_station_id", "")) or "")
            station_name = str(data.get("station_name", "") or "")
            solar_window = getattr(self, "_solar_window", None)
            system_name = str(data.get("system_name", "") or getattr(
                solar_window, "_last_solar_system_name", "") or "")
            catalogues = []
            for field in ("trade_inventory", "catalogue"):
                value = data.get(field)
                if isinstance(value, list):
                    catalogues.append((field, value))

            seen = getattr(self, "_shop_catalogue_seen", None)
            if seen is None:
                seen = set()
                self._shop_catalogue_seen = seen

            row_count = 0
            new_count = 0
            for field, catalogue in catalogues:
                for section in catalogue:
                    if not isinstance(section, dict):
                        continue
                    section_items = section.get("items")
                    if isinstance(section_items, list):
                        category_name = str(section.get("name", "") or "")
                        category_meta = {
                            "buy_cat": section.get("buy_cat"),
                            "sell_classes": section.get("sell_classes"),
                        }
                        items = section_items
                    else:
                        category_name = str(section.get(
                            "category", section.get("name", "")) or "")
                        category_meta = {}
                        items = [section]

                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_id = item.get(
                            "type", item.get("item_type", item.get("item_key")))
                        if item_id in (None, "") and not item.get("display_name"):
                            continue
                        row_count += 1
                        record = {
                            "source": source,
                            "station_id": station_id,
                            "station_name": station_name,
                            "system_name": system_name,
                            "catalogue_field": field,
                            "category": category_name,
                            "category_meta": category_meta,
                            "item": item,
                        }
                        encoded = json.dumps(
                            record, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, default=str)
                        if encoded in seen:
                            continue
                        seen.add(encoded)
                        new_count += 1
                        logger.info("SHOP_CATALOG %s", encoded)

            if row_count:
                logger.info(
                    "SHOP_CATALOG_SUMMARY source=%s station_id=%s "
                    "station_name=%r rows=%d new=%d",
                    source, station_id, station_name, row_count, new_count)

            training_inventory = data.get("training_inventory")
            if isinstance(training_inventory, list):
                training_seen = getattr(self, "_training_catalogue_seen", None)
                if training_seen is None:
                    training_seen = set()
                    self._training_catalogue_seen = training_seen
                training_new = 0
                for offer in training_inventory:
                    if not isinstance(offer, dict) or not offer.get("skill_id"):
                        continue
                    record = {
                        "source": source,
                        "station_id": station_id,
                        "station_name": station_name,
                        "system_name": system_name,
                        "offer": offer,
                    }
                    encoded = json.dumps(
                        record, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, default=str)
                    if encoded in training_seen:
                        continue
                    training_seen.add(encoded)
                    training_new += 1
                    logger.info("TRAINING_CATALOG %s", encoded)
                logger.info(
                    "TRAINING_CATALOG_SUMMARY source=%s station_id=%s "
                    "station_name=%r rows=%d new=%d",
                    source, station_id, station_name,
                    len(training_inventory), training_new)
        except Exception:
            # A diagnostic must never interfere with opening or refreshing a
            # real shop window, regardless of an unexpected payload shape.
            logger.exception("SHOP_CATALOG capture failed for %s", source)

'''

SCAN_BLOCK = '''        # STAR_EMPIRE_ARCHIVE_LOGGER_V1: passive planet scan capture
        logger.info(
            "PLANET_SCAN_RESULT %s",
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
'''

SYSTEM_BODY_ROSTER_HELPER = '''    # STAR_EMPIRE_ARCHIVE_LOGGER_V8: passive system body roster capture
    def _log_system_body_roster(self, layout: dict) -> None:
        """Log the safe, already-delivered normal-system body roster.

        This deliberately copies only the identity and classification fields
        needed for Companion's unscanned-body rows. It never logs the full
        solar layout, asks the server for data, or runs in dungeon layouts.
        """
        if not isinstance(layout, dict) or layout.get("is_dungeon"):
            return
        try:
            system_id = str(layout.get("system_id") or "").strip()
            system_name = str(layout.get("system_name") or "").strip()
            if not system_id and not system_name:
                return
            bodies = []
            for raw_body in layout.get("planets", []):
                if not isinstance(raw_body, dict):
                    continue
                body_id = str(raw_body.get("id") or "").strip()
                body_name = str(raw_body.get("name") or "").strip()
                if not body_id or not body_name:
                    continue
                is_moon = bool(raw_body.get("is_moon"))
                body_type = str(raw_body.get("planet_type") or "").strip()
                if not body_type:
                    body_type = "Moon" if is_moon else "Unknown"
                bodies.append({
                    "planet_id": body_id,
                    "planet_name": body_name,
                    "planet_type": body_type,
                    "is_moon": is_moon,
                })
            if bodies:
                self._log_archive_snapshot("SYSTEM_BODY_ROSTER", {
                    "system_id": system_id,
                    "system_name": system_name,
                    "bodies": bodies,
                })
        except Exception:
            # Companion diagnostics must never interfere with normal gameplay.
            logger.exception("System body roster capture failed")

'''

SYSTEM_BODY_ROSTER_BLOCK = '''        # STAR_EMPIRE_ARCHIVE_LOGGER_V8: passive normal-system roster only.
        self._log_system_body_roster(self._solar_layout)
'''

SNAPSHOT_HELPER = '''    # STAR_EMPIRE_ARCHIVE_LOGGER_V2: passive player and ship capture
    def _log_archive_snapshot(self, marker: str, data) -> None:
        """Log a payload that the server already delivered to this client.

        This helper never sends a request. Exact duplicates are suppressed for
        the lifetime of the process, and only explicitly hooked payloads can
        reach it.
        """
        try:
            encoded = json.dumps(
                data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            seen = getattr(self, "_archive_snapshot_seen", None)
            if seen is None:
                seen = set()
                self._archive_snapshot_seen = seen
            identity = (marker, encoded)
            if identity in seen:
                return
            seen.add(identity)
            logger.info("%s %s", marker, encoded)
        except Exception:
            # Archive diagnostics must never interfere with normal gameplay.
            logger.exception("Archive snapshot capture failed for %s", marker)

'''

STATION_EXTRACTOR_HELPER = '''    # STAR_EMPIRE_ARCHIVE_LOGGER_V11: passive docked production-module capture
    def _log_station_extractor_snapshot(self, data: dict) -> None:
        """Log known resource and processor module counts from an authorised dock.

        The dock handler supplies the station and planet context. The normal
        PS_STATION_CARGO response then supplies already-delivered equipped
        module counts. This never sends a request and excludes cargo, credits,
        weapons, and other private station state.
        """
        if not isinstance(data, dict):
            return
        try:
            raw_counts = data.get("equipped_module_counts")
            if not isinstance(raw_counts, dict):
                return
            production_module_types = {
                "metal_drill", "advanced_metal_drill", "industrial_metal_drill",
                "silicon_drill", "advanced_silicon_drill", "industrial_silicon_drill",
                "copper_extractor", "titanium_extractor", "gold_extractor",
                "oil_drill", "wood_cutter", "advanced_wood_cutter",
                "industrial_wood_cutter", "harvester", "advanced_harvester",
                "industrial_harvester", "furniture_factory", "metal_foundry", "microchip_fabricator", "ration_processor",
            }
            equipped = {}
            for module_type, quantity in raw_counts.items():
                module_key = str(module_type or "").strip()
                if module_key not in production_module_types:
                    continue
                try:
                    count = int(quantity)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    equipped[module_key] = count
            if not equipped:
                return

            context = getattr(self, "_sec_station_extractor_context", {})
            if not isinstance(context, dict):
                context = {}
            station_id = str(data.get("station_id") or context.get("station_id") or "").strip()
            system_name = str(data.get("system_name") or context.get("system_name") or "").strip()
            if not station_id or not system_name:
                return
            record = {
                "station_id": station_id,
                "station_name": str(data.get("station_name") or context.get("station_name") or "").strip(),
                "system_name": system_name,
                "planet_id": str(data.get("planet_id") or context.get("planet_id") or "").strip(),
                "planet_name": str(data.get("planet_name") or context.get("planet_name") or "").strip(),
                "equipped_module_counts": equipped,
            }
            self._log_archive_snapshot("STATION_EXTRACTOR_SNAPSHOT", record)
        except Exception:
            logger.exception("Station extractor capture failed")

'''

STATION_DOCK_CONTEXT_BLOCK = '''        # STAR_EMPIRE_ARCHIVE_LOGGER_V7: remember only dock context for the
        # following authorised cargo response; no command is sent from here.
        self._sec_station_extractor_context = {
            "station_id": data.get("attached_station_id", data.get("station_id", "")),
            "station_name": data.get("station_name", ""),
            "system_name": data.get("system_name", ""),
            "planet_id": data.get("planet_id", ""),
            "planet_name": data.get("planet_name", ""),
        }
'''

STATION_EXTRACTOR_SNAPSHOT_BLOCK = '''        # STAR_EMPIRE_ARCHIVE_LOGGER_V7: passive authorised station snapshot.
        self._log_station_extractor_snapshot(data)
'''


COLONY_ECONOMY_HELPER = '''    # STAR_EMPIRE_ARCHIVE_LOGGER_V10: passive Colony-tab economy capture
    def _log_colony_economy_snapshot(self, data: dict) -> None:
        """Log the minimal values already delivered to the normal Colony tab.

        This copies station identity, current population, server tick duration,
        and baseline per-capita basket only. It never sends a request and
        deliberately excludes prices, reserves, cargo, and other account data.
        """
        if not isinstance(data, dict):
            return
        try:
            colony = data.get("colony")
            raw_basket = data.get("basket")
            if not isinstance(colony, dict) or not isinstance(raw_basket, list):
                return
            try:
                tick_seconds = float(data.get("tick_interval_seconds"))
            except (TypeError, ValueError):
                return
            if tick_seconds <= 0:
                return
            context = getattr(self, "_sec_station_extractor_context", {})
            if not isinstance(context, dict):
                context = {}
            station_id = str(data.get("station_id") or context.get("station_id") or "").strip()
            system_name = str(data.get("system_name") or context.get("system_name") or "").strip()
            if not station_id or not system_name:
                return
            basket = []
            seen_resources = set()
            for raw_entry in raw_basket:
                if not isinstance(raw_entry, dict):
                    continue
                resource = str(raw_entry.get("resource") or "").strip()
                try:
                    per_capita = float(raw_entry.get("per_capita"))
                except (TypeError, ValueError):
                    continue
                if not resource or resource in seen_resources or per_capita <= 0:
                    continue
                basket.append({"resource": resource, "per_capita": per_capita})
                seen_resources.add(resource)
            if not basket:
                return
            self._log_archive_snapshot("COLONY_ECONOMY_SNAPSHOT", {
                "station_id": station_id,
                "station_name": str(data.get("station_name") or context.get("station_name") or "").strip(),
                "system_name": system_name,
                "planet_id": str(data.get("planet_id") or context.get("planet_id") or "").strip(),
                "planet_name": str(data.get("planet_name") or context.get("planet_name") or "").strip(),
                "tick_interval_seconds": tick_seconds,
                "population": colony.get("population"),
                "basket": basket,
            })
        except Exception:
            # Companion diagnostics must never interfere with normal gameplay.
            logger.exception("Colony economy capture failed")

'''

COLONY_ECONOMY_SNAPSHOT_BLOCK = '''        # STAR_EMPIRE_ARCHIVE_LOGGER_V10: passive Colony-tab response only.
        self._log_colony_economy_snapshot(data)
'''

@dataclass(frozen=True)
class PatchInspection:
    state: str
    message: str
    target: Path
    installed_parts: tuple[str, ...] = ()
    missing_parts: tuple[str, ...] = ()

    @property
    def can_repair(self) -> bool:
        return self.state == "repairable"


@dataclass(frozen=True)
class PatchResult:
    changed: bool
    target: Path
    backup: Path | None
    message: str
    source_backup: Path | None = None


class PatchError(RuntimeError):
    pass


def protocol_path(game_root: Path) -> Path:
    return Path(game_root) / SOURCE_RELATIVE


def client_path(game_root: Path) -> Path:
    return Path(game_root) / TARGET_RELATIVE


def _require_archive_support() -> None:
    if CArchiveReader is None or CArchiveWriter is None:
        raise PatchError("Embedded Client.exe repair support is unavailable in this build.")


def _ordered_carchive_toc(reader) -> tuple[list[tuple[str, tuple]], tuple]:
    with open(reader._filename, "rb") as handle:
        cookie_offset = reader._find_magic_pattern(handle, reader._COOKIE_MAGIC_PATTERN)
        if cookie_offset < 0:
            raise PatchError("The Client.exe PyInstaller archive cookie was not found.")
        handle.seek(cookie_offset)
        cookie = struct.unpack(reader._COOKIE_FORMAT, handle.read(reader._COOKIE_LENGTH))
        _, _, toc_offset, toc_length, _, _ = cookie
        handle.seek(reader._start_offset + toc_offset)
        raw_toc = handle.read(toc_length)

    ordered: list[tuple[str, tuple]] = []
    position = 0
    while position < len(raw_toc):
        header = struct.unpack(
            reader._TOC_ENTRY_FORMAT,
            raw_toc[position : position + reader._TOC_ENTRY_LENGTH],
        )
        entry_length, offset, length, raw_length, compressed, typecode = header
        name_data = raw_toc[position + reader._TOC_ENTRY_LENGTH : position + entry_length]
        name = name_data.rstrip(b"\0").decode("utf-8")
        ordered.append((name, (offset, length, raw_length, compressed, typecode.decode("ascii"))))
        position += entry_length
    return ordered, cookie


def _pyz_entries(pyz_data: bytes) -> list[tuple[str, tuple]]:
    """The embedded module table, in either shape PyInstaller writes it.

    Older builds marshal a list of ``(name, (typecode, offset, length))``;
    newer ones marshal a dict keyed by module name.  The game shipped a build
    with the dict form, and refusing it made the whole executable unreadable
    -- INSTALL / REPAIR reported the client "could not be read" and deployed
    nothing, which looks like a broken patcher rather than a changed format.
    """
    if pyz_data[:4] != b"PYZ\0":
        raise PatchError("Client.exe contains an invalid embedded PYZ archive.")
    toc_offset = struct.unpack("!i", pyz_data[8:12])[0]
    entries = marshal.loads(pyz_data[toc_offset:])
    if isinstance(entries, dict):
        entries = list(entries.items())
    if not isinstance(entries, list):
        raise PatchError(f"Unexpected embedded PYZ table type: {type(entries).__name__}")
    return [(name, tuple(item)) for name, item in entries]


def _embedded_module_code(executable: Path, module_name: str = "protocol") -> CodeType:
    _require_archive_support()
    reader = CArchiveReader(str(executable))
    pyz_data = reader.extract("PYZ.pyz")
    for name, (typecode, offset, length) in _pyz_entries(pyz_data):
        if name != module_name:
            continue
        if typecode != 0:
            raise PatchError(f"Embedded module {module_name!r} has unexpected type {typecode!r}.")
        try:
            code = marshal.loads(zlib.decompress(pyz_data[offset : offset + length]))
        except (ValueError, TypeError, zlib.error) as error:
            raise PatchError(f"Could not decode embedded module {module_name!r}: {error}") from error
        if not isinstance(code, CodeType):
            raise PatchError(f"Embedded module {module_name!r} is not a Python code object.")
        return code
    raise PatchError(f"Embedded module {module_name!r} was not found in Client.exe.")


def _code_strings(code: CodeType) -> set[str]:
    strings: set[str] = set()
    stack: list[object] = [code]
    while stack:
        current = stack.pop()
        values = current.co_consts if isinstance(current, CodeType) else current
        if not isinstance(values, (tuple, list, set, frozenset)):
            continue
        for value in values:
            if isinstance(value, str):
                strings.add(value)
            elif isinstance(value, CodeType):
                stack.append(value)
            elif isinstance(value, (tuple, list, set, frozenset)):
                stack.append(value)
    return strings


def _find_code_object(code: CodeType, qualified_name: str) -> CodeType | None:
    stack = [code]
    while stack:
        current = stack.pop()
        if current.co_qualname == qualified_name:
            return current
        stack.extend(value for value in current.co_consts if isinstance(value, CodeType))
    return None


def _attribute_call_arg_counts(code: CodeType, attribute_name: str) -> tuple[int, ...]:
    instructions = list(dis.get_instructions(code))
    counts: list[int] = []
    for index, instruction in enumerate(instructions):
        if instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"} or instruction.argval != attribute_name:
            continue
        for following in instructions[index + 1 : index + 14]:
            if following.opname == "CALL":
                counts.append(int(following.arg or 0))
                break
            if following.opname in {"RETURN_VALUE", "POP_TOP"}:
                break
    return tuple(counts)


def _embedded_login_layout_capacity(executable: Path) -> int | None:
    login_code = _embedded_module_code(executable, "login")
    method = _find_code_object(login_code, "LoginScreen.get_panel_layout")
    if method is None:
        raise PatchError("Embedded LoginScreen.get_panel_layout method was not found.")
    if method.co_flags & 0x04:  # CO_VARARGS
        return None
    return max(0, method.co_argcount - 1)  # Exclude the bound self argument.


def _embedded_panel_layout_compatible(executable: Path) -> bool:
    protocol_code = _embedded_module_code(executable, "protocol")
    method = _find_code_object(protocol_code, "ProtocolMixin._on_solar_system_data")
    if method is None:
        return True
    call_counts = _attribute_call_arg_counts(method, "get_panel_layout")
    if not call_counts:
        return True
    capacity = _embedded_login_layout_capacity(executable)
    return capacity is None or all(count <= capacity for count in call_counts)


def _adapt_protocol_for_embedded_client(text: str, executable: Path) -> str:
    account_call = "self._login.get_panel_layout(self.username)"
    if account_call not in text:
        return text
    capacity = _embedded_login_layout_capacity(executable)
    if capacity is None or capacity >= 1:
        return text
    adapted = text.replace(account_call, "self._login.get_panel_layout()")
    compile(adapted, "protocol.py", "exec")
    return adapted


def _embedded_part_presence(executable: Path) -> dict[str, bool]:
    strings = _code_strings(_embedded_module_code(executable))
    parts = {
        "shop helper": "SHOP_CATALOG %s" in strings and "SHOP_CATALOG capture failed for %s" in strings,
        "training catalogue": "TRAINING_CATALOG %s" in strings and "training_inventory" in strings,
        "planet scan": "PLANET_SCAN_RESULT %s" in strings,
        "snapshot helper": "Archive snapshot capture failed for %s" in strings,
        "station extractor capture": all(
            marker in strings
            for marker in (
                "STATION_EXTRACTOR_SNAPSHOT",
                "Station extractor capture failed",
                "equipped_module_counts",
            )
        ),
        "system body roster": all(
            marker in strings
            for marker in (
                "SYSTEM_BODY_ROSTER",
                "System body roster capture failed",
            )
        ),
        "login panel layout compatibility": _embedded_panel_layout_compatible(executable),
        "colony economy capture": all(
            marker in strings
            for marker in (
                "COLONY_ECONOMY_SNAPSHOT",
                "Colony economy capture failed",
                "tick_interval_seconds",
            )
        ),
    }
    for source in HOOKS.values():
        parts[source] = source in strings
    for marker, _expression, _anchor in SNAPSHOT_HOOKS.values():
        parts[marker] = marker in strings
    return parts


def _pyz_table_is_dict(pyz_data: bytes) -> bool:
    """Whether this archive's module table is marshalled as a dict.

    The table has to be written back in the shape the bootloader inside this
    executable expects.  Writing a list into a dict-format archive produces a
    client that fails to start -- a far worse outcome than refusing to patch.
    """
    toc_offset = struct.unpack("!i", pyz_data[8:12])[0]
    return isinstance(marshal.loads(pyz_data[toc_offset:]), dict)


def _replace_pyz_module(pyz_data: bytes, source: Path, module_name: str) -> bytes:
    entries = _pyz_entries(pyz_data)
    table_is_dict = _pyz_table_is_dict(pyz_data)
    positive_offsets = [offset for _, (_, offset, length) in entries if length > 0]
    if not positive_offsets:
        raise PatchError("Embedded PYZ archive has no data entries.")
    header_length = min(positive_offsets)
    toc_offset = struct.unpack("!i", pyz_data[8:12])[0]
    if header_length < 12 or header_length > toc_offset:
        raise PatchError(f"Invalid embedded PYZ header length: {header_length}")

    source_text = source.read_text(encoding="utf-8-sig")
    code = compile(source_text, f"{module_name}.py", "exec", dont_inherit=True, optimize=0)
    replacement = zlib.compress(marshal.dumps(code), level=6)
    output = bytearray(pyz_data[:header_length])
    new_toc = []
    replaced = False
    for name, (typecode, offset, length) in entries:
        new_offset = len(output)
        if name == module_name:
            if typecode != 0:
                raise PatchError(f"Embedded module {module_name!r} has unexpected type {typecode!r}.")
            blob = replacement
            replaced = True
        else:
            blob = pyz_data[offset : offset + length]
            if len(blob) != length:
                raise PatchError(f"Truncated embedded PYZ entry: {name!r}")
        output.extend(blob)
        new_toc.append((name, (typecode, new_offset, len(blob))))
    if not replaced:
        raise PatchError(f"Embedded module {module_name!r} was not found.")
    new_toc_offset = len(output)
    # Written back in the shape it was read in, never normalised.
    output.extend(marshal.dumps(dict(new_toc) if table_is_dict else new_toc))
    output[8:12] = struct.pack("!i", new_toc_offset)
    return bytes(output)


def _repack_client(
    executable: Path,
    sources: dict[str, Path],
    output: Path,
) -> None:
    _require_archive_support()
    reader = CArchiveReader(str(executable))
    ordered, cookie = _ordered_carchive_toc(reader)
    magic, _, _, _, pyvers, pylib_raw = cookie
    pylib_name = pylib_raw.split(b"\0", 1)[0]
    new_pyz = reader.extract("PYZ.pyz")
    for module_name, source in sources.items():
        new_pyz = _replace_pyz_module(new_pyz, source, module_name)
    archive_path = output.with_suffix(output.suffix + ".pkg.tmp")
    writer = object.__new__(CArchiveWriter)
    writer._collected_names = set()
    toc = []
    replaced = False
    try:
        with archive_path.open("wb") as archive_handle:
            for name, (_, _, _, compressed, typecode) in ordered:
                if typecode == "o":
                    blob = b""
                elif name == "PYZ.pyz":
                    blob = new_pyz
                    replaced = True
                else:
                    blob = reader.extract(name)
                toc.append(
                    writer._write_blob(
                        archive_handle,
                        blob,
                        name,
                        typecode,
                        compress=bool(compressed),
                    )
                )
            if not replaced:
                raise PatchError("Client.exe does not contain a PYZ.pyz entry.")
            toc_offset = archive_handle.tell()
            toc_data = writer._serialize_toc(toc)
            archive_handle.write(toc_data)
            archive_length = toc_offset + len(toc_data) + writer._COOKIE_LENGTH
            archive_handle.write(
                struct.pack(
                    writer._COOKIE_FORMAT,
                    magic,
                    archive_length,
                    toc_offset,
                    len(toc_data),
                    pyvers,
                    pylib_name,
                )
            )
        with executable.open("rb") as source_handle, output.open("wb") as output_handle:
            output_handle.write(source_handle.read(reader._start_offset))
            with archive_path.open("rb") as archive_handle:
                shutil.copyfileobj(archive_handle, output_handle, length=1024 * 1024)
        os.chmod(output, executable.stat().st_mode)
    finally:
        if archive_path.exists():
            archive_path.unlink()


def _read_source(path: Path) -> tuple[str, str, str]:
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(encoding)
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text, encoding, newline


def _part_presence(text: str) -> dict[str, bool]:
    parts = {
        "shop helper": all(
            marker in text
            for marker in (
                "def _log_shop_catalogue(",
                'logger.info("SHOP_CATALOG %s", encoded)',
                '"SHOP_CATALOG_SUMMARY source=%s station_id=%s "',
                'logger.exception("SHOP_CATALOG capture failed for %s", source)',
                '                            "system_name": system_name,',
            )
        ),
        "training catalogue": all(
            marker in text
            for marker in (
                'training_inventory = data.get("training_inventory")',
                'logger.info("TRAINING_CATALOG %s", encoded)',
                '"TRAINING_CATALOG_SUMMARY source=%s station_id=%s "',
                '"offer": offer,',
            )
        ),
        "planet scan": '"PLANET_SCAN_RESULT %s"' in text,
        "system body roster": all(
            marker in text
            for marker in (
                "def _log_system_body_roster(",
                '"SYSTEM_BODY_ROSTER"',
                "self._log_system_body_roster(self._solar_layout)",
                'logger.exception("System body roster capture failed")',
            )
        ),
        "snapshot helper": all(
            marker in text
            for marker in (
                "def _log_archive_snapshot(",
                'logger.info("%s %s", marker, encoded)',
                'logger.exception("Archive snapshot capture failed for %s", marker)',
            )
        ),
        "station extractor capture": all(
            marker in text
            for marker in (
                "def _log_station_extractor_snapshot(",
                '"STATION_EXTRACTOR_SNAPSHOT"',
                'logger.exception("Station extractor capture failed")',
                "self._sec_station_extractor_context = {",
                "self._log_station_extractor_snapshot(data)",
            )
        ),
        "colony economy capture": all(
            marker in text
            for marker in (
                "def _log_colony_economy_snapshot(",
                '"COLONY_ECONOMY_SNAPSHOT"',
                'logger.exception("Colony economy capture failed")',
                "self._log_colony_economy_snapshot(data)",
            )
        ),
    }
    for method, source in HOOKS.items():
        parts[source] = f'self._log_shop_catalogue("{source}", data)' in text
    for method, (marker, expression, _anchor) in SNAPSHOT_HOOKS.items():
        parts[marker] = f'self._log_archive_snapshot("{marker}", {expression})' in text
    return parts


def inspect_text(text: str, target: Path = Path("protocol.py")) -> PatchInspection:
    if "class ProtocolMixin:" not in text:
        return PatchInspection("incompatible", "ProtocolMixin class was not found.", target)
    if not re.search(r"(?m)^import json(?:\s|$)|^from json import ", text):
        return PatchInspection("incompatible", "The protocol module no longer imports json.", target)

    missing_methods = [name for name in REQUIRED_METHODS if not re.search(rf"(?m)^    def {re.escape(name)}\(", text)]
    if missing_methods:
        return PatchInspection(
            "incompatible",
            "Required game methods changed or are missing: " + ", ".join(missing_methods),
            target,
        )

    helper_exists = "def _log_shop_catalogue(" in text
    helper_complete = _part_presence(text)["shop helper"]
    if helper_exists and not helper_complete:
        familiar_legacy_helper = all(
            marker in text
            for marker in (
                'logger.info("SHOP_CATALOG %s", encoded)',
                '"SHOP_CATALOG_SUMMARY source=%s station_id=%s "',
                'logger.exception("SHOP_CATALOG capture failed for %s", source)',
            )
        )
        if not familiar_legacy_helper:
            return PatchInspection(
                "incompatible",
                "An unfamiliar partial shop logger already exists; automatic repair was refused.",
                target,
            )

    snapshot_helper_exists = "def _log_archive_snapshot(" in text
    snapshot_helper_complete = _part_presence(text)["snapshot helper"]
    if snapshot_helper_exists and not snapshot_helper_complete:
        return PatchInspection(
            "incompatible",
            "An unfamiliar partial snapshot logger already exists; automatic repair was refused.",
            target,
        )

    extractor_helper_exists = "def _log_station_extractor_snapshot(" in text
    extractor_capture_complete = _part_presence(text)["station extractor capture"]
    if extractor_helper_exists and not extractor_capture_complete:
        return PatchInspection(
            "incompatible",
            "An unfamiliar partial station extractor logger already exists; automatic repair was refused.",
            target,
        )

    colony_helper_exists = "def _log_colony_economy_snapshot(" in text
    colony_capture_complete = _part_presence(text)["colony economy capture"]
    if colony_helper_exists and not colony_capture_complete:
        return PatchInspection(
            "incompatible",
            "An unfamiliar partial colony economy logger already exists; automatic repair was refused.",
            target,
        )
    parts = _part_presence(text)
    installed = tuple(name for name, present in parts.items() if present)
    missing = tuple(name for name, present in parts.items() if not present)
    if not missing:
        return PatchInspection(
            "installed",
            f"Archive logger v{PATCH_VERSION} is installed and complete.",
            target,
            installed,
            missing,
        )
    return PatchInspection(
        "repairable",
        "The game layout is compatible. Missing hooks: " + ", ".join(missing),
        target,
        installed,
        missing,
    )


def inspect_protocol(game_root: Path) -> PatchInspection:
    source = protocol_path(game_root)
    target = client_path(game_root)
    if not source.is_file():
        return PatchInspection("missing", f"Game protocol source was not found: {source}", source)
    try:
        text, _encoding, _newline = _read_source(source)
    except (OSError, UnicodeError) as error:
        return PatchInspection("incompatible", f"Could not read the game protocol source: {error}", source)
    source_inspection = inspect_text(text, source)
    if not target.is_file():
        return source_inspection
    if CArchiveReader is None or CArchiveWriter is None:
        return PatchInspection(
            "incompatible",
            "This archive build cannot inspect the embedded Client.exe protocol.",
            target,
        )
    try:
        parts = _embedded_part_presence(target)
    except (OSError, ValueError, KeyError, PatchError) as error:
        return PatchInspection(
            "incompatible",
            f"Could not inspect the embedded Client.exe protocol: {error}",
            target,
        )
    installed = tuple(name for name, present in parts.items() if present)
    missing = tuple(name for name, present in parts.items() if not present)
    if not missing:
        return PatchInspection(
            "installed",
            f"Archive logger v{PATCH_VERSION} is embedded in Client.exe and complete.",
            target,
            installed,
            missing,
        )
    if source_inspection.state not in {"installed", "repairable"}:
        return PatchInspection(
            "incompatible",
            "Client.exe needs repair, but the matching protocol source is incompatible: "
            + source_inspection.message,
            target,
            installed,
            missing,
        )
    return PatchInspection(
        "repairable",
        "The embedded Client.exe integration needs repair. Missing hooks: " + ", ".join(missing),
        target,
        installed,
        missing,
    )


def _method_bounds(text: str, method: str) -> tuple[int, int]:
    match = re.search(rf"(?m)^    def {re.escape(method)}\(", text)
    if not match:
        raise PatchError(f"Required method was not found: {method}")
    next_match = re.search(r"(?m)^    def [A-Za-z_][A-Za-z0-9_]*\(", text[match.end() :])
    end = match.end() + next_match.start() if next_match else len(text)
    return match.start(), end


def _insert_before_in_method(text: str, method: str, anchor: str, block: str) -> str:
    start, end = _method_bounds(text, method)
    segment = text[start:end]
    relative = segment.find(anchor)
    if relative < 0:
        raise PatchError(f"Safe insertion point changed inside {method}.")
    index = start + relative
    return text[:index] + block + text[index:]


def patch_protocol_text(text: str) -> str:
    inspection = inspect_text(text)
    if inspection.state == "installed":
        return text
    if not inspection.can_repair:
        raise PatchError(inspection.message)

    candidate = text
    parts = _part_presence(candidate)
    if not parts["shop helper"] or not parts["training catalogue"]:
        if "def _log_shop_catalogue(" in candidate:
            start, end = _method_bounds(candidate, "_log_shop_catalogue")
            candidate = candidate[:start] + SHOP_HELPER + candidate[end:]
        else:
            class_anchor = "class ProtocolMixin:"
            class_index = candidate.index(class_anchor) + len(class_anchor)
            line_end = candidate.find("\n", class_index)
            if line_end < 0:
                raise PatchError("ProtocolMixin class body could not be located safely.")
            candidate = candidate[: line_end + 1] + SHOP_HELPER + candidate[line_end + 1 :]

    if not parts["snapshot helper"]:
        class_anchor = "class ProtocolMixin:"
        class_index = candidate.index(class_anchor) + len(class_anchor)
        line_end = candidate.find("\n", class_index)
        if line_end < 0:
            raise PatchError("ProtocolMixin class body could not be located safely.")
        candidate = candidate[: line_end + 1] + SNAPSHOT_HELPER + candidate[line_end + 1 :]

    if not parts["system body roster"]:
        class_anchor = "class ProtocolMixin:"
        class_index = candidate.index(class_anchor) + len(class_anchor)
        line_end = candidate.find("\n", class_index)
        if line_end < 0:
            raise PatchError("ProtocolMixin class body could not be located safely.")
        candidate = candidate[: line_end + 1] + SYSTEM_BODY_ROSTER_HELPER + candidate[line_end + 1 :]
        candidate = _insert_before_in_method(
            candidate,
            "_on_solar_system_data",
            "        # Raw on-the-wire size of this layout, in bytes.",
            SYSTEM_BODY_ROSTER_BLOCK,
        )

    if not parts["station extractor capture"]:
        class_anchor = "class ProtocolMixin:"
        class_index = candidate.index(class_anchor) + len(class_anchor)
        line_end = candidate.find("\n", class_index)
        if line_end < 0:
            raise PatchError("ProtocolMixin class body could not be located safely.")
        candidate = candidate[: line_end + 1] + STATION_EXTRACTOR_HELPER + candidate[line_end + 1 :]
        candidate = _insert_before_in_method(
            candidate,
            "_on_player_station_dock_ok",
            "        if self._solar_window is not None:",
            STATION_DOCK_CONTEXT_BLOCK,
        )
        candidate = _insert_before_in_method(
            candidate,
            "_on_ps_station_cargo",
            "        if self._solar_window is not None:",
            STATION_EXTRACTOR_SNAPSHOT_BLOCK,
        )

    if not parts["colony economy capture"]:
        class_anchor = "class ProtocolMixin:"
        class_index = candidate.index(class_anchor) + len(class_anchor)
        line_end = candidate.find("\n", class_index)
        if line_end < 0:
            raise PatchError("ProtocolMixin class body could not be located safely.")
        candidate = candidate[: line_end + 1] + COLONY_ECONOMY_HELPER + candidate[line_end + 1 :]
        candidate = _insert_before_in_method(
            candidate,
            "_on_colony_data",
            "        if self._solar_window is not None:",
            COLONY_ECONOMY_SNAPSHOT_BLOCK,
        )
    if not parts["planet scan"]:
        candidate = _insert_before_in_method(
            candidate,
            "_on_planet_scan_result",
            "        if self._solar_window is None:",
            SCAN_BLOCK,
        )

    for method, source in HOOKS.items():
        call = f'        self._log_shop_catalogue("{source}", data)\n'
        if call.strip() in candidate:
            continue
        candidate = _insert_before_in_method(
            candidate,
            method,
            "        if self._solar_window is not None:",
            call,
        )

    for method, (marker, expression, anchor) in SNAPSHOT_HOOKS.items():
        call = f'        self._log_archive_snapshot("{marker}", {expression})\n'
        if call.strip() in candidate:
            continue
        candidate = _insert_before_in_method(candidate, method, anchor, call)

    compile(candidate, "protocol.py", "exec")
    final = inspect_text(candidate)
    if final.state != "installed":
        raise PatchError("The candidate did not contain every required archive hook.")
    return candidate




def _restore_backups(pairs: list[tuple[Path | None, Path]]) -> list[str]:
    """Copy every backup back, and keep going when one of them fails.

    The rollback used to be a straight run of shutil.copy2 calls that stopped
    at the first exception -- and the file most likely to fail is Client.exe,
    locked by a running game, which was also the first one it tried.  So a
    deploy refused for the ordinary reason left the loose sources newly patched
    beside a stale executable, with nothing reporting the difference.

    Returns what could not be restored, so the caller can say so instead of
    claiming a clean rollback it did not achieve.
    """
    failures: list[str] = []
    for backup, destination in pairs:
        if backup is None or not backup.exists():
            continue
        try:
            shutil.copy2(backup, destination)
        except OSError as error:
            failures.append(f"{destination.name} ({error})")
    return failures


def _unique_backup_path(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f"{target.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate


def apply_protocol_patch(game_root: Path) -> PatchResult:
    source = protocol_path(game_root)
    target = client_path(game_root)
    inspection = inspect_protocol(game_root)
    if inspection.state == "installed":
        return PatchResult(False, inspection.target, None, inspection.message)
    if inspection.state != "installed" and not inspection.can_repair:
        raise PatchError(inspection.message)

    original, encoding, newline = _read_source(source)
    normalised = original.replace("\r\n", "\n")
    candidate = patch_protocol_text(normalised)
    candidate = candidate.replace("\n", newline)
    compile(candidate, str(source), "exec")
    if not target.is_file():
        backup = _unique_backup_path(source)
        shutil.copy2(source, backup)
        temporary = source.with_name(
            f".{source.name}.star-empire-archive-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(candidate.encode(encoding))
            os.chmod(temporary, source.stat().st_mode)
            os.replace(temporary, source)
            verified = inspect_protocol(game_root)
            if verified.state != "installed":
                shutil.copy2(backup, source)
                raise PatchError("Verification failed after replacement; the backup was restored.")
        except Exception:
            temporary.unlink(missing_ok=True)
            if backup.exists():
                shutil.copy2(backup, source)
            raise
        return PatchResult(
            True,
            source,
            backup,
            f"Archive logger v{PATCH_VERSION} source was installed and verified.",
            None,
        )

    source_changed = candidate != original
    backup = _unique_backup_path(target)
    source_backup = _unique_backup_path(source) if source_changed else None
    temporary_source = source.with_name(
        f".{source.name}.star-empire-source-{uuid.uuid4().hex}.tmp")
    temporary_executable = target.with_name(
        f".{target.name}.star-empire-archive-{uuid.uuid4().hex}.tmp")
    embedded_candidate = _adapt_protocol_for_embedded_client(candidate, target)
    temporary_source.write_bytes(embedded_candidate.encode(encoding))
    os.chmod(temporary_source, source.stat().st_mode)
    _repack_client(target, {"protocol": temporary_source}, temporary_executable)
    candidate_parts = _embedded_part_presence(temporary_executable)
    if not all(candidate_parts.values()):
        temporary_source.unlink(missing_ok=True)
        temporary_executable.unlink(missing_ok=True)
        missing = [name for name, present in candidate_parts.items() if not present]
        raise PatchError("Repacked Client.exe failed verification; missing: " + ", ".join(missing))

    if source_changed:
        temporary_source.write_bytes(candidate.encode(encoding))
        os.chmod(temporary_source, source.stat().st_mode)

    shutil.copy2(target, backup)
    if source_backup is not None:
        shutil.copy2(source, source_backup)
    restore_pairs: list[tuple[Path | None, Path]] = [
        (backup, target),
        (source_backup, source),
    ]
    try:
        os.replace(temporary_executable, target)
        if source_changed:
            os.replace(temporary_source, source)
        else:
            temporary_source.unlink(missing_ok=True)
        verified = inspect_protocol(game_root)
        if verified.state != "installed":
            failures = _restore_backups(restore_pairs)
            raise PatchError(
                "Verification failed after replacement; the backup was restored."
                if not failures else
                "Verification failed after replacement, and the rollback could "
                "not restore: " + ", ".join(failures))
    except Exception:
        temporary_source.unlink(missing_ok=True)
        temporary_executable.unlink(missing_ok=True)
        _restore_backups(restore_pairs)
        raise

    return PatchResult(
        True,
        target,
        backup,
        f"Archive logger v{PATCH_VERSION} was embedded in Client.exe and verified.",
        source_backup,
    )
