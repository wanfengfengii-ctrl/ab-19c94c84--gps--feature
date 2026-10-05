"""Tests for the leap-second conversion core.

Run with:  python -m unittest discover -s tests -v

Independent reference values (Unix timestamps / offsets) come from the
IANA tzdb leap-seconds.list table and IERS Bulletin C 52; nothing here
reads the production table to produce its own expected values.
"""

from __future__ import annotations

import random
import unittest

from app.timecore import (
    LEAP_BOUNDS,
    MIN_SUPPORTED_TAI_NS,
    NS_PER_SECOND,
    NS_PER_WEEK,
    SECONDS_PER_WEEK,
    TABLE_EXPIRY_TAI_NS,
    NormalizedEvent,
    TimeConversionError,
    normalize_gps_event,
    normalize_utc_event,
    resolve_gps_modulo_event,
    tai_to_utc,
    utc_to_tai,
)
from app.timecore import (
    _check_civil_validity,
    _civil_from_days,
    _days_from_civil,
    _format_utc,
    GPS_EPOCH_TAI_NS,
)


# Unix (UTC label) timestamps of independent reference instants.
UNIX_2016_PRE = 1_483_228_799   # 2016-12-31T23:59:59Z
UNIX_2017_JAN1 = 1_483_228_800  # 2017-01-01T00:00:00Z
UNIX_EXPIRY = 1_498_608_000     # 2017-06-28T00:00:00Z


class KnownVectorTests(unittest.TestCase):
    def test_2016_leap_triple(self) -> None:
        pre = normalize_utc_event("2016-12-31T23:59:59Z")
        leap = normalize_utc_event("2016-12-31T23:59:60Z")
        post = normalize_utc_event("2017-01-01T00:00:00Z")
        self.assertEqual(pre.tai_ns, (UNIX_2016_PRE + 36) * NS_PER_SECOND)
        self.assertEqual(leap.tai_ns, pre.tai_ns + NS_PER_SECOND)
        self.assertEqual(post.tai_ns, (UNIX_2017_JAN1 + 37) * NS_PER_SECOND)
        self.assertEqual(post.tai_ns - leap.tai_ns, NS_PER_SECOND)
        # Old offset (-36) applies *during* the leap second.
        self.assertEqual(
            (pre.utc_minus_tai, leap.utc_minus_tai, post.utc_minus_tai),
            (-36, -36, -37),
        )

    def test_gps_epoch(self) -> None:
        g = normalize_gps_event(0, 0, 0)
        self.assertEqual(g.utc_canonical, "1980-01-06T00:00:00Z")
        self.assertEqual(g.utc_minus_tai, -19)
        self.assertEqual(g.tai_ns, 315_964_819 * NS_PER_SECOND)

    def test_2016_leap_via_gps(self) -> None:
        # GPS runs 18 s ahead of UTC after the 2016 leap: UTC midnight
        # 2017-01-01 is GPS week 1930, SOW 18; the leap second is SOW 17.
        post = normalize_utc_event("2017-01-01T00:00:00Z")
        leap = normalize_utc_event("2016-12-31T23:59:60Z")
        pre = normalize_utc_event("2016-12-31T23:59:59Z")
        self.assertEqual(normalize_gps_event(1930, 18, 0).tai_ns, post.tai_ns)
        self.assertEqual(
            normalize_gps_event(1930, 17, 0),
            NormalizedEvent(leap.tai_ns, "2016-12-31T23:59:60Z", -36),
        )
        self.assertEqual(normalize_gps_event(1930, 16, 0).tai_ns, pre.tai_ns)

    def test_first_table_rows_1972(self) -> None:
        self.assertEqual(
            normalize_utc_event("1972-01-01T00:00:00Z").utc_minus_tai, -10
        )
        self.assertEqual(
            normalize_utc_event("1972-06-30T23:59:60Z").utc_minus_tai, -10
        )
        self.assertEqual(
            normalize_utc_event("1972-07-01T00:00:00Z").utc_minus_tai, -11
        )

    def test_table_shape(self) -> None:
        # 28 rows: opening row 1972 (offset 10) plus 27 positive leaps.
        self.assertEqual(len(LEAP_BOUNDS), 28)
        self.assertEqual(LEAP_BOUNDS[0].offset, 10)
        self.assertEqual(LEAP_BOUNDS[-1].offset, 37)
        self.assertEqual(LEAP_BOUNDS[-1].date, (2017, 1, 1))
        for a, b in zip(LEAP_BOUNDS, LEAP_BOUNDS[1:]):
            self.assertEqual(b.offset - a.offset, 1)
            self.assertGreater(b.tai_ns, a.tai_ns)

    def test_expiry_reference(self) -> None:
        self.assertEqual(
            TABLE_EXPIRY_TAI_NS, (UNIX_EXPIRY + 37) * NS_PER_SECOND
        )
        last_ok = normalize_utc_event("2017-06-27T23:59:59.999999999Z")
        self.assertEqual(last_ok.tai_ns, TABLE_EXPIRY_TAI_NS - 1)


class CrossInputEquivalenceTests(unittest.TestCase):
    def _gps_for_tai(self, tai: int) -> tuple[int, int, int]:
        raw = tai - GPS_EPOCH_TAI_NS
        w, rem = divmod(raw, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow, ns = divmod(rem, NS_PER_SECOND)
        return w, sow, ns

    def test_same_instant_identical_results_2016(self) -> None:
        via_utc = normalize_utc_event("2016-12-31T23:59:60.5Z")
        via_gps = normalize_gps_event(1930, 17, 500_000_000)
        self.assertEqual(via_utc, via_gps)
        self.assertEqual(via_utc.utc_canonical, "2016-12-31T23:59:60.5Z")
        # Serialized TAI values must be byte-identical decimal strings.
        self.assertEqual(
            via_utc.to_response()["taiNanoseconds"],
            via_gps.to_response()["taiNanoseconds"],
        )
        self.assertEqual(
            via_utc.to_response()["utcTaiOffsetSeconds"],
            via_gps.to_response()["utcTaiOffsetSeconds"],
        )

    def test_random_instant_equivalence(self) -> None:
        rng = random.Random(20261005)
        checked = 0
        while checked < 3000:
            tai = rng.randrange(
                GPS_EPOCH_TAI_NS, TABLE_EXPIRY_TAI_NS
            )  # GPS cannot express pre-1980
            w, sow, ns = self._gps_for_tai(tai)
            utc_text = _format_utc(*tai_to_utc(tai)[:7])
            via_gps = normalize_gps_event(w, sow, ns)
            via_utc = normalize_utc_event(utc_text)
            self.assertEqual(via_gps.tai_ns, tai)
            self.assertEqual(via_utc.tai_ns, tai)
            self.assertEqual(via_gps.utc_canonical, via_utc.utc_canonical)
            self.assertEqual(
                via_gps.utc_minus_tai, via_utc.utc_minus_tai
            )
            self.assertEqual(via_gps.to_response(), via_utc.to_response())
            checked += 1


class RoundTripTests(unittest.TestCase):
    def test_all_leap_days_canonical(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            py, pm, pd = _civil_from_days(
                _days_from_civil(*b.date) - 1
            )
            pre = normalize_utc_event(
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:59Z"
            )
            leap = normalize_utc_event(
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:60.123456789Z"
            )
            post = normalize_utc_event(
                f"{b.date[0]:04d}-{b.date[1]:02d}-{b.date[2]:02d}"
                "T00:00:00Z"
            )
            self.assertEqual(
                leap.utc_canonical,
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:60.123456789Z",
            )
            self.assertEqual(
                leap.tai_ns - pre.tai_ns, 1_123_456_789
            )
            self.assertEqual(post.tai_ns - leap.tai_ns, 876_543_211)
            self.assertEqual(leap.utc_minus_tai, -(b.offset - 1))
            self.assertEqual(post.utc_minus_tai, -b.offset)

    def test_dense_nanoseconds_around_every_edge(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            for edge in (b.tai_ns - NS_PER_SECOND, b.tai_ns):
                lo, hi = edge - 500, edge + 500
                for tai in range(lo, hi):
                    comps = tai_to_utc(tai)
                    _check_civil_validity(*comps[:6])
                    self.assertEqual(utc_to_tai(*comps[:7]), tai)

    def test_whole_seconds_around_every_boundary(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            for k in range(-5, 6):
                tai = b.tai_ns + k * NS_PER_SECOND
                if tai < MIN_SUPPORTED_TAI_NS:
                    continue
                comps = tai_to_utc(tai)
                self.assertEqual(utc_to_tai(*comps[:7]), tai)

    def test_coarse_sweep_of_whole_era(self) -> None:
        t = MIN_SUPPORTED_TAI_NS
        step = 7919 * NS_PER_SECOND + 123_456_789
        while t < TABLE_EXPIRY_TAI_NS:
            comps = tai_to_utc(t)
            self.assertEqual(utc_to_tai(*comps[:7]), t)
            t += step

    def test_canonical_fraction_formatting(self) -> None:
        cases = {
            "2016-12-31T23:59:59.0Z": "2016-12-31T23:59:59Z",
            "2016-12-31T23:59:59.500Z": "2016-12-31T23:59:59.5Z",
            "2016-12-31T23:59:59.000000001Z":
                "2016-12-31T23:59:59.000000001Z",
            "2016-12-31T23:59:60.999999999Z":
                "2016-12-31T23:59:60.999999999Z",
        }
        for raw, canon in cases.items():
            self.assertEqual(
                normalize_utc_event(raw).utc_canonical, canon
            )


class RejectionTests(unittest.TestCase):
    def assert_rejected_utc(self, text: object) -> None:
        with self.assertRaises(TimeConversionError):
            normalize_utc_event(text)

    def test_invalid_calendar_dates(self) -> None:
        for text in (
            "2017-02-29T00:00:00Z",   # 2017 not a leap year
            "2016-02-30T12:00:00Z",
            "2016-04-31T00:00:00Z",
            "2016-13-01T00:00:00Z",
            "2016-00-01T00:00:00Z",
            "2016-12-00T00:00:00Z",
            "0000-01-01T00:00:00Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_invalid_clock_fields(self) -> None:
        for text in (
            "2016-12-31T24:00:00Z",
            "2016-12-31T23:60:00Z",
            "2016-12-31T23:59:61Z",
            "2016-12-31T23:59:62Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_fake_leap_second_positions(self) -> None:
        for text in (
            "2016-12-30T23:59:60Z",   # day before the leap day
            "2016-12-31T23:58:60Z",   # wrong minute
            "2016-12-31T22:59:60Z",   # wrong hour
            "2017-01-01T00:00:60Z",   # after boundary
            "2000-01-01T23:59:60Z",   # 2000 had no leap second
            "2008-06-30T23:59:60Z",   # 2008 leap was Dec 31
            "1971-12-31T23:59:60Z",   # before table era
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_real_leap_second_positions_accepted(self) -> None:
        for text in (
            "2008-12-31T23:59:60Z",
            "2015-06-30T23:59:60Z",
            "2016-12-31T23:59:60Z",
            "1998-12-31T23:59:60Z",
            "1972-06-30T23:59:60Z",
        ):
            normalize_utc_event(text)  # must not raise

    def test_unsupported_era(self) -> None:
        for text in (
            "1971-12-31T23:59:59Z",
            "1900-01-01T00:00:00Z",
            "2017-06-28T00:00:00Z",
            "2020-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_malformed_strings(self) -> None:
        for text in (
            "2016-12-31T23:59:60",      # no Z
            "2016-12-31 23:59:59Z",     # space separator
            "23:59:60Z",
            "2016-12-31T23:59Z",
            "2016-12-31T23:59:59+00:00",
            "2016-12-31T23:59:59.1e3Z",
            "2016-12-31T23:59:59.1234567891Z",  # > 9 frac digits
            "",
            "garbage",
            None,
            42,
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_gps_out_of_range(self) -> None:
        for week, sow, ns in (
            (0, 604_800, 0),
            (0, -1, 0),
            (0, 0, -1),
            (0, 0, 1_000_000_000),
            (-1, 0, 0),
            (3000, 0, 0),   # far beyond table expiry
        ):
            with self.subTest(week=week, sow=sow, ns=ns):
                with self.assertRaises(TimeConversionError):
                    normalize_gps_event(week, sow, ns)

    def test_gps_types(self) -> None:
        for week, sow in (("0", 0), (0, 1.0), (1.5, 0), (True, 0), (0, None)):
            with self.subTest(week=week, sow=sow):
                with self.assertRaises(TimeConversionError):
                    normalize_gps_event(week, sow)  # type: ignore[arg-type]

    def test_gps_at_expiry_rejected(self) -> None:
        # Smallest GPS week/SOW at/after 2017-06-28T00:00:00Z.
        raw = TABLE_EXPIRY_TAI_NS - GPS_EPOCH_TAI_NS
        w, rem = divmod(raw, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow, ns = divmod(rem, NS_PER_SECOND)
        with self.assertRaises(TimeConversionError):
            normalize_gps_event(w, sow, ns)
        # one ns earlier is fine
        w0, rem0 = divmod(raw - 1, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow0, ns0 = divmod(rem0, NS_PER_SECOND)
        normalize_gps_event(w0, sow0, ns0)


class NoFloatingPointTests(unittest.TestCase):
    def test_responses_use_decimal_strings(self) -> None:
        r = normalize_utc_event("2016-12-31T23:59:60Z").to_response()
        for key in ("taiNanoseconds", "utcTaiOffsetSeconds"):
            self.assertIsInstance(r[key], str)
            int(r[key])  # purely decimal

    def test_source_contains_no_float_arithmetic(self) -> None:
        import ast
        import inspect
        import app.timecore as tc

        tree = ast.parse(inspect.getsource(tc))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, ast.Div
            ):
                self.fail(f"true-division operator at line {node.lineno}")
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name == "float":
                    self.fail(f"float() call at line {node.lineno}")


class GpsModuloResolutionTests(unittest.TestCase):
    # Week 906 (1997) and week 1930 (2016) share modulo 906: at SOW 17,
    # 500_000_000 ns they are, respectively,
    #   1997-05-18T00:00:06.5Z (UTC-TAI -30) and the 2016 leap second
    #   2016-12-31T23:59:60.5Z (UTC-TAI -36),
    # exactly 1024 GPS weeks apart in TAI.
    M906_SOW = 17
    M906_NS = 500_000_000
    WEEK_906_TAI = 863_913_636_500_000_000
    WEEK_1930_TAI = 1_483_228_836_500_000_000

    def test_resolves_to_each_epoch_by_reference(self) -> None:
        late = resolve_gps_modulo_event(
            906, self.M906_SOW, self.M906_NS, "2017-01-02T00:00:00Z"
        )
        early = resolve_gps_modulo_event(
            906, self.M906_SOW, self.M906_NS, "1997-05-20T00:00:00Z"
        )
        self.assertEqual(late.resolved_gps_week, 1930)
        self.assertEqual(early.resolved_gps_week, 906)
        self.assertEqual(late.tai_ns, self.WEEK_1930_TAI)
        self.assertEqual(early.tai_ns, self.WEEK_906_TAI)
        self.assertEqual(late.utc_canonical, "2016-12-31T23:59:60.5Z")
        self.assertEqual(early.utc_canonical, "1997-05-18T00:00:06.5Z")
        self.assertEqual(late.utc_minus_tai, -36)
        self.assertEqual(early.utc_minus_tai, -30)

    def test_resolved_event_equals_full_week_and_utc_event(self) -> None:
        via_mod = resolve_gps_modulo_event(
            906, self.M906_SOW, self.M906_NS, "2017-01-02T00:00:00Z"
        )
        via_full = normalize_gps_event(1930, self.M906_SOW, self.M906_NS)
        via_utc = normalize_utc_event("2016-12-31T23:59:60.5Z")
        self.assertEqual(via_mod.tai_ns, via_full.tai_ns)
        self.assertEqual(via_mod.tai_ns, via_utc.tai_ns)
        self.assertEqual(via_mod.utc_canonical, via_utc.utc_canonical)
        self.assertEqual(via_mod.utc_minus_tai, via_utc.utc_minus_tai)
        body = via_mod.to_response()
        self.assertEqual(body["resolvedGpsWeek"], 1930)
        self.assertIsInstance(body["resolvedGpsWeek"], int)
        # A plain full-week GPS event keeps its original response shape.
        self.assertNotIn("resolvedGpsWeek", via_full.to_response())
        self.assertNotIn("resolvedGpsWeek", via_utc.to_response())

    def test_reference_exactly_at_event_instant_resolves_it(self) -> None:
        # Distance zero is unambiguously inside the 512-week window even
        # when the reference label is itself a leap second.
        r = resolve_gps_modulo_event(
            906, self.M906_SOW, self.M906_NS, "2016-12-31T23:59:60.5Z"
        )
        self.assertEqual(r.resolved_gps_week, 1930)
        r = resolve_gps_modulo_event(
            906, self.M906_SOW, self.M906_NS, "1997-05-18T00:00:06.5Z"
        )
        self.assertEqual(r.resolved_gps_week, 906)

    def test_nanoseconds_optional(self) -> None:
        r = resolve_gps_modulo_event(
            906, 0, 0, "2017-01-01T00:00:00Z"
        )
        self.assertEqual(
            r, NormalizedEvent(
                tai_ns=normalize_gps_event(1930, 0, 0).tai_ns,
                utc_canonical="2016-12-31T23:59:43Z",
                utc_minus_tai=-36,
                resolved_gps_week=1930,
            )
        )

    def test_resolved_full_week_may_be_negative(self) -> None:
        # GPS existed on paper at week 0 (1980); a tape from the early
        # 1970s resolves to a negative full week that is still inside the
        # leap-second table era.  modulo 700 -> week -324 (1973).
        r = resolve_gps_modulo_event(
            700, 200_000, 1, "1973-10-25T00:00:00Z"
        )
        self.assertEqual(r.resolved_gps_week, -324)
        self.assertEqual(r.utc_canonical, "1973-10-23T07:33:27.000000001Z")
        expected_tai = (
            GPS_EPOCH_TAI_NS - 324 * NS_PER_WEEK
            + 200_000 * NS_PER_SECOND + 1
        )
        self.assertEqual(r.tai_ns, expected_tai)

    def test_exact_512_week_boundary_is_ambiguous(self) -> None:
        # Reference exactly midway between the week-906 and week-1930
        # candidates (SOW 100, 200 ns): both sit at a distance of exactly
        # 512 weeks in TAI nanoseconds, which the strict < rule rejects.
        midpoint_tai = (
            GPS_EPOCH_TAI_NS + 906 * NS_PER_WEEK
            + 100 * NS_PER_SECOND + 200 + 512 * NS_PER_WEEK
        )
        midpoint = _format_utc(*tai_to_utc(midpoint_tai)[:7])
        self.assertEqual(midpoint, "2007-03-11T00:01:26.0000002Z")
        with self.assertRaises(TimeConversionError) as cm:
            resolve_gps_modulo_event(906, 100, 200, midpoint)
        msg = str(cm.exception)
        self.assertIn("512-week", msg)
        self.assertIn("906", msg)
        self.assertIn("1930", msg)

    def test_one_nanosecond_off_boundary_resolves_uniquely(self) -> None:
        midpoint_tai = (
            GPS_EPOCH_TAI_NS + 906 * NS_PER_WEEK
            + 100 * NS_PER_SECOND + 200 + 512 * NS_PER_WEEK
        )
        before = _format_utc(*tai_to_utc(midpoint_tai - 1)[:7])
        after = _format_utc(*tai_to_utc(midpoint_tai + 1)[:7])
        self.assertEqual(
            resolve_gps_modulo_event(906, 100, 200, before).resolved_gps_week,
            906,
        )
        self.assertEqual(
            resolve_gps_modulo_event(906, 100, 200, after).resolved_gps_week,
            1930,
        )

    def test_no_candidate_beyond_table_expiry(self) -> None:
        # Week 1955 (modulo 931) starts 2017-06-24 and straddles the
        # 2017-06-28 expiry: at SOW 259218 the candidate instant is exactly
        # at the expiry (out of era), and the previous congruent week 931
        # is ~1024 weeks farther from the reference.  No candidate remains.
        with self.assertRaises(TimeConversionError) as cm:
            resolve_gps_modulo_event(
                931, 259_218, 0, "2017-06-26T00:00:00Z"
            )
        self.assertIn("no GPS week congruent", str(cm.exception))
        # One second earlier the same week is inside the era and resolves.
        ok = resolve_gps_modulo_event(
            931, 259_217, 0, "2017-06-26T00:00:00Z"
        )
        self.assertEqual(ok.resolved_gps_week, 1955)
        self.assertEqual(ok.utc_canonical, "2017-06-27T23:59:59Z")

    def test_no_candidate_before_table_start(self) -> None:
        # Modulo 594: nearest congruent weeks are -430 (1971, before the
        # table) and 594 (1991).  A 1973 reference is ~86 weeks from the
        # former -- out of era -- and ~938 weeks from the latter.
        with self.assertRaises(TimeConversionError) as cm:
            resolve_gps_modulo_event(
                594, 0, 0, "1973-06-01T00:00:00Z"
            )
        self.assertIn("no GPS week congruent", str(cm.exception))

    def test_boundary_rejected_even_when_twin_is_out_of_era(self) -> None:
        # modulo 594, reference exactly 512 weeks after the out-of-era
        # week -430: its distance is exactly 512 weeks (strict < fails),
        # and the other congruent week is another 512 weeks farther.
        boundary_tai = (
            GPS_EPOCH_TAI_NS - 430 * NS_PER_WEEK + 512 * NS_PER_WEEK
        )
        boundary = _format_utc(*tai_to_utc(boundary_tai)[:7])
        self.assertEqual(boundary, "1981-08-01T23:59:59Z")
        with self.assertRaises(TimeConversionError) as cm:
            resolve_gps_modulo_event(594, 0, 0, boundary)
        self.assertIn("512-week", str(cm.exception))

    def test_exhaustive_integer_distance_check(self) -> None:
        # For thousands of random in-era congruent instants (including
        # negative full weeks) and references offset by an exact integer
        # number of nanoseconds, resolution must recover the true full
        # week; at the strict +/-512-week boundary it must refuse.
        rng = random.Random(424242)
        half = 512 * NS_PER_WEEK
        checked = 0
        while checked < 4000:
            w = rng.randrange(-500, 1956)
            sow = rng.randrange(SECONDS_PER_WEEK)
            ns = rng.randrange(NS_PER_SECOND)
            tai = (
                GPS_EPOCH_TAI_NS + w * NS_PER_WEEK
                + sow * NS_PER_SECOND + ns
            )
            if not (MIN_SUPPORTED_TAI_NS <= tai < TABLE_EXPIRY_TAI_NS):
                continue
            mod = w % 1024
            for _ in range(4):
                delta = rng.randrange(-half + 1, half)  # strict interior
                ref_tai = tai + delta
                if not (
                    MIN_SUPPORTED_TAI_NS <= ref_tai < TABLE_EXPIRY_TAI_NS
                ):
                    continue
                ref_text = _format_utc(*tai_to_utc(ref_tai)[:7])
                r = resolve_gps_modulo_event(mod, sow, ns, ref_text)
                self.assertEqual(r.resolved_gps_week, w)
                self.assertEqual(r.tai_ns, tai)
                checked += 1
            # Exact boundary (both congruent instants in era) must fail.
            twin = tai + (1024 * NS_PER_WEEK)
            if MIN_SUPPORTED_TAI_NS <= twin < TABLE_EXPIRY_TAI_NS:
                ref_text = _format_utc(*tai_to_utc(tai + half)[:7])
                with self.assertRaises(TimeConversionError):
                    resolve_gps_modulo_event(mod, sow, ns, ref_text)
        self.assertGreater(checked, 3000)

    def test_modulo_range_and_types(self) -> None:
        good_ref = "2010-01-01T00:00:00Z"
        for mod, sow, ns, ref in (
            (1024, 0, 0, good_ref),
            (-1, 0, 0, good_ref),
            (906, 604_800, 0, good_ref),
            (906, -1, 0, good_ref),
            (906, 0, 1_000_000_000, good_ref),
            (906, 0, -1, good_ref),
            (906, 0, 0, "2018-01-01T00:00:00Z"),  # ref outside era
            (906, 0, 0, "not-a-time"),
            (906, 0, 0, None),
            ("906", 0, 0, good_ref),
            (1.5, 0, 0, good_ref),
            (True, 0, 0, good_ref),
        ):
            with self.subTest(args=(mod, sow, ns, ref)):
                with self.assertRaises(TimeConversionError):
                    resolve_gps_modulo_event(mod, sow, ns, ref)


if __name__ == "__main__":
    unittest.main(verbosity=2)
