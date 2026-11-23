"""Tests of seasonal_rain.py, without internet (the web request is replaced by a stand-in).

Run:  python -m unittest test_seasonal_rain.py
"""

import datetime as dt
import unittest
import urllib.parse

import seasonal_rain as sr


def fake_archive(mm_per_day_by_point, missing_day=None):
    """A stand-in for the archive: each point gets a constant daily rain."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start = dt.date.fromisoformat(query["start_date"][0])
        end = dt.date.fromisoformat(query["end_date"][0])
        days = (end - start).days + 1
        answer = []
        for mm in mm_per_day_by_point:
            values = [mm] * days
            if missing_day is not None:
                values[missing_day] = None
            answer.append({"daily": {"time": [], "precipitation_sum": values}})
        return answer if len(answer) > 1 else answer[0]

    fetch.calls = calls
    return fetch


class SeasonDates(unittest.TestCase):
    def test_seasons(self):
        self.assertEqual(sr.season_dates(2024, "summer"), (dt.date(2024, 6, 1), dt.date(2024, 8, 31)))
        self.assertEqual(sr.season_dates(2024, "autumn"), (dt.date(2024, 9, 1), dt.date(2024, 11, 30)))
        # Winter 2024 starts in December 2023 and ends on 29 February (leap year).
        self.assertEqual(sr.season_dates(2024, "Winter"), (dt.date(2023, 12, 1), dt.date(2024, 2, 29)))
        self.assertEqual(sr.season_dates(2023, "winter")[1], dt.date(2023, 2, 28))
        with self.assertRaises(ValueError):
            sr.season_dates(2024, "monsoon")


class Normals(unittest.TestCase):
    def test_seasonal_sums_of_the_findel_normals(self):
        self.assertEqual(sr.normal_rain_mm("winter"), 220.5)
        self.assertEqual(sr.normal_rain_mm("spring"), 179.3)
        self.assertEqual(sr.normal_rain_mm("summer"), 217.0)
        self.assertEqual(sr.normal_rain_mm("autumn"), 214.5)
        self.assertEqual(sr.normal_rain_mm("year"), 831.3)
        four = sum(sr.normal_rain_mm(s) for s in sr.SEASONS)
        self.assertAlmostEqual(four, 831.3, places=6)


class Volume(unittest.TestCase):
    def test_one_mm_on_one_square_metre_is_one_litre(self):
        self.assertEqual(sr.rain_volume_m3(1, 1), 0.001)
        self.assertEqual(sr.rain_volume_m3(217.0, 1000), 217.0)
        self.assertAlmostEqual(sr.rain_volume_m3(217.0, 200), 43.4)
        self.assertAlmostEqual(sr.rain_volume_m3(831.3, 25.8e6), 21447540.0)
        with self.assertRaises(ValueError):
            sr.rain_volume_m3(-1, 10)


class FromTheArchive(unittest.TestCase):
    TODAY = dt.date(2026, 11, 22)

    def test_mean_of_the_points_over_the_season(self):
        fetch = fake_archive([2.0, 3.0, 4.0])
        rain = sr.seasonal_rain_mm(2024, "summer", fetch=fetch, today=self.TODAY)
        # 92 days; the three points total 184, 276 and 368 mm: mean 276 mm.
        self.assertEqual(rain["days"], 92)
        self.assertEqual(rain["rain_mm"], 276.0)
        self.assertEqual(rain["missing_days"], 0)
        self.assertEqual((rain["start"], rain["end"]), ("2024-06-01", "2024-08-31"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(fetch.calls[0]).query)
        self.assertEqual(query["daily"], ["precipitation_sum"])
        self.assertEqual(query["timezone"], ["Europe/Luxembourg"])
        self.assertEqual(query["latitude"], ["49.6200,49.5960,49.5720"])

    def test_one_point_and_a_missing_day(self):
        fetch = fake_archive([1.0], missing_day=10)
        rain = sr.seasonal_rain_mm(2023, "winter", points=[(49.6, 6.13)], fetch=fetch, today=self.TODAY)
        self.assertEqual(rain["days"], 90)
        self.assertEqual(rain["missing_days"], 1)
        self.assertEqual(rain["rain_mm"], 89.0)

    def test_a_season_not_yet_in_the_archive(self):
        with self.assertRaises(ValueError):
            sr.seasonal_rain_mm(2026, "autumn", fetch=fake_archive([1.0, 1.0, 1.0]), today=self.TODAY)
        with self.assertRaises(ValueError):
            sr.seasonal_rain_mm(1950, "winter", fetch=fake_archive([1.0, 1.0, 1.0]), today=self.TODAY)

    def test_an_answer_of_the_wrong_length_is_refused(self):
        def short(url, timeout):
            return [{"daily": {"precipitation_sum": [1.0] * 10}}] * 3
        with self.assertRaises(ValueError):
            sr.seasonal_rain_mm(2024, "summer", fetch=short, today=self.TODAY)

    def test_table_of_a_year_skips_the_seasons_not_yet_complete(self):
        rows = sr.seasonal_table(1000.0, year=2026, fetch=fake_archive([1.0, 1.0, 1.0]), today=self.TODAY)
        self.assertEqual([r["season"] for r in rows], ["winter", "spring", "summer"])
        self.assertEqual(rows[2]["rain_mm"], 92.0)
        self.assertEqual(rows[2]["volume_m3"], 92.0)

    def test_table_of_the_normals(self):
        rows = sr.seasonal_table(150000.0)
        self.assertEqual([r["season"] for r in rows], ["winter", "spring", "summer", "autumn", "year"])
        self.assertEqual(rows[2]["volume_m3"], 32550.0)


if __name__ == "__main__":
    unittest.main()
