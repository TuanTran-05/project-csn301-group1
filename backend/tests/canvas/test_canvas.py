import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("canvas_harness.cjs")
CASES = [
    "natural_sort", "every_layout_places_every_node", "hierarchy_orders_roles",
    "hierarchy_distance_fallback", "star_hub", "force_is_deterministic",
    "layouts_tiny_inputs", "place_centres_and_snaps", "zoom_anchor", "fit_contains_nodes",
]


@pytest.mark.parametrize("case", CASES)
def test_canvas_behaviour(case):
    result = subprocess.run(["node", str(HARNESS), case], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"case": case, "ok": True}
