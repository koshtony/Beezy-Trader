"""Forecast track record: published projections checked against the prices that followed."""

import datetime as dt

import numpy as np
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from market.models import JobLog, PriceBar, Projection, Stock
from market.services import accuracy, projections

THU, FRI, MON, TUE = dt.date(2026, 11, 19), dt.date(2026, 11, 20), dt.date(2026, 11, 23), dt.date(2026, 11, 24)


def prices_scraped(day: dt.date, hh: int, mm: int = 30):
    row = JobLog.objects.create(job="prices", status=JobLog.OK)
    when = timezone.make_aware(dt.datetime.combine(day, dt.time(hh, mm)))
    JobLog.objects.filter(pk=row.pk).update(started_at=when, finished_at=when)


class TrackRecordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.s = {t: Stock.objects.create(ticker=t, name=f"{t} Plc", sector="Other") for t in ("AAA", "BBB", "CCC")}
        for t, (thu, fri) in {"AAA": (100, 102), "BBB": (50, 50), "CCC": (20, 18)}.items():
            PriceBar.objects.create(stock=self.s[t], date=THU, close=thu)
            PriceBar.objects.create(stock=self.s[t], date=FRI, close=fri)
        # Thursday's 1-day calls for Friday: AAA up (right), BBB up (didn't move), CCC up (wrong, outside range)
        for t, exp, lo, hi in (("AAA", 101, 98, 104), ("BBB", 50.5, 48, 52), ("CCC", 20.2, 19, 21)):
            base = {"AAA": 100, "BBB": 50, "CCC": 20}[t]
            self.proj(t, "1D", THU, FRI, base, exp, lo, hi)
        self.proj("AAA", "1W", THU, dt.date(2026, 11, 26), 100, 103, 95, 110)

    def proj(self, t, h, made, target, base, exp, lo, hi):
        return Projection.objects.create(
            stock=self.s[t], horizon=h, as_of=made, target_date=target, base_price=base, expected_price=exp,
            low=lo, high=hi, expected_return=exp / base - 1, prob_up=0.55,
        )

    def test_waits_for_the_official_close(self):
        prices_scraped(FRI, 12)  # midday: Friday's prices are still intraday
        lv = accuracy.live()
        self.assertEqual(lv["through"], THU)
        self.assertEqual(lv["by_horizon"]["1D"]["n"], 0)
        self.assertEqual(lv["pending"]["1D"]["n"], 3)

    def test_checks_forecasts_against_actual_closes(self):
        prices_scraped(FRI, 17)
        lv = accuracy.live()
        s = lv["by_horizon"]["1D"]
        self.assertEqual((s["n"], s["moved"], s["right"], s["flat"]), (3, 2, 1, 1))
        self.assertAlmostEqual(s["direction"], 0.5)
        self.assertAlmostEqual(s["inside"], 2 / 3)
        err = (1 / 102 + 0.5 / 50 + 2.2 / 18) / 3
        self.assertAlmostEqual(s["error"], err)
        self.assertAlmostEqual(s["accuracy"], 1 - err)
        self.assertAlmostEqual(s["naive_error"], (2 / 102 + 0 + 2 / 18) / 3)
        by = {r["ticker"]: r for r in lv["records"]}
        self.assertEqual([by[t]["direction"] for t in ("AAA", "BBB", "CCC")], ["right", "flat", "wrong"])
        self.assertEqual([by[t]["in_range"] for t in ("AAA", "BBB", "CCC")], [True, True, False])
        self.assertEqual(lv["pending"]["1W"], {"n": 1, "next": dt.date(2026, 11, 26)})

    def test_holiday_target_uses_the_next_session(self):
        self.proj("AAA", "1D", FRI, MON, 102, 103, 99, 106)  # Monday turns out to be a holiday
        prices_scraped(FRI, 17)
        self.assertEqual(accuracy.live()["pending"]["1D"]["n"], 1)
        PriceBar.objects.create(stock=self.s["AAA"], date=TUE, close=104)
        prices_scraped(TUE, 17)
        rec = [r for r in accuracy.live()["records"] if r["made"] == FRI][0]
        self.assertEqual((rec["target"], rec["actual"], rec["direction"]), (TUE, 104, "right"))

    def test_backtest_aggregate_uses_each_stocks_latest_run(self):
        bt_a = {"samples": 100, "moves": 80, "hit_rate": 0.5, "band_coverage": 0.9, "mae": 0.05, "mae_naive": 0.05, "skill": 0.0}
        bt_b = {"samples": 50, "moves": 50, "hit_rate": 0.6, "band_coverage": 0.8, "mae": 0.04, "mae_naive": 0.05, "skill": 0.2}
        old = self.proj("AAA", "1M", dt.date(2026, 11, 18), dt.date(2026, 12, 17), 99, 100, 90, 110)
        Projection.objects.filter(pk=old.pk).update(backtest={**bt_a, "hit_rate": 0.99})  # superseded: ignored
        for t, bt in (("AAA", bt_a), ("BBB", bt_b)):
            p = self.proj(t, "1M", THU, dt.date(2026, 12, 18), 100, 101, 90, 110)
            Projection.objects.filter(pk=p.pk).update(backtest=bt)
        b = accuracy.backtest()["1M"]
        self.assertEqual((b["n"], b["stocks"]), (150, 2))
        self.assertAlmostEqual(b["direction"], (0.5 * 80 + 0.6 * 50) / 130)
        self.assertAlmostEqual(b["inside"], (0.9 * 100 + 0.8 * 50) / 150)
        self.assertAlmostEqual(b["beats_share"], 0.5)
        self.assertEqual(accuracy.backtest()["1D"], {"n": 0})

    def test_pages_api_and_download(self):
        prices_scraped(FRI, 17)
        r = self.client.get("/api/experiments/accuracy")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["headline"]["key"], "1D")
        self.assertEqual(len(data["latest"]), 3)
        self.assertTrue(data["notes"]["findings"])
        frag = self.client.get("/api/ui/experiments/accuracy")
        self.assertContains(frag, "Price accuracy")
        self.assertContains(frag, "Latest checked forecasts")
        self.assertContains(frag, 'id="exp-acc-1D"')
        self.assertContains(self.client.get("/experiments/?tab=accuracy"), "Accuracy")
        csv = b"".join(self.client.get("/api/download/accuracy?format=csv&ticker=CCC").streaming_content).decode()
        self.assertIn("inside_range", csv)
        self.assertEqual(csv.count("CCC"), 1)

    def test_nothing_checked_yet(self):
        frag = self.client.get("/api/ui/experiments/accuracy")
        self.assertContains(frag, "No forecast has reached its target date yet")
        csv = b"".join(self.client.get("/api/download/accuracy").streaming_content).decode()
        self.assertTrue(csv.lstrip("\ufeff").startswith("ticker,horizon,made_on"))


class BacktestDirectionTests(SimpleTestCase):
    def test_unchanged_prices_are_not_scored_for_direction(self):
        rng = np.random.default_rng(3)
        moves = rng.normal(0.0005, 0.015, 900)
        moves[rng.random(900) < 0.4] = 0.0  # thinly traded: many unchanged days
        close = 50 * np.exp(np.cumsum(moves))
        bt = projections.backtest(close, "1D")
        self.assertGreater(bt["flat_share"], 0.2)
        self.assertLess(bt["moves"], bt["samples"])
        self.assertTrue(0 <= bt["hit_rate"] <= 1)
