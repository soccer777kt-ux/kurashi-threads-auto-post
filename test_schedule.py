import unittest
from datetime import datetime

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

    def test_recovery_window_eventually_closes(self):
        too_late = datetime(2026, 8, 11, 23, 16, tzinfo=post_threads.JST)
        self.assertIsNone(post_threads.scheduled_slot(self.state, too_late))


if __name__ == "__main__":
    unittest.main()
