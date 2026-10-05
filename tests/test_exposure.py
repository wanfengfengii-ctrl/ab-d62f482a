"""Tests for the adjudication core."""

import unittest
from decimal import Decimal

from app.exposure import ValidationError, evaluate, parse_rfc3339


def base_payload(readings, **overrides):
    stamps = sorted(item["timestamp"] for item in readings)
    payload = {
        "start_at": stamps[0],
        "end_at": stamps[-1],
        "temperature_threshold": 8,
        "max_interval_seconds": 600,
        "max_exposure_seconds": 900,
        "degree_minutes_budget": 100,
        "readings": readings,
    }
    payload.update(overrides)
    return payload


def r(minute, temp, second=0):
    return {
        "timestamp": f"2026-10-05T08:{minute:02d}:{second:02d}Z",
        "temperature": temp,
    }


class CrossingTests(unittest.TestCase):
    def test_exact_threshold_crossing_and_integral(self):
        # 08:00 10 -> 08:01 10 (fully above), then -> 08:02 6 (crosses 8).
        payload = base_payload([r(0, 10), r(1, 10), r(2, 6)])
        result = evaluate(payload)
        self.assertEqual(len(result["exposures"]), 1)
        iv = result["exposures"][0]
        # crossing: 60 + (8-10)/(6-10)*60 = 90s
        self.assertEqual(iv["start"], "2026-10-05T08:00:00Z")
        self.assertEqual(iv["end"], "2026-10-05T08:01:30Z")
        self.assertEqual(Decimal(str(iv["duration_seconds"])), Decimal("90"))
        # [0,60]: excess 2 => 2 deg-min; [60,90]: triangle 2*30/2/60 = 0.5
        self.assertEqual(
            Decimal(str(iv["degree_minutes"])), Decimal("2.5")
        )
        self.assertEqual(
            Decimal(str(result["total_degree_minutes"])), Decimal("2.5")
        )
        self.assertEqual(result["verdict"], "pass")

    def test_equality_is_not_exposure(self):
        payload = base_payload([r(0, 8), r(1, 8), r(2, 8)])
        result = evaluate(payload)
        self.assertEqual(result["exposures"], [])
        self.assertEqual(
            Decimal(str(result["total_degree_minutes"])), Decimal(0)
        )
        self.assertTrue(result["pass"])

    def test_touching_equality_closes_interval(self):
        # above -> exactly threshold -> below: one interval ending at touch
        payload = base_payload([r(0, 10), r(1, 8), r(2, 6)])
        result = evaluate(payload)
        self.assertEqual(len(result["exposures"]), 1)
        self.assertEqual(result["exposures"][0]["end"], "2026-10-05T08:01:00Z")
        self.assertEqual(
            Decimal(str(result["exposures"][0]["duration_seconds"])),
            Decimal("60"),
        )

    def test_upward_crossing_startpoint(self):
        # below -> above starts exactly at the computed intersection
        payload = base_payload([r(0, 6), r(1, 10)])
        result = evaluate(payload)
        iv = result["exposures"][0]
        self.assertEqual(iv["start"], "2026-10-05T08:00:30Z")
        self.assertEqual(iv["end"], "2026-10-05T08:01:00Z")
        self.assertEqual(
            Decimal(str(iv["duration_seconds"])), Decimal("30")
        )

    def test_two_separated_intervals(self):
        payload = base_payload(
            [r(0, 10), r(1, 6), r(2, 6), r(3, 10)]
        )
        result = evaluate(payload)
        self.assertEqual(len(result["exposures"]), 2)
        self.assertEqual(result["exposures"][0]["end"], "2026-10-05T08:00:30Z")
        self.assertEqual(result["exposures"][1]["start"], "2026-10-05T08:02:30Z")

    def test_degree_minutes_trapezoid_decimal(self):
        # threshold 0: readings 1 and 3 over 60 s -> mean excess 2, 2 dm
        payload = base_payload(
            [
                {"timestamp": "2026-10-05T08:00:00Z", "temperature": 1},
                {"timestamp": "2026-10-05T08:01:00Z", "temperature": 3},
            ],
            temperature_threshold=0,
            start_at="2026-10-05T08:00:00Z",
            end_at="2026-10-05T08:01:00Z",
        )
        result = evaluate(payload)
        self.assertEqual(
            Decimal(str(result["total_degree_minutes"])), Decimal("2")
        )


class GapTests(unittest.TestCase):
    def test_gap_blocks_interpolation_and_fails(self):
        payload = base_payload(
            [r(0, 10), r(1, 10), r(15, 10), r(20, 6)],
            max_interval_seconds=600,
        )
        result = evaluate(payload)
        self.assertEqual(len(result["coverage_gaps"]), 1)
        gap = result["coverage_gaps"][0]
        self.assertEqual(gap["start"], "2026-10-05T08:01:00Z")
        self.assertEqual(gap["end"], "2026-10-05T08:15:00Z")
        self.assertEqual(
            Decimal(str(gap["duration_seconds"])), Decimal("840")
        )
        # exposure on the right segment (15->20) must not join the left one
        self.assertEqual(len(result["exposures"]), 2)
        self.assertEqual(result["verdict"], "fail")
        codes = {reason["code"] for reason in result["reasons"]}
        self.assertIn("coverage_gap", codes)

    def test_interval_equal_to_max_is_allowed(self):
        payload = base_payload([r(0, 10), r(10, 6)], max_interval_seconds=600)
        result = evaluate(payload)
        self.assertEqual(result["coverage_gaps"], [])
        self.assertEqual(len(result["exposures"]), 1)


class OrderingAndRepresentationTests(unittest.TestCase):
    READINGS = [r(2, 6), r(0, 10), r(1, 10)]

    def test_input_order_does_not_change_result(self):
        a = evaluate(base_payload(list(self.READINGS)))
        shuffled = [self.READINGS[2], self.READINGS[0], self.READINGS[1]]
        b = evaluate(base_payload(shuffled))
        self.assertEqual(a, b)

    def test_decimal_representation_variants_agree(self):
        # 8.10 / 8.1 / Decimal("8.10") are the same mathematical value; the
        # conclusion must not depend on the textual decimal representation.
        from decimal import Decimal as D

        canonical = base_payload(
            [
                {"timestamp": "2026-10-05T08:00:00Z", "temperature": 8.10},
                {"timestamp": "2026-10-05T08:01:00Z", "temperature": 8.30},
            ],
            temperature_threshold=D("8.10"),
            end_at="2026-10-05T08:01:00Z",
            max_exposure_seconds=60,
        )
        payload_b = {
            **canonical,
            "temperature_threshold": D("8.1"),
            "readings": [
                {"timestamp": "2026-10-05T08:00:00Z",
                 "temperature": D("8.1")},
                {"timestamp": "2026-10-05T08:01:00Z", "temperature": 8.3},
            ],
        }
        ra = evaluate(canonical)
        rb = evaluate(payload_b)
        self.assertEqual(ra["exposures"], rb["exposures"])
        self.assertEqual(ra["total_degree_minutes"], rb["total_degree_minutes"])
        # excess 0 -> 0.2 over 60 s: triangle = 0.2 * 60 / 2 / 60 = 0.1 dm
        self.assertEqual(
            Decimal(str(ra["total_degree_minutes"])), Decimal("0.1")
        )

    def test_timezone_offsets_normalize_to_same_instant(self):
        a = evaluate(base_payload([r(0, 10), r(1, 6)]))
        b = evaluate(
            base_payload(
                [
                    {"timestamp": "2026-10-05T16:00:00+08:00",
                     "temperature": 10},
                    {"timestamp": "2026-10-05T16:01:00+08:00",
                     "temperature": 6},
                ]
            )
        )
        self.assertEqual(a["exposures"], b["exposures"])


class LimitTests(unittest.TestCase):
    def test_single_exposure_limit_exceeded(self):
        payload = base_payload(
            [r(0, 10), r(2, 10)], max_exposure_seconds=60
        )
        result = evaluate(payload)
        self.assertEqual(result["verdict"], "fail")
        codes = {x["code"] for x in result["reasons"]}
        self.assertIn("single_exposure_exceeded", codes)

    def test_single_exposure_limit_boundary_is_ok(self):
        payload = base_payload(
            [r(0, 10), r(1, 10)], max_exposure_seconds=60
        )
        result = evaluate(payload)
        self.assertTrue(result["pass"])

    def test_budget_exceeded(self):
        payload = base_payload(
            [r(0, 10), r(1, 10)], degree_minutes_budget=1
        )
        result = evaluate(payload)
        codes = {x["code"] for x in result["reasons"]}
        self.assertIn("degree_minutes_budget_exceeded", codes)
        self.assertFalse(result["pass"])

    def test_budget_boundary_is_ok(self):
        payload = base_payload(
            [r(0, 10), r(1, 10)], degree_minutes_budget=2
        )
        result = evaluate(payload)
        self.assertTrue(result["pass"])


class ValidationTests(unittest.TestCase):
    def _locs(self, payload):
        with self.assertRaises(ValidationError) as ctx:
            evaluate(payload)
        return {e.loc: e.message for e in ctx.exception.errors}

    def test_duplicate_timestamp_is_located(self):
        locs = self._locs(
            base_payload([r(0, 10), r(0, 8), r(20, 6)])
        )
        self.assertIn("readings[1].timestamp", locs)

    def test_illegal_timestamp_is_located(self):
        readings = [
            {"timestamp": "2026-10-05T08:00:00Z", "temperature": 1},
            {"timestamp": "not-a-time", "temperature": 2},
        ]
        locs = self._locs(base_payload(readings))
        self.assertIn("readings[1].timestamp", locs)

    def test_impossible_calendar_date(self):
        readings = [
            {"timestamp": "2026-10-05T08:00:00Z", "temperature": 1},
            {"timestamp": "2026-13-45T99:00:00Z", "temperature": 2},
        ]
        locs = self._locs(base_payload(readings))
        self.assertIn("readings[1].timestamp", locs)

    def test_missing_timezone_rejected(self):
        readings = [
            {"timestamp": "2026-10-05T08:00:00", "temperature": 1},
            {"timestamp": "2026-10-05T08:20:00Z", "temperature": 2},
        ]
        locs = self._locs(base_payload(readings))
        self.assertIn("readings[0].timestamp", locs)

    def test_non_finite_temperature_located(self):
        locs = self._locs(
            base_payload(
                [
                    {"timestamp": "2026-10-05T08:00:00Z",
                     "temperature": float("nan")},
                    {"timestamp": "2026-10-05T08:20:00Z",
                     "temperature": float("inf")},
                ]
            )
        )
        self.assertIn("readings[0].temperature", locs)
        self.assertIn("readings[1].temperature", locs)

    def test_string_temperature_rejected(self):
        locs = self._locs(
            base_payload(
                [
                    {"timestamp": "2026-10-05T08:00:00Z", "temperature": "8"},
                    {"timestamp": "2026-10-05T08:20:00Z", "temperature": 8},
                ]
            )
        )
        self.assertIn("readings[0].temperature", locs)

    def test_bool_rejected_as_number(self):
        locs = self._locs(
            base_payload(
                [
                    {"timestamp": "2026-10-05T08:00:00Z",
                     "temperature": True},
                    {"timestamp": "2026-10-05T08:20:00Z", "temperature": 8},
                ]
            )
        )
        self.assertIn("readings[0].temperature", locs)

    def test_wrong_reading_count(self):
        locs = self._locs(base_payload([r(0, 8)]))
        self.assertIn("readings", locs)

    def test_endpoints_must_match_boundaries(self):
        locs = self._locs(
            base_payload(
                [r(1, 8), r(20, 8)],
                start_at="2026-10-05T08:00:00Z",
                end_at="2026-10-05T08:20:00Z",
            )
        )
        self.assertIn("readings", locs)

    def test_reading_outside_window_located(self):
        locs = self._locs(
            base_payload(
                [r(0, 8), r(10, 8),
                 {"timestamp": "2026-10-05T08:21:00Z",
                  "temperature": 8},
                 r(20, 8)],
                start_at="2026-10-05T08:00:00Z",
                end_at="2026-10-05T08:20:00Z",
            )
        )
        self.assertIn("readings[2].timestamp", locs)

    def test_missing_scalar_field(self):
        payload = base_payload([r(0, 8), r(20, 8)])
        del payload["max_interval_seconds"]
        locs = self._locs(payload)
        self.assertIn("max_interval_seconds", locs)

    def test_end_before_start(self):
        locs = self._locs(
            base_payload(
                [r(0, 8), r(20, 8)],
                start_at="2026-10-05T09:00:00Z",
                end_at="2026-10-05T08:00:00Z",
            )
        )
        self.assertIn("end_at", locs)


class ParsingTests(unittest.TestCase):
    def test_fractional_seconds_exact(self):
        self.assertEqual(
            parse_rfc3339("2026-10-05T08:00:00.5Z"),
            parse_rfc3339("2026-10-05T08:00:00Z") + Decimal("0.5"),
        )

    def test_offset_equivalence(self):
        self.assertEqual(
            parse_rfc3339("2026-10-05T16:00:00+08:00"),
            parse_rfc3339("2026-10-05T08:00:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
