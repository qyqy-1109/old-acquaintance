"""Focused regressions for date and profile input accuracy."""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models.user import UserProfile
from app.utils.clock import find_date


class MemoryAccuracyTests(unittest.TestCase):
    def test_explicit_relative_year_is_not_lost(self):
        now = dt.datetime(2026, 9, 23, 20, 0)
        self.assertEqual(find_date("去年9月20日去了医院", now), dt.date(2025, 9, 20))
        self.assertEqual(find_date("前年9月20日去了医院", now), dt.date(2024, 9, 20))
        self.assertEqual(find_date("明年9月20日要去医院", now), dt.date(2027, 9, 20))
        self.assertEqual(find_date("去年12月31日", now), dt.date(2025, 12, 31))

    def test_old_month_day_in_past_event_stays_in_current_year(self):
        now = dt.datetime(2026, 9, 23, 20, 0)
        self.assertEqual(find_date("用户1月2日去了医院", now), dt.date(2026, 1, 2))

    def test_profile_analyzes_newest_conversation_within_limit(self):
        captured = []

        def fake_chat_json(client, model, messages, **kwargs):
            captured.append(messages[0]["content"])
            return {"summary": "更新"}, ""

        with tempfile.TemporaryDirectory() as directory:
            profile = UserProfile(None, path=str(Path(directory) / "profile.json"))
            turns = [
                {"role": "user", "content": "旧消息" * 20},
                {"role": "assistant", "content": "旧回复" * 20},
                {"role": "user", "content": "最新消息：我搬到成都"},
            ]
            with patch("app.models.user.chat_json", side_effect=fake_chat_json):
                profile.analyze(turns, max_chars=45)

        self.assertIn("最新消息：我搬到成都", captured[0])


if __name__ == "__main__":
    unittest.main()
