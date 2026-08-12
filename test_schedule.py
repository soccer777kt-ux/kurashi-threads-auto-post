import unittest
from datetime import datetime
from unittest.mock import patch

import post_threads


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.night = datetime(2026, 8, 11, 22, 3, tzinfo=post_threads.JST)
        self.state = {
            "next_index": 34,
            "posted_slots": ["2026-08-11-morning", "2026-08-11-lunch"],
            "last_posted_at": "2026-08-11T19:02:51+09:00",
        }

    def test_late_night_run_recovers_missing_slot(self):
        due = post_threads.scheduled_slot(self.state, self.night)
        self.assertIsNotNone(due)
        self.assertEqual(due[0], "night")

    def test_posted_slot_cannot_duplicate(self):
        self.state["posted_slots"].append("2026-08-11-night")
        self.assertIsNone(post_threads.scheduled_slot(self.state, self.night))

    def test_run_before_random_target_does_not_wait_or_post(self):
        before_target = datetime(2026, 8, 12, 7, 29, tzinfo=post_threads.JST)
        state = {"posted_slots": [], "last_posted_at": "2026-08-11T22:00:00+09:00"}
        self.assertIsNone(post_threads.scheduled_slot(state, before_target))

    def test_missed_morning_still_recovers_at_10_48(self):
        recovery_time = datetime(2026, 8, 12, 10, 48, tzinfo=post_threads.JST)
        due = post_threads.scheduled_slot(self.state, recovery_time)
        self.assertIsNotNone(due)
        self.assertEqual(due[0], "morning")

    @patch("post_threads.fetch_tokyo_weather")
    def test_weather_post_is_emotional_not_a_forecast(self, fetch_weather):
        fetch_weather.return_value = {
            "code": 61,
            "description": "雨",
            "emoji": "☔",
            "max_temp": 26,
            "min_temp": 22,
            "rain_probability": 100,
        }
        morning = datetime(2026, 8, 12, 7, 47, tzinfo=post_threads.JST)
        text = post_threads.build_morning_post(morning)
        self.assertIn("雨", text)
        for forecast_word in ("東京", "最高", "最低", "降水確率", "26℃", "22℃", "100％"):
            self.assertNotIn(forecast_word, text)

    def test_recovery_window_eventually_closes(self):
        too_late = datetime(2026, 8, 12, 11, 16, tzinfo=post_threads.JST)
        self.assertIsNone(post_threads.scheduled_slot(self.state, too_late))


if __name__ == "__main__":
    unittest.main()
