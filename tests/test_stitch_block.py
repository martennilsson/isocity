import numpy as np
from PIL import Image

from pipeline.plan_tiles import load_area, plan
from pipeline.stitch_block import load_tiles, measure_seams, stitch
from tests.test_plan_tiles import AREA


def test_stitch_rebuilds_world_image_and_overlaps_match(tmp_path):
    cfg, boundary = load_area(AREA)
    cfg = {**cfg, "tiles": {**cfg["tiles"], "overlap_px": 5}}
    p = plan(cfg, boundary)
    rects = [t["render_rect_px"] for t in p["tiles"]]
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    world = np.random.default_rng(0).integers(0, 256, (y1 - y0, x1 - x0, 4), dtype=np.uint8)
    world[..., 3] = 255
    for t in p["tiles"]:
        rx0, ry0, rx1, ry1 = t["render_rect_px"]
        Image.fromarray(world[ry0 - y0 : ry1 - y0, rx0 - x0 : rx1 - x0]).save(tmp_path / f"{t['id']}.png")

    tiles = load_tiles(p, tmp_path, None)
    canvas, (ox, oy) = stitch(tiles)
    for t in p["tiles"]:
        sx0, sy0, sx1, sy1 = t["screen_rect_px"]
        assert (canvas[sy0 - oy : sy1 - oy, sx0 - ox : sx1 - ox] == world[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0]).all()
    seams = measure_seams(tiles, canvas, (ox, oy))
    assert seams and all(s.overlap_max == 0 for s in seams)
    assert all(0.8 < s.step_ratio < 1.2 for s in seams)  # random noise: seam step ~ inside step
