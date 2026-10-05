"""Tests for the batch HTTP layer (no network: calls normalize_batch)."""

from __future__ import annotations

import json
import unittest

from app.server import (
    MAX_EVENTS,
    RequestError,
    normalize_batch,
)


def enc(obj: object) -> bytes:
    return json.dumps(obj).encode()


class BatchHappyPathTests(unittest.TestCase):
    def test_order_preserved_and_mixed_inputs(self) -> None:
        payload = {
            "events": [
                {"id": "a", "utc": "2016-12-31T23:59:60Z"},
                {"id": 7, "gpsWeek": 1930, "gpsSecondsInWeek": 18},
                {"id": "c", "gpsWeek": 0, "gpsSecondsInWeek": 0,
                 "gpsNanoseconds": 1},
                {"id": "d", "utc": "1972-01-01T00:00:00Z"},
            ]
        }
        out = normalize_batch(enc(payload))
        self.assertEqual([r["id"] for r in out], ["a", "7", "c", "d"])
        self.assertEqual(out[0]["utc"], "2016-12-31T23:59:60Z")
        self.assertEqual(out[0]["utcTaiOffsetSeconds"], "-36")
        self.assertEqual(out[1]["utc"], "2017-01-01T00:00:00Z")
        self.assertEqual(out[1]["utcTaiOffsetSeconds"], "-37")
        self.assertEqual(out[2]["utc"], "1980-01-06T00:00:00.000000001Z")
        self.assertEqual(out[3]["utcTaiOffsetSeconds"], "-10")
        for r in out:
            int(r["taiNanoseconds"])  # decimal string
            self.assertIsInstance(r["taiNanoseconds"], str)

    def test_same_instant_two_inputs_identical_values(self) -> None:
        payload = {
            "events": [
                {"id": "via-gps", "gpsWeek": 1930,
                 "gpsSecondsInWeek": 17, "gpsNanoseconds": 500_000_000},
                {"id": "via-utc", "utc": "2016-12-31T23:59:60.5Z"},
            ]
        }
        out = normalize_batch(enc(payload))
        self.assertEqual(
            out[0]["taiNanoseconds"], out[1]["taiNanoseconds"]
        )
        self.assertEqual(out[0]["utc"], out[1]["utc"])
        self.assertEqual(
            out[0]["utcTaiOffsetSeconds"], out[1]["utcTaiOffsetSeconds"]
        )

    def test_up_to_200_events(self) -> None:
        events = [
            {"id": i, "utc": "2016-12-31T23:59:59Z"}
            for i in range(MAX_EVENTS)
        ]
        self.assertEqual(len(normalize_batch(enc({"events": events}))), 200)


class BatchErrorTests(unittest.TestCase):
    def _expect_error(self, payload: object) -> RequestError:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(payload))
        return cm.exception

    def test_bad_event_reports_id_and_index(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"},
                {"id": "ev-42", "utc": "2016-12-30T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.status, 400)
        self.assertEqual(exc.code, "INVALID_EVENT")
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "ev-42")
        self.assertIn("leap second", exc.message)

    def test_invalid_gps_reports_id_and_index(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "g1", "gpsWeek": 1930,
                 "gpsSecondsInWeek": 604_800},
            ]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "g1")
        self.assertIn("gpsSecondsInWeek", exc.message)

    def test_error_never_carries_partial_results(self) -> None:
        # Normalize raises; callers can only catch the error, and the error
        # has no result payload field.
        payload = {
            "events": [
                {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"},
                {"id": "ok-2", "utc": "2016-12-31T23:59:60Z"},
                {"id": "bad", "utc": "2018-01-01T00:00:00Z"},
                {"id": "ok-4", "utc": "2017-01-01T00:00:00Z"},
            ]
        }
        exc = self._expect_error(payload)
        self.assertFalse(
            hasattr(exc, "results"),
            "error must not contain partial normalization results",
        )
        self.assertNotIn("taiNanoseconds", vars(exc).__repr__())

    def test_duplicate_ids_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "utc": "2016-12-31T23:59:59Z"},
                {"id": "x", "utc": "2016-12-31T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.event_index, 1)
        self.assertIn("duplicate", exc.message.lower())

    def test_duplicate_ids_int_and_string_distinct(self) -> None:
        # "7" and 7 collide because ids identify the same caller entity.
        exc = self._expect_error({
            "events": [
                {"id": 7, "utc": "2016-12-31T23:59:59Z"},
                {"id": "7", "utc": "2016-12-31T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.event_index, 1)

    def test_batch_size_limits(self) -> None:
        exc = self._expect_error({"events": []})
        self.assertIn("between", exc.message)
        exc = self._expect_error({
            "events": [
                {"id": i, "utc": "2016-12-31T23:59:59Z"}
                for i in range(MAX_EVENTS + 1)
            ]
        })
        self.assertIn("200", exc.message)

    def test_malformed_json(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(b"{not json")
        self.assertIn("JSON", cm.exception.message)

    def test_floating_point_numbers_rejected(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(
                b'{"events":[{"id":"x","gpsWeek":1.0,'
                b'"gpsSecondsInWeek":1}]}'
            )
        self.assertIn("floating-point", cm.exception.message)

    def test_both_kinds_or_neither(self) -> None:
        for event in (
            {"id": "x", "utc": "2016-12-31T23:59:59Z",
             "gpsWeek": 1, "gpsSecondsInWeek": 1},
            {"id": "x"},
        ):
            with self.subTest(event=event):
                self._expect_error({"events": [event]})

    def test_unknown_field_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "utc": "2016-12-31T23:59:59Z",
                 "leapSeconds": 37},
            ]
        })
        self.assertIn("unknown", exc.message)

    def test_string_gps_fields_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "gpsWeek": "1930",
                 "gpsSecondsInWeek": 17},
            ]
        })
        self.assertEqual(exc.event_id, "x")
        self.assertIn("gpsWeek", exc.message)

    def test_missing_id(self) -> None:
        exc = self._expect_error({
            "events": [{"utc": "2016-12-31T23:59:59Z"}]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertIn("id", exc.message)

    def test_first_invalid_event_wins_no_side_effects(self) -> None:
        # Even when later events are also invalid, the first failure is
        # reported and nothing is returned.
        exc = self._expect_error({
            "events": [
                {"id": 1, "utc": "not-a-time"},
                {"id": 2, "utc": "also-bad"},
            ]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "1")


class BatchModuloWeekTests(unittest.TestCase):
    def test_cross_era_batch_order_and_resolved_week(self) -> None:
        payload = {
            "events": [
                {"id": "era-1997", "gpsWeekModulo": 906,
                 "gpsSecondsInWeek": 0,
                 "referenceUtc": "1997-05-25T12:00:00Z"},
                {"id": "utc-twin", "utc": "1997-05-17T23:59:49Z"},
                {"id": "era-2017", "gpsWeekModulo": 906,
                 "gpsSecondsInWeek": 0,
                 "referenceUtc": "2017-01-01T00:00:00Z"},
                {"id": "full-twin", "gpsWeek": 1930, "gpsSecondsInWeek": 0},
            ]
        }
        out = normalize_batch(enc(payload))
        self.assertEqual(
            [r["id"] for r in out],
            ["era-1997", "utc-twin", "era-2017", "full-twin"],
        )
        # Only modulo events echo the resolved full week.
        self.assertEqual(out[0]["resolvedGpsWeek"], "906")
        self.assertEqual(out[2]["resolvedGpsWeek"], "1930")
        self.assertNotIn("resolvedGpsWeek", out[1])
        self.assertNotIn("resolvedGpsWeek", out[3])
        # Resolved instants are identical to their full-week/UTC twins.
        self.assertEqual(out[0]["taiNanoseconds"], "863913619000000000")
        self.assertEqual(out[0]["utc"], "1997-05-17T23:59:49Z")
        self.assertEqual(out[0]["taiNanoseconds"], out[1]["taiNanoseconds"])
        self.assertEqual(out[2]["taiNanoseconds"], "1483228819000000000")
        self.assertEqual(out[2]["utc"], out[3]["utc"])
        self.assertEqual(
            out[2]["taiNanoseconds"], out[3]["taiNanoseconds"]
        )
        self.assertIsInstance(out[0]["resolvedGpsWeek"], str)

    def test_modulo_defaults_nanos_to_zero(self) -> None:
        payload = {"events": [
            {"id": "m", "gpsWeekModulo": 906, "gpsSecondsInWeek": 18,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]}
        out = normalize_batch(enc(payload))
        self.assertEqual(out[0]["utc"], "2017-01-01T00:00:00Z")
        self.assertEqual(out[0]["resolvedGpsWeek"], "1930")

    def _expect_error(self, payload: object) -> RequestError:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(payload))
        return cm.exception

    def test_field_mixing_rejected(self) -> None:
        cases = [
            # utc mixed with modulo fields
            {"id": "x", "utc": "2017-01-01T00:00:00Z",
             "gpsWeekModulo": 906, "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
            # utc mixed with a bare referenceUtc
            {"id": "x", "utc": "2017-01-01T00:00:00Z",
             "referenceUtc": "2016-01-01T00:00:00Z"},
            # full week mixed with modulo week
            {"id": "x", "gpsWeek": 1930, "gpsWeekModulo": 906,
             "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
            # full week mixed with referenceUtc
            {"id": "x", "gpsWeek": 1930, "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
            # referenceUtc without gpsWeekModulo
            {"id": "x", "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]
        for event in cases:
            with self.subTest(event=event):
                exc = self._expect_error({"events": [event]})
                self.assertEqual(exc.code, "INVALID_EVENT")
                self.assertEqual(exc.event_id, "x")

    def test_modulo_missing_fields(self) -> None:
        exc = self._expect_error({"events": [
            {"id": "m1", "gpsWeekModulo": 906, "gpsSecondsInWeek": 0},
        ]})
        self.assertIn("referenceUtc", exc.message)
        exc = self._expect_error({"events": [
            {"id": "m2", "gpsWeekModulo": 906,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]})
        self.assertIn("gpsSecondsInWeek", exc.message)

    def test_modulo_type_and_range_errors_locate_event(self) -> None:
        exc = self._expect_error({"events": [
            {"id": "ok", "utc": "2016-12-31T23:59:59Z"},
            {"id": "bad", "gpsWeekModulo": "906", "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]})
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "bad")
        self.assertIn("gpsWeekModulo", exc.message)

        exc = self._expect_error({"events": [
            {"id": "bad-range", "gpsWeekModulo": 1024,
             "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]})
        self.assertEqual(exc.event_id, "bad-range")
        self.assertIn("[0, 1023]", exc.message)

    def test_modulo_unresolvable_batches_fail_wholesale(self) -> None:
        # No candidate inside the era within 512 weeks of the reference.
        exc = self._expect_error({"events": [
            {"id": "ok", "gpsWeek": 1930, "gpsSecondsInWeek": 18},
            {"id": "no-cand", "gpsWeekModulo": 1000,
             "gpsSecondsInWeek": 0,
             "referenceUtc": "2017-01-01T00:00:00Z"},
        ]})
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "no-cand")
        self.assertIn("no GPS week congruent to 1000", exc.message)
        self.assertFalse(hasattr(exc, "results"))

        # Exactly at the 512-week rollover midpoint.
        exc = self._expect_error({"events": [
            {"id": "midpoint", "gpsWeekModulo": 906,
             "gpsSecondsInWeek": 0,
             "referenceUtc": "2007-03-10T23:59:46Z"},
        ]})
        self.assertEqual(exc.event_id, "midpoint")
        self.assertIn("exactly 512 weeks", exc.message)

        # Reference instant outside the supported era.
        exc = self._expect_error({"events": [
            {"id": "ref-out", "gpsWeekModulo": 1, "gpsSecondsInWeek": 0,
             "referenceUtc": "2020-01-01T00:00:00Z"},
        ]})
        self.assertEqual(exc.event_id, "ref-out")
        self.assertIn("2017-06-28", exc.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
