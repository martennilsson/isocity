import numpy as np

from pipeline import water
from pipeline.mosaic import seam_stats


def _scene():
    """400×400 render: open water on top, a textured land strip below, a dark roof in the land."""
    rng = np.random.default_rng(0)
    img = np.empty((400, 400, 3), np.uint8)
    img[:250] = (41, 60, 80)  # render water
    img[250:] = rng.integers(60, 200, (150, 400, 3))  # textured land
    img[300:340, 50:120] = (40, 58, 78)  # a small water-coloured roof: too small to be water
    img[100:104, 200:204] = (230, 200, 60)  # a buoy: small hole, filled
    img[60:110, 300:380] = (240, 240, 240)  # a boat: big hole, kept
    return img


def test_ground_area_per_pixel():
    plan = {"render_size_px": 1024, "camera": {"frustum_width_m": 256.0, "view_dir_enu": [0, 0.866, -0.5]}}
    assert water.ground_m2_per_px(plan) == 0.125  # 0.25 m × 0.5 m at 30°


def test_water_mask_finds_open_water_only():
    m = water.water_mask(_scene(), m2_per_px=0.125 * 2)  # as if 0.25 m/px were twice as coarse
    assert m[20:230, 20:280].all()  # open water, buoy filled
    assert not m[255:, :].any()  # no land, not the small roof
    assert not m[70:100, 310:370].any()  # the boat stays


def test_grow_into_takes_only_connected_blue():
    mask = np.zeros((50, 50), bool)
    mask[:10] = True
    styled = np.full((50, 50, 3), (150, 150, 150), np.uint8)
    styled[10:20] = (40, 127, 200)  # model water next to the open water
    styled[35:45] = (40, 127, 200)  # a blue roof elsewhere
    g = water.grow_into(mask, styled)
    assert g[:20].all() and not g[20:].any()


def test_paint_is_anchored_in_screen_space():
    mask = np.ones((300, 600), bool)
    a = np.zeros((300, 600, 3), np.uint8)
    b = np.zeros((300, 600, 3), np.uint8)
    water.paint(a, mask, 1000, 2000)
    water.paint(b, mask, 1000 - 60, 2000 - 30)  # same screen area, canvas starts elsewhere
    assert (a[:-30, :-60] == b[30:, 60:]).all()
    colours = {tuple(c) for c in a.reshape(-1, 3)}
    assert colours == {water.WATER, water.HIGHLIGHT}


def test_seam_stats_skips_water_rows():
    c = np.zeros((100, 40, 3), np.uint8)
    path = np.full(100, 20)
    skip = np.ones((100, 40), bool)
    assert seam_stats(c, path, 0, range(100), skip)["step_ratio"] is None
    skip[:60] = False
    assert seam_stats(c, path, 0, range(100), skip)["rows"] == 60
