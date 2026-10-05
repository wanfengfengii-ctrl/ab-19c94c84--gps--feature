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


class GpsModuloBatchTests(unittest.TestCase):
    MOD_EVENT = {
        "id": "tape-1", "gpsWeekModulo": 906,
        "gpsSecondsInWeek": 17, "gpsNanoseconds": 500_000_000,
        "referenceUtc": "2017-01-02T00:00:00Z",
    }

    def _expect_error(self, payload: object) -> RequestError:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(payload))
        return cm.exception

    def test_modulo_happy_path_echoes_resolved_week(self) -> None:
        out = normalize_batch(enc({"events": [dict(self.MOD_EVENT)]}))
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["id"], "tape-1")
        self.assertEqual(r["resolvedGpsWeek"], 1930)
        self.assertEqual(r["taiNanoseconds"], "1483228836500000000")
        self.assertEqual(r["utc"], "2016-12-31T23:59:60.5Z")
        self.assertEqual(r["utcTaiOffsetSeconds"], "-36")
        self.assertEqual(
            set(r),
            {"id", "resolvedGpsWeek", "taiNanoseconds", "utc",
             "utcTaiOffsetSeconds"},
        )

    def test_modulo_other_epoch_same_modulo(self) -> None:
        ev = dict(self.MOD_EVENT, referenceUtc="1997-05-20T00:00:00Z")
        out = normalize_batch(enc({"events": [ev]}))
        self.assertEqual(out[0]["resolvedGpsWeek"], 906)
        self.assertEqual(out[0]["utc"], "1997-05-18T00:00:06.5Z")
        self.assertEqual(out[0]["utcTaiOffsetSeconds"], "-30")

    def test_nanoseconds_optional_and_order_preserved(self) -> None:
        payload = {"events": [
            {"id": "utc-a", "utc": "2016-12-31T23:59:60Z"},
            {"id": "mod-b", "gpsWeekModulo": 906,
             "gpsSecondsInWeek": 0, "referenceUtc": "1997-06-01T00:00:00Z"},
            {"id": "full-c", "gpsWeek": 1930, "gpsSecondsInWeek": 18},
        ]}
        out = normalize_batch(enc(payload))
        self.assertEqual([r["id"] for r in out], ["utc-a", "mod-b", "full-c"])
        self.assertEqual(out[1]["resolvedGpsWeek"], 906)
        self.assertNotIn("resolvedGpsWeek", out[0])
        self.assertNotIn("resolvedGpsWeek", out[2])

    def test_modulo_equivalent_to_full_week_and_utc(self) -> None:
        payload = {"events": [
            dict(self.MOD_EVENT, id="via-mod"),
            {"id": "via-full", "gpsWeek": 1930,
             "gpsSecondsInWeek": 17, "gpsNanoseconds": 500_000_000},
            {"id": "via-utc", "utc": "2016-12-31T23:59:60.5Z"},
        ]}
        out = normalize_batch(enc(payload))
        self.assertEqual(
            out[0]["taiNanoseconds"], out[1]["taiNanoseconds"]
        )
        self.assertEqual(
            out[0]["taiNanoseconds"], out[2]["taiNanoseconds"]
        )
        self.assertEqual(out[0]["utc"], out[2]["utc"])

    def test_field_mixing_rejected(self) -> None:
        bad_events = [
            dict(self.MOD_EVENT, id="m1", utc="2016-12-31T23:59:59Z"),
            dict(self.MOD_EVENT, id="m2", gpsWeek=1930),
            {"id": "m3", "gpsWeek": 1930, "gpsWeekModulo": 906,
             "gpsSecondsInWeek": 1, "referenceUtc": "2010-01-01T00:00:00Z"},
            {"id": "m4", "utc": "2016-12-31T23:59:59Z",
             "referenceUtc": "2010-01-01T00:00:00Z"},
        ]
        for ev in bad_events:
            with self.subTest(ev=ev["id"]):
                exc = self._expect_error({"events": [ev]})
                self.assertEqual(exc.code, "INVALID_EVENT")
                self.assertEqual(exc.event_index, 0)
                self.assertEqual(exc.event_id, ev["id"])

    def test_missing_modulo_fields(self) -> None:
        bases = {
            "no-mod": {"id": "e", "gpsSecondsInWeek": 17,
                       "referenceUtc": "2017-01-02T00:00:00Z"},
            "no-sow": {"id": "e", "gpsWeekModulo": 906,
                       "referenceUtc": "2017-01-02T00:00:00Z"},
            "no-ref": {"id": "e", "gpsWeekModulo": 906,
                       "gpsSecondsInWeek": 17},
        }
        for label, ev in bases.items():
            with self.subTest(label=label):
                exc = self._expect_error({"events": [ev]})
                self.assertEqual(exc.event_id, "e")
                self.assertEqual(exc.event_index, 0)

    def test_modulo_out_of_range_indexed(self) -> None:
        ev = dict(self.MOD_EVENT, id="bad-mod", gpsWeekModulo=1024)
        exc = self._expect_error({"events": [
            {"id": "ok", "utc": "2016-12-31T23:59:59Z"}, ev,
        ]})
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "bad-mod")
        self.assertIn("gpsWeekModulo", exc.message)

    def test_no_candidate_indexed_no_partial_results(self) -> None:
        ev = dict(self.MOD_EVENT, id="no-cand",
                  gpsWeekModulo=931, gpsSecondsInWeek=259_218,
                  gpsNanoseconds=0, referenceUtc="2017-06-26T00:00:00Z")
        exc = self._expect_error({"events": [
            {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"}, ev,
            {"id": "ok-3", "utc": "2017-01-01T00:00:00Z"},
        ]})
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "no-cand")
        self.assertIn("no GPS week congruent", exc.message)
        self.assertFalse(hasattr(exc, "results"))

    def test_exact_boundary_indexed(self) -> None:
        ev = {
            "id": "edge", "gpsWeekModulo": 906,
            "gpsSecondsInWeek": 100, "gpsNanoseconds": 200,
            "referenceUtc": "2007-03-11T00:01:26.0000002Z",
        }
        exc = self._expect_error({"events": [ev]})
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "edge")
        self.assertIn("512-week", exc.message)

    def test_modulo_integer_literal_enforced(self) -> None:
        for field, value in (
            ("gpsWeekModulo", "906"),
            ("gpsSecondsInWeek", "17"),
        ):
            with self.subTest(field=field):
                ev = dict(self.MOD_EVENT, **{field: value})
                exc = self._expect_error({"events": [ev]})
                self.assertEqual(exc.event_id, "tape-1")
                self.assertIn(field, exc.message)

    def test_reference_utc_must_be_string(self) -> None:
        ev = dict(self.MOD_EVENT, referenceUtc=2017)
        exc = self._expect_error({"events": [ev]})
        self.assertEqual(exc.event_id, "tape-1")
        self.assertIn("referenceUtc", exc.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
