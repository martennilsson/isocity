# Handoff — 2026-11-05

State of the work at the end of a session, for whoever picks it up next. `PLAN.md` is the full
plan and history; this file is the short version of where things stand.

## Where we are

- **Style is chosen:** Nano Banana Pro (`gemini-3-pro-image` on Vertex, project `crafty-haiku-422`,
  ADC auth), prompt `style/prompts/photo-v4.md`, two style references in `style/references/`
  (private, git-ignored), 1K per image. No pixel grid, no colour grade for now (user's choice).
  The user's key goal: keep enough detail (buildings, windows, doors, trees) that people
  recognise their own address, with an 80s-arcade feel. tinyrj is the reference for the look.
- **Seams are solved** (3×3 test): overlapping 1024 px generation windows every 768 px (256 px
  shared bands), composed with min-error path cuts. Seam step ratio mean 0.84 (straight cuts
  1.80; ~1 = invisible). All 9 windows passed the fidelity check.
- **Local GPU route is paused** (the PC has faulty RAM). Its code is on branch `local-model`.
- **Spent on Vertex so far:** ~$7.20.

## Latest commits

All work is committed and pushed (`main`). `3801ef3`: local fidelity check, shared rate-limit
retry, path-cut plan. `c9fb961`: generation windows and the path-cut mosaic:

- `pipeline/plan_tiles.py`, `config/area.yaml`: generation windows (`generation:` with
  `window_px: 1024`, `stride_px: 768`); `gen_window_rect_px`, `plan_windows`. The plan JSON gets a
  `generation` section (40 windows for the pilot). Window geometry is computed only here.
- `pipeline/mosaic.py`: `cut` (window renders) and `compose` (path cuts → canvas, marked
  canvas, `cuts.json`, plan tiles as RGBA).
- Tests in `tests/test_plan_tiles.py` and `tests/test_mosaic.py` (17 pass, ruff clean).
- `pipeline/infill.py` and `style/prompts/infill-v1.md` removed (superseded).

## Next step (waiting on the user's go-ahead)

Generate the whole pilot: 40 windows, ≈ $5.40 with one seed or ≈ $10.70 with two seeds (choose
per window). Then compose and check by eye:

```sh
uv run python -m pipeline.plan_tiles --area config/area.yaml --out data/plan/sodermalm.json
uv run python -m pipeline.mosaic cut --plan data/plan/sodermalm.json \
    --renders data/renders/sodermalm --out data/windows/sodermalm      # prints the --tiles list
uv run python -m pipeline.stylize --renders data/windows/sodermalm --tiles=<list> \
    --prompt style/prompts/photo-v4.md --out data/styled/nbp-v4-s2-win \
    --project crafty-haiku-422 --model gemini-3-pro-image --seed 2 \
    --ref "style/references/Inklistrat 2026-11-04 kl. 1111.22.30.png" \
    --ref "style/references/Skärmavbild 2026-11-04 kl. 11.20.47.png"
uv run python -m pipeline.fidelity --renders data/windows/sodermalm --styled data/styled/nbp-v4-s2-win
uv run python -m pipeline.mosaic compose --plan data/plan/sodermalm.json \
    --styled data/styled/nbp-v4-s2-win --out data/mosaic/nbp-v4-s2
```

The 9 windows from the 3×3 test (i, j in −1..1) already exist in `data/styled/nbp-v4-s2-win`.
`stylize.py` skips existing outputs, so they won't be paid for again.

After that: `data/map.db` per window (candidates, scores, choice), the review app, then the
pyramid from the mosaic tiles (`build_pyramid.py` tile mode) and the viewer.

## Open points

- Remaining seam issue: faint tone shifts across flat areas (lawns, plazas). If they're
  visible on the whole pilot, pick the seed per window that agrees best with its neighbours.
- Seed variance sometimes invents things (boats, a building in a park). `fidelity.py`'s local
  check flags them as "CHECK local", which needs a look by eye.
- Later options: a 2× pixel grid (`pipeline/pixelate.py`) to cut storage, a flatter look and
  colour grade (deferred), the user's own style references, over-zoom in the viewer.
- Waiting on the user: licence replies (Stockholm stad, Hexagon, HERE). Closed mode still
  applies, so nothing derived from Google is published.

## Rules to keep (from CLAUDE.md and the user)

- Update `PLAN.md` in the same change as the work. Commit or push only when asked.
- Compute tile and window geometry only in `plan_tiles.py`. Use nearest-neighbour scaling only.
  Never blend at seams.
- Test on a 3×3 block before running a large batch. Keep secrets in `.env`.
