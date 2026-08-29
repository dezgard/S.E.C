# Star Empire Companion (S.E.C.)

Star Empire Companion is a native Windows desktop archive for player-observed Star Empire data.

## Public boundary

This source tree is intentionally separate from the private `StarEmpireDataBrowser` project next to it.

- It may use a **passive, logger-only Game Link** to record data the normal client already receives.
- It must never automate gameplay, send player commands, or create an in-game control panel.
- It contains no dungeon planner, dungeon runner, dungeon AI view, combat profile, or saved dungeon plan.
- It contains no in-game visual effects, including damage numbers, shield effects, turret overlays, or hardpoint displays.
- It remains a native desktop application; it does not start a web browser or web server.

The private project is retained unchanged as rollback material. S.E.C. v1.5.5.0
is published as the public v0.6 release. It has not been deployed to a game
folder.

## Run or build locally

Use `start.cmd` to run the native desktop application from Python. It requires
Pillow in the active Python environment. `build_exe.cmd` creates a one-file
`StarEmpireCompanion.exe` in its local `releases\current` folder and backs up
an existing executable first. It also creates the matching
`StarEmpireCompanion.exe.sha256` release asset. Neither script is run
automatically.

## Updates

The packaged application has a manual **CHECK UPDATE** button. It checks only
the latest release in `dezgard/S.E.C`; nothing is downloaded automatically.
When a newer public tag is available, it requires confirmation, downloads the
release executable and its matching SHA-256 asset, and installs only after the
checksum and published size match. The current Companion is replaced only after
it closes, then restarts. Source/Python runs and all game files are excluded.

## Universal search

The **Items** scope accepts numeric comparisons for every numeric column in
the item table. For example, `speed>=10` reads the same **Speed**, **Max
Speed**, or **Ship Speed** stat displayed in the item list. The matching
station-item search uses the same comparisons.

## Map overview

System names remain available at twice the former zoom-out distance. The map
shows only systems with a recorded jump connection, while preserving all other
observations in the archive. Its toggleable coalition-control layer now uses
the authoritative territory assignments already received by the game. It
reproduces the game's clipped territory cells with each coalition's real name
and public colour. Each coalition name is one ordinary, normally spaced line,
centred on its territory anchor and horizontal whenever that remains readable.
If exact containment would otherwise make the text too small, the complete line
may angle along the territory's central direction to gain size; the baseline
never curves and letters are never rotated separately. Coalition names stay
enabled at every zoom level. Every rendered pixel, including the dark outline,
must fit inside that coalition region; regions too small for a complete readable
name remain unlabelled until the player zooms closer. Disconnected systems remain
hidden as nodes but still act as invisible boundary-clipping sites.

Large-map interaction reuses rendered coalition-name rasters and checks exact
containment only across each label's local footprint. Rapid wheel events are
combined into the next redraw, while dragging moves the existing canvas
immediately and performs one complete refresh on release. This keeps the map
responsive without changing its data, labels, boundaries, or click targets.

## Extractor slots

When the player docks at a managed extraction base, the passive Game Link may
record only that base's already-received resource-extractor module counts.
Map Intelligence now gives a detailed selected-system extraction panel: scanned
body, planet, moon, and maximum-base counts; each resource's known locally
observed use / maximum build capacity; and the retained observed tier mix such
as `100× T3 · 200× T6 · 90× T9`. Planets allow three bases and moons allow one.
The system-yield table keeps the one-base aggregate first and places its
moon-aware maximum in parentheses—for example `Metal Ore: 498 (1,494)` when
all contributing bodies are planets. “Not locally observed as used” is not a
claim that a slot is empty: used counts and tiers include only bases the player
has docked at. Those private observations are stored locally and never enter a
shared `.secintel.json` bundle; older aggregate-only records remain totals-only
rather than having tiers guessed from their count.
