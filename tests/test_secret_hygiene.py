"""Keep temporary probes from embedding service credentials."""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models.emotion import EmotionTracker
from app.utils.weather import WeatherCache


ROOT = Path(__file__).parents[1]


class SecretHygieneTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "_tmptest" / "probe_jev_systemone.py").exists(),
                         "local probe fixture is not published")
    def test_emotion_probe_loads_key_from_environment(self):
        source = (ROOT / "_tmptest" / "probe_jev_systemone.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        assignments = [
            node.value for node in module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "KEY"
                    for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        self.assertNotIsInstance(assignments[0], ast.Constant)

    def test_corrupt_emotion_shape_loads_as_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "emotion.json"
            path.write_text(json.dumps({"mood": "低落"}), encoding="utf-8")
            tracker = EmotionTracker(str(path), "", "", "")
            self.assertEqual(tracker.entries, [])
            path.write_text(json.dumps([{"mood": "低落"}]), encoding="utf-8")
            tracker = EmotionTracker(str(path), "", "", "")
            self.assertEqual(tracker.entries, [])

    def test_weather_refresh_catches_fetch_failure_and_keeps_retry_window(self):
        cache = WeatherCache("key", "440300", ttl=3600)
        with patch.object(cache, "_fetch", side_effect=TimeoutError("weather timeout")):
            with self.assertLogs("app.weather", level="WARNING") as captured:
                result = cache.refresh()
        self.assertIsNone(result)
        self.assertTrue(any("weather timeout" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
