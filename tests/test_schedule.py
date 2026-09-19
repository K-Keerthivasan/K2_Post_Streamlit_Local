from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule


WEEKDAY_CONFIG = {"schedule": {"times": ["09:00", "18:00"],
                               "days": ["mon", "tue", "wed", "thu", "fri"]}}


class ParseWhenTests(unittest.TestCase):
    def test_datetime_local_input(self):
        self.assertEqual(schedule.parse_when("2026-08-27T18:30"),
                         datetime(2026, 8, 27, 18, 30))

    def test_spreadsheet_date_formats(self):
        self.assertEqual(schedule.parse_when("2026-09-03 09:00"),
                         datetime(2026, 9, 3, 9, 0))
        self.assertEqual(schedule.parse_when("2026-09-03"),
                         datetime(2026, 9, 3, 0, 0))

    def test_empty_means_unscheduled(self):
        self.assertIsNone(schedule.parse_when(""))
        self.assertIsNone(schedule.parse_when(None))

    def test_utc_suffix_is_tolerated(self):
        self.assertEqual(schedule.parse_when("2026-08-27T18:30:00Z"),
                         datetime(2026, 8, 27, 18, 30))

    def test_garbage_raises(self):
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_when("next tuesday-ish")


class IsDueTests(unittest.TestCase):
    def _entry(self, when, status="scheduled"):
        return {"status": status, "scheduled_at": schedule.iso(when)}

    def test_past_slot_is_due(self):
        past = datetime.now() - timedelta(minutes=5)
        self.assertTrue(schedule.is_due(self._entry(past)))

    def test_future_slot_is_not_due(self):
        future = datetime.now() + timedelta(hours=2)
        self.assertFalse(schedule.is_due(self._entry(future)))

    def test_only_scheduled_entries_are_ever_due(self):
        past = datetime.now() - timedelta(hours=1)
        for status in ("pending", "approved", "published", "rejected", "failed"):
            self.assertFalse(schedule.is_due(self._entry(past, status)), status)

    def test_scheduled_without_a_time_is_not_due(self):
        self.assertFalse(schedule.is_due({"status": "scheduled", "scheduled_at": ""}))


class NextSlotsTests(unittest.TestCase):
    def test_slots_come_from_config_in_order(self):
        start = datetime(2026, 8, 26, 8, 0)          # a Wednesday, before 09:00
        slots = schedule.next_slots(3, config=WEEKDAY_CONFIG, start=start)

        self.assertEqual(slots, [datetime(2026, 8, 26, 9, 0),
                                 datetime(2026, 8, 26, 18, 0),
                                 datetime(2026, 8, 27, 9, 0)])

    def test_weekend_days_are_skipped(self):
        start = datetime(2026, 8, 28, 19, 0)         # Friday, after the last slot
        slots = schedule.next_slots(1, config=WEEKDAY_CONFIG, start=start)

        self.assertEqual(slots[0], datetime(2026, 8, 31, 9, 0))   # the Monday

    def test_taken_slots_are_not_reused(self):
        start = datetime(2026, 8, 26, 8, 0)
        taken = ["2026-08-26T09:00:00"]
        slots = schedule.next_slots(2, config=WEEKDAY_CONFIG, start=start, taken=taken)

        self.assertEqual(slots, [datetime(2026, 8, 26, 18, 0),
                                 datetime(2026, 8, 27, 9, 0)])

    def test_slots_are_always_in_the_future(self):
        start = datetime(2026, 8, 26, 12, 0)         # between the two slots
        slots = schedule.next_slots(1, config=WEEKDAY_CONFIG, start=start)

        self.assertEqual(slots[0], datetime(2026, 8, 26, 18, 0))

    def test_malformed_times_are_ignored_not_fatal(self):
        cfg = {"schedule": {"times": ["nope", "10:00"], "days": ["mon"]}}
        slots = schedule.next_slots(1, config=cfg, start=datetime(2026, 8, 26, 8, 0))

        self.assertEqual(slots[0], datetime(2026, 8, 31, 10, 0))


class SlotConfigTests(unittest.TestCase):
    def test_missing_block_falls_back_to_defaults(self):
        cfg = schedule.slot_config({})

        self.assertEqual(cfg["times"], schedule.DEFAULT_SLOTS)
        self.assertTrue(cfg["auto_publish"])

    def test_day_names_are_normalised_to_three_letters(self):
        cfg = schedule.slot_config({"schedule": {"days": ["Monday", "TUESDAY"]}})

        self.assertEqual(cfg["days"], ["mon", "tue"])


class MonthGridTests(unittest.TestCase):
    entries = [
        {"id": "a", "scheduled_at": "2026-08-27T18:30:00", "status": "scheduled"},
        {"id": "b", "scheduled_at": "2026-08-27T09:00:00", "status": "scheduled"},
        {"id": "c", "status": "pending"},
    ]

    def test_posts_land_on_their_day_sorted_by_time(self):
        grid = schedule.month_grid(2026, 8, self.entries)
        day = next(d for w in grid["weeks"] for d in w if d["date"] == "2026-08-27")

        self.assertEqual([p["id"] for p in day["posts"]], ["b", "a"])

    def test_undated_posts_are_separated(self):
        grid = schedule.month_grid(2026, 8, self.entries)

        self.assertEqual([p["id"] for p in grid["undated"]], ["c"])

    def test_grid_is_whole_weeks_with_out_of_month_padding(self):
        grid = schedule.month_grid(2026, 8, [])

        for week in grid["weeks"]:
            self.assertEqual(len(week), 7)
        self.assertTrue(any(not d["in_month"] for w in grid["weeks"] for d in w))

    def test_label(self):
        self.assertEqual(schedule.month_grid(2026, 8, [])["label"], "August 2026")


class ShiftMonthTests(unittest.TestCase):
    def test_wraps_backwards_across_the_year(self):
        self.assertEqual(schedule.shift_month(2026, 1, -1), (2025, 12))

    def test_wraps_forwards_across_the_year(self):
        self.assertEqual(schedule.shift_month(2026, 12, 1), (2027, 1))

    def test_no_op(self):
        self.assertEqual(schedule.shift_month(2026, 6, 0), (2026, 6))


if __name__ == "__main__":
    unittest.main()
