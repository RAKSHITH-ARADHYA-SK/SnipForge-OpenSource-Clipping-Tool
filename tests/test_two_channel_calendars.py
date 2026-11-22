import datetime
import unittest
from studio import posting, accounts, ctx


class TestTwoChannelCalendars(unittest.TestCase):
    def setUp(self):
        self.tok = ctx._uid.set("u_local")
        # Sample account IDs matching real setup or mock
        self.acc1_id = "yt_UCvAfa965sKNJcG3YZPMA27w" # Viral Snips (added earlier -> Channel 1)
        self.acc2_id = "yt_UCKzydBBNgFFLHGVbjhGrPZQ" # ViroX_Viru (added second -> Channel 2)

    def tearDown(self):
        ctx._uid.reset(self.tok)

    def test_channel_identification(self):
        # Channel 1 is not channel 2
        self.assertFalse(posting._is_channel_2(self.acc1_id))
        # Channel 2 is identified as channel 2
        self.assertTrue(posting._is_channel_2(self.acc2_id))

    def test_channel_2_slots(self):
        # Before Nov 20, 2026: 3 shorts per day at 6:30 AM, 2:30 PM, 7:30 PM
        date_pre = datetime.date(2026, 11, 18)
        slots_pre = posting._slots_for_date(date_pre, self.acc2_id)
        self.assertEqual(slots_pre, [(6, 30), (14, 30), (19, 30)])
        self.assertEqual(posting._max_shorts_for_date(date_pre, self.acc2_id), 3)

        # From Nov 20, 2026 onwards: 8 shorts per day
        date_nov20 = datetime.date(2026, 11, 20)
        slots_nov20 = posting._slots_for_date(date_nov20, self.acc2_id)
        self.assertEqual(len(slots_nov20), 8)
        self.assertEqual(posting._max_shorts_for_date(date_nov20, self.acc2_id), 8)

        date_jan1 = datetime.date(2027, 1, 1) # Friday
        slots_jan1 = posting._slots_for_date(date_jan1, self.acc2_id)
        self.assertEqual(len(slots_jan1), 8)
        self.assertEqual(posting._max_shorts_for_date(date_jan1, self.acc2_id), 8)

    def test_channel_1_slots(self):
        # Before Nov 20, 2026: 4 shorts/day
        date_pre = datetime.date(2026, 11, 18)
        slots_ch1_pre = posting._slots_for_date(date_pre, self.acc1_id)
        self.assertEqual(len(slots_ch1_pre), 4)
        self.assertEqual(posting._max_shorts_for_date(date_pre, self.acc1_id), 4)

        # From Nov 20, 2026 onwards: 8 shorts/day
        date_nov20 = datetime.date(2026, 11, 20)
        slots_ch1_nov20 = posting._slots_for_date(date_nov20, self.acc1_id)
        self.assertEqual(len(slots_ch1_nov20), 8)
        self.assertEqual(posting._max_shorts_for_date(date_nov20, self.acc1_id), 8)

        # Later (e.g. Dec 10, 2026 onwards): 8 shorts/day
        date_dec10 = datetime.date(2026, 12, 10)
        slots_ch1_dec10 = posting._slots_for_date(date_dec10, self.acc1_id)
        self.assertEqual(len(slots_ch1_dec10), 8)
        self.assertEqual(posting._max_shorts_for_date(date_dec10, self.acc1_id), 8)

    def test_upcoming_slots_ch2(self):
        slots_ch2 = posting.upcoming_slots(self.acc2_id, count=6)
        self.assertTrue(len(slots_ch2) > 0)
        for s in slots_ch2:
            self.assertEqual(s["phase"], 2)

    def test_separate_calendars(self):
        cal_ch1 = posting.schedule_calendar(days_ahead=5, account_id=self.acc1_id)
        cal_ch2 = posting.schedule_calendar(days_ahead=5, account_id=self.acc2_id)

        self.assertEqual(cal_ch1["account_id"], self.acc1_id)
        self.assertEqual(cal_ch2["account_id"], self.acc2_id)
        self.assertFalse(cal_ch1["is_channel_2"])
        self.assertTrue(cal_ch2["is_channel_2"])

        # Check slot counts in first day (from Nov 20 onwards, 8 slots per day on both channels)
        day1_ch1 = cal_ch1["days"][0]
        day1_ch2 = cal_ch2["days"][0]

        self.assertEqual(day1_ch2["max_slots"], 8)
        self.assertEqual(len(day1_ch2["slots"]), 8)

        self.assertEqual(day1_ch1["max_slots"], 8)
        self.assertEqual(len(day1_ch1["slots"]), 8)


if __name__ == "__main__":
    unittest.main()
