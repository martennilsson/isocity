"""Open water drawn by code, not by the image model (Phase 5).

The image model will not leave a large empty water area empty: on almost-all-water windows it
invents islands and quays even when told not to, and its water colour differs from window to
window. So open water is found in the render and painted flat, the way the style asks for it
anyway ("water as flat blue with a few highlight pixels").

  water_mask: render pixels that are open water. Dark, blue-dominant, almost texture-free
              (after a 5 px median, which removes the thin bright mesh-seam lines Google's 3D
              tiles show on water), in connected areas of at least MIN_AREA_M2: dark roofs and
              shadows are small or textured, so they drop out. Small holes (buoys, artefacts)
              are filled; big ones (boats, piers) stay.
  grow_into:  extend the mask into connected pixels of the styled canvas that are clearly water
              blue, so the model's own water next to the open water (harbours, under bridges,
              where the render has reflections) gets the same flat colour.
  paint:      flat WATER colour + short horizontal highlight dashes. Dashes are placed by a hash of
              absolute screen coordinates, so they are the same on every run and continue across
              window and tile edges.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

WATER = (64, 124, 176)  # middle of the model's own water colours on the pilot (photo-v4)
HIGHLIGHT = (132, 186, 228)
MIN_AREA_M2 = 5_000  # smallest open-water area (40 000 px at 0.25 m/px, 30° camera)
HOLE_M2 = 75  # holes smaller than this inside open water are filled (buoys, artefacts)
DASH_CELL = 6  # one possible dash start every DASH_CELL px along a row
DASH_ODDS = 4 / 997  # share of dash starts that get a dash


PAD = 16  # edge padding so the morphology does not eat the canvas border


def ground_m2_per_px(plan: dict) -> float:
    """Ground area one screen pixel covers: mpp east-west × mpp / sin(elevation) north-south."""
    mpp = plan["camera"]["frustum_width_m"] / plan["render_size_px"]
    return mpp * mpp / -plan["camera"]["view_dir_enu"][2]


def water_mask(render: np.ndarray, m2_per_px: float) -> np.ndarray:
    """Bool mask of open water in an RGB render (h × w × 3, uint8).

    Area thresholds are in m², so they hold at any scale; the filter sizes (median, texture
    window, morphology) are in pixels and were tuned at 0.25 m/px."""
    render = np.pad(render, ((PAD, PAD), (PAD, PAD), (0, 0)), mode="edge")
    r = np.stack([ndi.median_filter(render[..., k], size=5) for k in range(3)], -1).astype(np.float32)
    g = r.mean(-1)
    m = ndi.uniform_filter(g, 9)
    sd = np.sqrt(np.maximum(ndi.uniform_filter(g * g, 9) - m * m, 0))
    blue_red = r[..., 2] - r[..., 0]
    cand = ((g < 80) & (blue_red >= 15) & (blue_red <= 50)
            & (r[..., 1] > r[..., 0]) & (r[..., 2] > r[..., 1]) & (sd < 3.5))
    cand = ndi.binary_opening(cand, iterations=2)
    lab, n = ndi.label(cand)
    if n == 0:
        return np.zeros((render.shape[0] - 2 * PAD, render.shape[1] - 2 * PAD), bool)
    sizes = ndi.sum(cand, lab, range(1, n + 1))
    mask = np.isin(lab, 1 + np.nonzero(sizes * m2_per_px >= MIN_AREA_M2)[0])
    mask = ndi.binary_closing(mask, iterations=6)
    holes = ndi.binary_fill_holes(mask) & ~mask
    hl, hn = ndi.label(holes)
    if hn:
        hs = ndi.sum(holes, hl, range(1, hn + 1))
        mask |= np.isin(hl, 1 + np.nonzero(hs * m2_per_px < HOLE_M2)[0])
    return mask[PAD:-PAD, PAD:-PAD]


def grow_into(mask: np.ndarray, styled: np.ndarray) -> np.ndarray:
    """mask plus the clearly-water-blue styled pixels connected to it."""
    s = styled.astype(int)
    blue = (s[..., 2] - s[..., 0] > 50) & (s[..., 2] - s[..., 1] > 25)
    blue = ndi.binary_opening(np.pad(blue, PAD, mode="edge"), iterations=1)[PAD:-PAD, PAD:-PAD]
    return ndi.binary_propagation(mask, mask=blue | mask)


def _hash(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return ((a.astype(np.int64) * 73856093) ^ (b.astype(np.int64) * 19349663)) & 0xFFFFFFFF


def paint(canvas: np.ndarray, mask: np.ndarray, x0: int, y0: int) -> None:
    """Paint mask flat water in place; (x0, y0) = absolute screen px of canvas[0, 0]."""
    canvas[mask] = WATER
    h, w = mask.shape
    # columns where abs x % DASH_CELL == 0, from one cell left of the canvas (dashes < DASH_CELL)
    xs = np.arange(-(x0 % DASH_CELL) - DASH_CELL, w, DASH_CELL)
    ys = np.arange(h)
    ax, ay = np.meshgrid(xs + x0, ys + y0)
    hv = _hash(ax // DASH_CELL, ay)
    yy, kk = np.nonzero((hv % 997) < round(DASH_ODDS * 997))
    for y, k in zip(yy, kk):
        x = xs[k]
        a, b = max(x, 0), x + 2 + int(hv[y, k] >> 10) % 4
        if b > a:
            canvas[y, a:b][mask[y, a:b]] = HIGHLIGHT
