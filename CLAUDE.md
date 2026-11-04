# CLAUDE.md

Project: isometric pixel-art map of a real city (pilot: Södermalm, Stockholm).
The full plan is in `PLAN.md` — read it first and work phase by phase.

**Always keep `PLAN.md` up to date**, in the same change as the work itself:
- tick off checkboxes as tasks are completed;
- record decisions made (short "→ ..." note under the task) and open questions;
- add new tasks, risks or changes of approach as they come up; remove ones that no longer apply;
- keep the "Status" section at the top current (done / next / blocked on).

## Layout

- `config/` — area boundary (GeoJSON), `area.yaml` with camera angle, scale, tile size
- `render/` — three.js isometric renderer (Node/TypeScript, run headless via Playwright)
- `pipeline/` — Python: tile planning, image generation, database, stitching, pyramid
- `review/` — small local web app to review/approve/regenerate tiles
- `viewer/` — the public web viewer (OpenSeadragon or Leaflet)
- `style/` — reference images and prompt versions
- `data/` — renders, training pairs, generated quadrants, `map.db` (git-ignored)
- `dist/` — final tile pyramid for hosting (git-ignored)

## Conventions

- Python 3.11+, managed with `uv`. Node 20+ for `render/` and `viewer/`.
- All scripts take explicit arguments (paths, area name); no hard-coded paths.
- Generation must be resumable: state lives in `data/map.db`, never only in memory.
- Secrets in `.env` (see `.env.example`); never commit them.
- Pixel art must stay pixel art: nearest-neighbour scaling only, no smoothing, palette-snap outputs.
- Geometry must line up between tiles: tile positions are computed in isometric screen space
  by `pipeline/plan_tiles.py` — do not compute them anywhere else.

## Before scaling up

Always test changes on a 3×3 block of tiles and check the stitched result for seams
before running a large batch.
