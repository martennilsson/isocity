# Handoff — 2026-11-06 (late evening)

State of the work at the end of a session, for whoever picks it up next. `PLAN.md` is the full
plan and history; this file is the short version of where things stand.

## Where we are

- **The whole pilot map exists** and is zoomable in the browser: Södermalm, 7936×3328 px at
  0.25 m/px (user: keep this resolution for now; scaling up later is kept possible, see PLAN.md
  "Resolution").
- **Style:** Nano Banana Pro (`gemini-3-pro-image` on Vertex, project `crafty-haiku-422`, ADC),
  prompt `style/prompts/photo-v4.md`, two private style refs in `style/references/`, 1K windows.
  Goal: people recognise their own address, with an 80s-arcade feel (tinyrj is the benchmark).
- **Generation:** 40 overlapping 1024 px windows (stride 768) → min-error path cuts
  (`pipeline/mosaic.py`). Chosen candidate per window = symlinks (png + json) in
  `data/styled/nbp-v4-chosen` → `nbp-v4-s2-win` (seed 2), `nbp-v4-s3-win` (7 rejects redone with
  per-window hints in `style/prompts/hints/`), `nbp-v4-s5-win` (w0_0, better seam at the arc).
  ~$13 spent on Vertex in total.
- **Open water is painted by code** (`pipeline/water.py`, `mosaic compose --water-from`): the
  model invented islands in open water.
- **Viewer:** own WebGL renderer is the default (`viewer/src/glmap.ts`, `tileworker.ts`,
  `minimap.ts`): tiles decoded in workers, draws only when something changes, 60 fps, mini-map,
  pinch/trackpad/keyboard. `?renderer=osd` = the old OpenSeadragon viewer (canvas drawer).
  Benchmark: `viewer/bench.ts` (numbers in PLAN.md Phase 7). Map credit "Map data © Google" flows
  from the render sidecars through `cut` → `compose` → pyramid manifest.
- **User requirement:** the map must be fast and snappy; a game engine will add CPU load later.
- **Local GPU route paused** (PC has faulty RAM), code on branch `local-model`.
- **Google 3D Tiles: 403 for this account since 2026-11-06** ("not available for your account
  and region"). Existing renders still work; nothing new can be rendered from Google.

## How to run

```sh
# compose (after changing chosen windows) → HEAD image → pyramid → viewer
uv run python -m pipeline.mosaic compose --plan data/plan/sodermalm.json \
    --styled data/styled/nbp-v4-chosen --out data/mosaic/nbp-v4-pilot \
    --water-from data/renders/sodermalm
uv run python -m pipeline.head --mosaic data/mosaic/nbp-v4-pilot --area config/area.yaml \
    --out data --note "what changed" [--detail x,y]
uv run python -m pipeline.build_pyramid --plan data/plan/sodermalm.json \
    --tiles-dir data/mosaic/nbp-v4-pilot --out dist/sodermalm --style pixel --title "Södermalm"
cd viewer && npm run viewer -- dev --map ../dist/sodermalm      # http://127.0.0.1:5180/
npm run viewer -- build --map ../dist/sodermalm && npm run bench -- --site ../dist/sodermalm --label gl
```

`data/HEAD.png` (+ `HEAD.md`) is the user's progress view: regenerate it after every change to
the map and say what it shows. Delete superseded outputs in `data/` as you go.

## Cars as sprites: first test done on ground heights

User asked: "can you make cars of the sprites?" Done as a game-layer test (PLAN.md Phase 7):

- `pipeline/roads.py points|build` → `dist/sodermalm/roads.json` (OSM from
  `data/osm/Stockholm.osm.pbf`, BBBike; Overpass was down all afternoon).
- `viewer/src/sprites.ts` (atlas from 3D boxes, the manifest's camera), `viewer/src/cars.ts`
  (traffic + instanced draw), `MapLayer` hook in `glmap.ts`. `?cars=N` (default 400), `p` pauses.
- 2000 cars: 60 fps idle/pan/zoom, +37 ms/s main thread. Screenshot:
  `data/roads/sodermalm/cars-view.png`.

**Ground heights.** Roads are at h = 0 (`build --flat`), so on the hills cars drive
on rooftops.

1. User: create a free Geotorget account (geotorget.lantmateriet.se); put the login in `.env`
   as `LM_USER` / `LM_PASSWORD`.
2. Download Markhöjdmodell `m657_67`, `m658_67` (URLs via STAC `dtm-cog`, see PLAN.md), add
   `roads.py build --dem` (pyproj to SWEREF99 TM, sample the 1 m grid, RH2000 → plan h,
   calibrate the offset on the waterfront), rebuild `roads.json`, check cars on Götgatan.
3. Then occlusion (buildings in front of streets), now without the Google depth buffer:
   building blocks (OSM / Stockholm LOD1) → "ground visible" mask.

## Deferred (user: move forward, revisit quality later)

- Seams where neighbours drew the same building differently: rank seams by colour difference
  along the cut, 2 more seeds for the worse window of each bad seam (~12 windows, ~$3.20), pick
  by agreement. Scorer logic in PLAN.md Phase 5 (make it `mosaic agree`).
- Skeppsbron quay (w2_-2): invented buildings on the open quay.
- A bright photo-looking green strip at the west edge of the pilot.
- map.db + review app (Phase 5).
- Waiting on the user: licence replies (Stockholm stad, Hexagon, HERE). **Closed mode**: nothing
  derived from Google is published; the viewer runs on localhost only.

## Rules to keep (from CLAUDE.md and the user)

- Update `PLAN.md` in the same change as the work. The user has OK'd commit + push as work lands.
- Compute tile and window geometry only in `plan_tiles.py`. Nearest-neighbour scaling only.
  Never blend at seams.
- Test on a 3×3 block before running a large batch. Keep secrets in `.env`.
- Keep `data/` tidy and `data/HEAD.png` current; tell the user what it shows.
