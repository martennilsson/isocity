import numpy as np

from pipeline.mosaic import GUARD_PX, band_cut, min_cut


def test_min_cut_follows_cheap_valley():
    cost = np.ones((50, 20))
    cols = 5 + (np.arange(50) // 10)  # a valley that drifts right one column every 10 rows
    cost[np.arange(50), cols] = 0
    assert (min_cut(cost) == cols).all()


def test_band_cut_runs_where_the_images_agree():
    rng = np.random.default_rng(0)
    old = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
    new = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
    new[:, 38:43] = old[:, 38:43]  # the two drawings agree only in columns 38..42
    p = band_cut(old, new, straight=False)
    assert ((p >= 38) & (p <= 42)).all()
    assert (p >= GUARD_PX).all() and (p < 64 - GUARD_PX).all()
    assert (band_cut(old, new, straight=True) == 32).all()
