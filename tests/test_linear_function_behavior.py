"""Execute the real interaction script and inspect canvas output (Node required)."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from learning_coach.adapters.visuals import _SCRIPT


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the visual interaction script")
class LinearFunctionBehaviorTests(unittest.TestCase):
    def test_sliders_keep_axes_fixed_and_manual_controls_change_view(self):
        spec = {"kind": "linear_function", "title": "斜率的变化", "slope": 1,
                "intercept": 0, "x_min": -10, "x_max": 10}
        completed = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests/linear_function_harness.cjs")],
            input=json.dumps({"script": _SCRIPT, "spec": spec}),
            text=True, capture_output=True, check=True, timeout=10,
        )
        result = json.loads(completed.stdout)
        initial, changed, fitted, reset = (result[key] for key in ("initial", "changed", "fitted", "reset"))
        self.assertEqual(initial["ticks"], changed["ticks"])
        self.assertNotEqual(initial["curve"]["points"], changed["curve"]["points"])
        self.assertTrue(changed["curve"]["clipped"])
        self.assertNotEqual(changed["ticks"], fitted["ticks"])
        self.assertEqual(reset, initial)


if __name__ == "__main__":
    unittest.main()
