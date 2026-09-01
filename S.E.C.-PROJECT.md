# Star Empire Companion (S.E.C.)

This folder is the canonical public working copy of **Star Empire Companion**.
It is a native Windows desktop archive for data the player has already
observed in Star Empire.

## Public safety boundary

- The optional Game Link is passive and logger-only: it records normal client
  payloads that have already been received.
- It never automates gameplay, sends player commands, purchases or sells,
  or adds an in-game menu, panel, or visual effect.
- The tool contains no dungeon planner, dungeon AI, dungeon run plan, combat
  profile, or saved automation state.
- It runs as a native desktop app only. It does not start a browser or a web
  server.
- Do not modify game files or deploy the Game Link unless Dezgard explicitly
  authorizes that separate action.

## Key paths

| Purpose | Location |
| --- | --- |
| Public source and build root | This folder |
| Desktop entry point | `launcher.py` / `start.cmd` |
| Passive Game Link source | `game_link.py` |
| Release executable | `releases\current\StarEmpireCompanion.exe` |
| Build output | `builds\current\StarEmpireCompanion.exe` |
| Rollback source retained during the move | `..\StarEmpireDataBrowser\public` |
| Local user archive | `%LOCALAPPDATA%\StarEmpireCompanion\archive.json` |
| Local preferences and annotations | `%LOCALAPPDATA%\StarEmpireCompanion\user_data.json` |

## v0.19 release highlights

- Adds a top-level **System Extraction** page. Right-click a Galaxy Map system to save it locally, then select it from the left-hand list to review the recorded detail without closing the map.
- The saved-system detail now lists every rostered planet and moon. Named docked bases are nested beneath their authoritative planet ID/name, show their extractor tier mix, and report current raw extractor output per 2-hour Colony tick. Generic `Dezgard's Station` placeholders and jump-link clutter are omitted.

- Adds a local per-base **Observed Extractor Output / Colony Tick** panel.
  Output comes from each logged extractor module's own catalogued `Production`
  value and the server-supplied Colony tick interval; it does not assume a
  rate from T3/T6/T9 labels alone.
- The optional passive Game Link records only an already-delivered normal
  Colony-tab response: station/system/planet context, current population,
  tick interval, and baseline basket resource/per-capita values. It sends no
  command and excludes prices, reserves, cargo, food, and other colony data.
- Direct-extraction colony support is the minimum of
  `extractor output per tick / basket per-capita demand`. If any basket
  resource has no direct observed output, S.E.C. reports the estimate as
  incomplete rather than inventing processor, logistics, market, or reserve
  assumptions.
- This data is private local archive data and is excluded from `.secintel.json`
  sharing. No game folder was modified or deployed for this source change.
## Current public version

| Version | Date | Status | Notes |
| --- | --- | --- | --- |
| 0.19 | 2026-09-01 | Published on GitHub; not deployed to a game folder | Adds the top-level saved System Extraction workspace and right-click map workflow. Every rostered planet/moon is visible, including unscanned bodies; properly named docked bases are attached to their recorded planet and show extractor tiers plus current raw extraction per 2-hour Colony tick. Generic station placeholders and jump-connection clutter are removed. This release also contains the public passive logger, unscanned body roster, full per-body resource matrix, coalition map, sharing safeguards, and verified updater handoff work that existed locally after v0.18. Verified by 65 tests, a Tk startup smoke check, and matching EXE/checksum data. GitHub tag: `v0.19`; Windows package metadata: `0.19.0.0`. |
| 0.18 | 2026-08-30 | Published on GitHub; not deployed to a game folder | Removes the Planetary Scan Archive's old Best Resources summary column and preset. Any saved archive layout containing that retired column is automatically replaced at startup with the complete resource matrix: Metal Ore, Silicon, Copper, Gold, Oil, Promethium, Space Corn, Titanium, and Wood. Scanned zero yields remain visible as `0`; unscanned values stay unknown; Export Resource CSV keeps the filtered planet/moon rows in that same order. Verified by 55 public tests, matching GitHub assets/checksum data, and the live release-description check. GitHub tag: `v0.18`; Windows package metadata: `0.18.0.0`. |
| 0.17 | 2026-08-30 | Published on GitHub; not deployed to a game folder | Makes the Planetary Scan Archive's standard view a complete per-body resource matrix: Metal Ore, Silicon, Copper, Gold, Oil, Promethium, Space Corn, Titanium, and Wood all retain their own columns. Scanned zero yields remain visible as `0`, while unscanned bodies remain unknown. The filtered planet/moon rows can be exported in the same order through Export Resource CSV; the compact Best Resources summary remains an optional Overview preset and the redundant Recommended Extractors panel was removed. Verified by 55 public tests, matching GitHub assets/checksum data, and release-description checks. GitHub tag: `v0.17`; Windows package metadata: `0.17.0.0`. |
| 0.16 | 2026-08-30 | Published on GitHub; not deployed to a game folder | Fixes the verified automatic-update handoff: after checksum validation and confirmed install, S.E.C. closes itself, swaps the actual running packaged EXE, and reopens it. The updater keeps its verified SHA-256, rollback, renamed-EXE, and elevation-on-access-denied safeguards. System Resource Yields now retains roster-only systems as `UNSCANNED`; partial systems preserve only known yields and identify remaining unscanned bodies. Verified by 54 public tests and a disposable end-to-end helper replacement/relaunch check. GitHub tag: `v0.16`; Windows package metadata: `0.16.0.0`. |
| 0.15 | 2026-08-30 | Published on GitHub; not deployed to a game folder | Adds a passive normal-system body roster. Entering a system records only the client-delivered planet/moon ID, name, type, moon flag, system, and time; the Planetary Scan Archive shows those rows as UNSCANNED until an ordinary in-game scan supplies real data. Roster observations are safely included in opt-in community bundles, while resource fields are stripped from unscanned imports and a true scan cannot be overwritten by a later roster sighting. Verified by 51 focused public tests, a dry-run upgrade of the current Game Link source, and matching published asset/checksum data; no game files changed. GitHub tag: `v0.15`; Windows package metadata: `0.15.0.0`. |
| 1.5.5.0 | 2026-08-29 | Built, ready to publish as v0.6 | Adds a manual Companion updater. CHECK UPDATE reads only the latest `dezgard/S.E.C` GitHub release, requires both `StarEmpireCompanion.exe` and its SHA-256 asset, validates size and hash before staging, then replaces the packaged Companion after exit and restarts it. It never runs for source/Python launches, downloads nothing without confirmation, and never touches game files. The build now produces the required `.sha256` asset. Verified by 45 public tests. |
| 1.5.4.0 | 2026-08-29 | Built, not deployed | Makes the draggable Map Intelligence detail panel resizable from its bottom-right corner. Its size stays within the map, is saved locally with the map view, and RESET PANELS returns it to the default. Verified by 42 public tests. |
| 1.5.3.0 | 2026-08-29 | Built, not deployed | Adds detailed moon-aware system extraction capacity. Non-moon planets permit three bases; game-marked moons permit one. Map Intelligence now shows selected-system scanned-body capacity, known local used slots, safely worded not-locally-observed-as-used remainder, and observed T3/T6/T9 mix. System Resource Yields retains the raw one-base total and appends its moon-aware build maximum—for example `498 (1,494)` when every contributing body is a planet. Local usage and tiers remain excluded from sharing. Verified by 42 public tests. |
| 1.5.2.0 | 2026-08-29 | Built, not deployed | Adds the local observed extractor production-tier mix beneath each Map Intelligence used / possible resource line: base, advanced, and industrial module families display as T3, T6, and T9—for example `100× T3 · 200× T6 · 90× T9`. It derives only from retained docked-base module types, aggregates across observed bases in the selected system, never guesses legacy aggregate-only records, and remains excluded from sharing. Verified by 40 public tests. |
| 1.5.1.0 | 2026-08-29 | Built, not deployed | Fixes Universal Intel Search numeric predicates for every numeric item-table column, including `speed>=10`. Speed comparisons now use the displayed Speed / Max Speed / Ship Speed stat in both the Items scope and station-item searches. Verified against the current 708-item local catalogue (`speed>=10` returns 48 items) and the 38-test public suite. |
| 1.5.0.0 | 2026-08-29 | Built, not deployed | Adds private, passive docked-extraction-base observations. Map Intelligence now presents each scanned resource as used / possible extractor slots, such as Metal Ore 390 / 590 from 100 base, 200 advanced, and 90 industrial drills. Only recognised resource-extractor counts plus station/system/planet context are retained locally; cargo, credits, weapons, and other station state are excluded. Community bundles remain whitelist-sanitised and do not contain this private data. |
| 1.4.7.0 | 2026-08-29 | Built, not deployed | Removes the primary large-map redraw bottleneck without changing map output. Coalition-name fonts and whole-line rasters are reused, exact containment checks only the local label footprint, rapid wheel events coalesce before one redraw, and panning moves existing canvas items until one release refresh. On the real 1,150-system archive, the warm coalition overview redraw fell from about 257 ms to 40.5 ms (about 84% faster). |
| 1.4.6.0 | 2026-08-29 | Built, not deployed | Keeps coalition names horizontal whenever they fit at a readable size, but allows the entire normally kerned line to angle along the territory's central direction when a horizontal fit would become too small. The angle is capped for left-to-right readability; names remain centred, uncurved, fully contained in the exact region mask, and available throughout the zoom range. |
| 1.4.5.0 | 2026-08-29 | Built, not deployed | Removes curved coalition-name rendering from the map presentation. Names are now normal straight horizontal text centred on each territory anchor, while progressive font fitting, exact region containment, full-name rendering, and visibility throughout the zoom range remain enforced. |
| 1.4.4.0 | 2026-08-29 | Built, not deployed | Replaces rigid word-block rotations with one continuous full-name bend. The centreline is rounded without moving its territory midpoint, character spacing preserves the font's complete kerning, and neighbouring tangents blend into a gentle readable curve while the exact containment mask and all-zoom visibility remain enforced. |
| 1.4.3.0 | 2026-08-29 | Built, not deployed | Keeps letters tethered into normally kerned word runs while retaining a restrained bend between words. Coalition names are no longer hidden at detail zoom; the exact territory mask remains authoritative, so zooming in can reveal a complete label but never make an already visible label disappear merely because of zoom level. |
| 1.4.2.0 | 2026-08-29 | Built, not deployed | Constrains every curved coalition label to the exact union of its authoritative territory cells. The renderer tries progressively smaller condensed type, accepts a label only when its complete glyph-and-outline raster is inside the region mask, and omits tiny regions until zoom provides enough room rather than clipping, abbreviating, or crossing a border. |
| 1.4.1.0 | 2026-08-29 | Built, not deployed | Replaces flat coalition names with complete per-glyph labels that follow a restrained territory-derived centreline. Curves retain left-to-right readability, cap steep angles, and independently balance both arms so every text midpoint remains exactly on its territory anchor, including narrow and asymmetric regions. |
| 1.4.0.0 | 2026-08-29 | Built, not deployed | Replaces the incorrect ownership/jump-linked purple approximation with the authoritative `GALAXY_STATIC.territory` map and game-equivalent clipped Voronoi/envelope geometry. Uses real coalition names/colours, renders 1,150 unique connected nodes, retains all 2,007 positions as invisible clipping sites, deduplicates archived systems by canonical name, and safely shares territory without letting a partial community file overwrite a local server snapshot. |
| 1.3.0.0 | 2026-08-29 | Built, not deployed | Replaces the faint coalition overlay with clear solid observed-control blobs: jump-linked coalition clusters use outlined hulls/capsules, while single observed systems retain a circular marker. The map now hides systems with no recorded jump connection without deleting their archive data. |
| 1.2.0.0 | 2026-08-29 | Built, not deployed | Galaxy-map overview update: system labels remain visible at 2x the former zoom-out distance, and a toggleable, evidence-based coalition-control layer marks recorded coalition-owned systems and their jump-linked areas. Shared map bundles retain public control context but exclude personal/mine station data. |
| 1.1.0.0 | 2026-08-29 | Built, not deployed | Adds opt-in `.secintel.json` community intel export/import. No automatic upload or server. Export excludes player, ship, inventory, local notes, and personal station status. |
| 1.0.0.0 | 2026-08-29 | Built, not deployed | First S.E.C. public desktop build. Passive logger-only Game Link; no game-folder copy has been made. |

Current published release verification (v0.19):

- GitHub tag: `v0.19`
- Release asset source: `builds\candidate-20260901-221815\StarEmpireCompanion.exe`
- Size: 23,995,557 bytes
- SHA-256: `B03459A0CBF993337A43AEEBD213947741E7CB9ACB901A5572167BEC44BCC5A1`
- Checksum asset: `StarEmpireCompanion.exe.sha256` (same SHA-256)
- Windows product/file version: `0.19.0.0`

## Build and verification

`build_exe.cmd` builds from `StarEmpireCompanion.spec`. It creates a new
single-file executable in `builds\current`, backs up an existing public
release before replacement, then copies the verified result to
`releases\current`.

Before any version is marked ready, record the result here:

1. Confirm the source scan contains no automation, dungeon, in-game menu, or
   visual-effect code.
2. Run focused logger/archive checks and confirm public modules import.
3. Build with the public spec and confirm the release hash matches the build
   output.
4. Confirm executable metadata, release filename, this history table, and any
   published version all use the same version number.
5. Do not deploy to a game folder or publish a release without separate,
   explicit approval.
6. Before and after creating or editing a GitHub release, retrieve its live
   description and check the rendered copy: title, bullets, actual line breaks
   (never literal `\n`), public-safe wording, tag, and asset names must all be
   correct.

## Sharing community intel

`SHARE INTEL` provides opt-in export and import of a portable
`.secintel.json` file. It is intentionally serverless in v1.1.0.0: users
choose when and where to share the file, for example through Discord.

Every export and import is sanitised to include only community-safe map,
scan, station, item-market, and training-offer observations. Public map
ownership, authoritative territory names/colours/coordinates, and
coalition/other-station totals are retained so shared maps keep their control
context. A shared file may supply territory when no local static snapshot is
available, but cannot replace an existing local server snapshot. It excludes
player, ship, inventory, local notes,
account data, personal/mine station status and IDs, and unrecognised fields.
An edited bundle is sanitised again before merge. Normal-system body rosters
are included: system, body ID/name/type, moon flag, scan status, and observation
time. An unscanned roster row is never allowed to contain resource or extractor
data, and it can never overwrite a real scan.

The current first-phase verification is `test_sharing.py`, covering export
privacy exclusions, defensive import sanitisation, preservation of local
player state, and rejection of incompatible files.

## Galaxy overview and coalition control

With **SHOW SYSTEM NAMES** on, the map displays labels once the measured
system spacing reaches 35 screen pixels—exactly twice as far zoomed out as
the former 70-pixel threshold. A screen-grid limit keeps labels distributed
across the view rather than turning a dense sector into a text block.

**SHOW COALITION CONTROL** is on by default. It uses the authoritative public
`GALAXY_STATIC.territory` assignment for each claimed system: coalition ID,
coalition name, and public colour. Territory cells are calculated against all
known system positions, clipped to the same locally padded coalition envelope
as the game, and omit shared internal borders between adjacent claims by the
same coalition. Strategic zoom displays one real coalition name per visually
connected region. The setting is local and can be toggled off.

Each strategic-region name is rendered as one normally kerned line centred on
the territory anchor. It stays horizontal whenever that fit remains readable.
For a narrow or diagonal shape, the whole intact line may rotate along the
central direction of the territory only when doing so permits a larger font;
the readable angle is capped at 55 degrees, the baseline never curves, and
letters are never rotated separately. A per-region mask is built from the exact
authoritative cells, and the complete name plus outline is accepted only when
every visible pixel remains inside that mask. Type is progressively reduced
before a too-small region is left unlabelled; no partial name or abbreviation
is drawn. The disabled label raster cannot intercept map clicks or panning and
remains available throughout the map's zoom range whenever coalition control
is enabled.

The label renderer caches its immutable whole-name rasters and compares each
candidate only with the matching local crop of the authoritative mask instead
of allocating and scanning a canvas-sized candidate. Wheel inputs update the
target view immediately but share one short scheduled redraw. During a drag,
all existing `map-world` items follow the pointer directly and the complete
culled view is rebuilt once on release. The reference 1,150-system archive
measured about 40.5 ms per warm overview redraw after this change, down from
about 257 ms with the same coalition layer enabled.

Only positioned systems with at least one recorded jump link are rendered on
the map. Systems without a recorded connection remain safely stored and
searchable in the archive; they are simply not drawn as nodes. All 2,007
static coordinates still participate invisibly in territory clipping so
removing disconnected nodes cannot distort coalition boundaries.

## Updating S.E.C.

For every change:

1. Keep changes inside this public folder and preserve unrelated files.
2. Create a dated backup in `backups` before changing an existing fragile or
   release file.
3. Increase the single public version number consistently.
4. Add a new row to **Current public version** with the change, checks, and
   deployment state.
5. Rebuild and record the new release file size and SHA-256.

Never copy private Auto Explorer, dungeon, combat-profile, in-game panel, or
visual-effect code into this folder.
