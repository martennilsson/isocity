import math
from pathlib import Path

import pytest

from pipeline.plan_tiles import (
    Projection,
    ecef_to_geodetic,
    geodetic_to_ecef,
    load_area,
    gen_window_rect_px,
    plan,
    window_rect_px,
)

AREA = Path(__file__).resolve().parents[1] / "config" / "area.yaml"


@pytest.fixture(scope="module")
def area():
    return load_area(AREA)


@pytest.fixture(scope="module")
def proj(area):
    return Projection.from_config(area[0])


def test_geodetic_roundtrip():
    lon, lat, h = ecef_to_geodetic(*geodetic_to_ecef(18.07, 59.31, 42.0))
    assert lon == pytest.approx(18.07, abs=1e-10)
    assert lat == pytest.approx(59.31, abs=1e-10)
    assert h == pytest.approx(42.0, abs=1e-6)


def test_origin_is_zero(proj):
    assert proj.lonlat_to_enu(proj.origin_lon, proj.origin_lat) == pytest.approx((0, 0, 0), abs=1e-6)
    assert proj.enu_to_screen((0, 0, 0)) == (0, 0)


def test_camera_basis_orthonormal(proj):
    r, v, u = proj.right, proj.view_dir, proj.camera_up
    for a in (r, v, u):
        assert math.hypot(*a) == pytest.approx(1)
    for a, b in ((r, v), (r, u), (v, u)):
        assert sum(x * y for x, y in zip(a, b)) == pytest.approx(0, abs=1e-12)


def test_two_to_one_ratio():
    p = Projection(18, 59, 0, 30, 45, 1.0)
    # A 1 m ground square rotated 45° projects to a diamond twice as wide as tall.
    pts = [p.enu_to_screen(c) for c in ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0))]
    xs, ys = [a for a, _ in pts], [b for _, b in pts]
    assert (max(xs) - min(xs)) / (max(ys) - min(ys)) == pytest.approx(2)


def test_screen_ground_roundtrip(proj):
    for sx, sy in ((0, 0), (512, -300), (-2048.5, 1500)):
        assert proj.enu_to_screen(proj.screen_to_ground(sx, sy)) == pytest.approx((sx, sy), abs=1e-9)


def test_heights_move_straight_up(proj):
    sx0, sy0 = proj.enu_to_screen((10, 20, 0))
    sx1, sy1 = proj.enu_to_screen((10, 20, 10))
    assert sx1 == pytest.approx(sx0)
    assert sy0 - sy1 == pytest.approx(10 * math.cos(math.radians(30)) / proj.metres_per_pixel)


def test_tiles_abut_exactly(area):
    result = plan(*area)
    size = result["tile_size_px"]
    by_xy = {(t["x"], t["y"]): t for t in result["tiles"]}
    assert by_xy, "plan produced no tiles"
    for (x, y), t in by_xy.items():
        assert t["screen_rect_px"] == [x * size, y * size, (x + 1) * size, (y + 1) * size]
        if (x + 1, y) in by_xy:
            assert by_xy[(x + 1, y)]["screen_rect_px"][0] == t["screen_rect_px"][2]
        if (x, y + 1) in by_xy:
            assert by_xy[(x, y + 1)]["screen_rect_px"][1] == t["screen_rect_px"][3]


def test_targets_project_to_tile_centres(area, proj):
    result = plan(*area)
    size = result["tile_size_px"]
    for t in result["tiles"]:
        assert proj.enu_to_screen(tuple(t["target_enu"])) == pytest.approx(
            ((t["x"] + 0.5) * size, (t["y"] + 0.5) * size), abs=1e-6
        )
        assert proj.ecef_to_enu(tuple(t["target_ecef"])) == pytest.approx(t["target_enu"], abs=1e-6)


def test_quadrants_unique(area):
    result = plan(*area)
    quads = [tuple(q) for t in result["tiles"] for q in t["quadrants"]]
    assert len(quads) == len(set(quads))


def test_overlap_pads_render_but_not_grid(area):
    cfg, boundary = area
    cfg = {**cfg, "tiles": {**cfg["tiles"], "overlap_px": 3}}
    result = plan(cfg, boundary)
    assert result["render_size_px"] == result["tile_size_px"] + 6
    assert result["camera"]["frustum_width_m"] == pytest.approx(
        result["render_size_px"] * cfg["scale"]["metres_per_pixel"]
    )
    for t in result["tiles"]:
        x0, y0, x1, y1 = t["screen_rect_px"]
        assert t["render_rect_px"] == [x0 - 3, y0 - 3, x1 + 3, y1 + 3]
    # padding is symmetric, so the camera target (tile centre) is unchanged
    base = {t["id"]: t["target_enu"] for t in plan(*area)["tiles"]}
    for t in result["tiles"]:
        assert t["target_enu"] == pytest.approx(base[t["id"]])


def test_infill_window_spanning_tiles_is_covered_by_renders(area):
    result = plan(*area)
    q = result["quadrant_size_px"]
    owner = {(c["qx"], c["qy"]): c["tile"] for c in result["quadrants"]}
    # a 2x2 window straddling the shared corner (screen origin) of four render tiles
    qx = qy = -1
    assert window_rect_px(qx, qy, 2, 2, q) == (-q, -q, q, q)
    cells = [(qx + i, qy + j) for j in (0, 1) for i in (0, 1)]
    assert all(c in owner for c in cells)
    assert [owner[c] for c in cells] == ["-1_-1", "0_-1", "-1_0", "0_0"]


def test_quadrant_records_match_tiles(area):
    result = plan(*area)
    q = result["quadrant_size_px"]
    for c in result["quadrants"]:
        assert c["screen_rect_px"] == [c["qx"] * q, c["qy"] * q, (c["qx"] + 1) * q, (c["qy"] + 1) * q]


def test_generation_windows_overlap_by_band_and_cover_region(area):
    result = plan(*area)
    g = result["generation"]
    s, band = g["stride_px"], g["band_px"]
    by_ij = {(w["i"], w["j"]): w for w in g["windows"]}
    assert by_ij
    for (i, j), w in by_ij.items():
        assert w["rect_px"] == list(gen_window_rect_px(i, j, s, g["window_px"]))
        x0, y0, x1, y1 = w["rect_px"]
        assert (x1 - x0, y1 - y0) == (g["window_px"], g["window_px"])
        if (i + 1, j) in by_ij:
            assert x1 - by_ij[(i + 1, j)]["rect_px"][0] == band
            assert by_ij[(i + 1, j)]["core_rect_px"][0] == w["core_rect_px"][2]
        if (i, j + 1) in by_ij:
            assert y1 - by_ij[(i, j + 1)]["rect_px"][1] == band
    # a full rectangle of windows whose cores cover the boundary
    iis, jjs = {i for i, _ in by_ij}, {j for _, j in by_ij}
    assert len(by_ij) == len(iis) * len(jjs)
    bx = [c[0] for c in result["screen_boundary_px"]]
    by = [c[1] for c in result["screen_boundary_px"]]
    assert min(iis) * s <= min(bx) and (max(iis) + 1) * s >= max(bx)
    assert min(jjs) * s <= min(by) and (max(jjs) + 1) * s >= max(by)
