from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_training_area_shares_grid_with_pilot():
    pilot = yaml.safe_load((CONFIG / "area.yaml").read_text())
    train = yaml.safe_load((CONFIG / "training.yaml").read_text())
    for key in ("origin", "camera", "scale"):
        assert train[key] == pilot[key], key
    for key in ("render_size_px", "quadrants_per_side", "overlap_px"):
        assert train["tiles"][key] == pilot["tiles"][key], key
