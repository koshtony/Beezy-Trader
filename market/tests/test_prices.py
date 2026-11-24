"""Price sources: parsers for each site, checks, failover, and the bundled history."""

import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from market.models import JobLog, PriceBar, Stock
from market.services import afx, ingest, pipeline, prices

TICKERS = [f"T{i:02d}" for i in range(45)] + ["SCOM", "HFCK"]
TODAY = dt.date(2026, 11, 24)


def mansa_html(price=33.95):
    rows = "".join(
        f'<tr><td>{i}</td><td><div><a href="/kenya/{t.lower()}">{t} Plc</a><span>{t}</span></div></td>'
        f'<td><span>KSh{price if t == "SCOM" else 10 + i}</span></td><td><span>-0.15</span></td><td>−0.44%</td>'
        f'<td>1.05M</td><td>40.07B</td><td>KSh1.21T</td></tr>'
        for i, t in enumerate(TICKERS)
    )
    return (f"<html><body><p>Last updated 24 Nov, 15:15 GMT+3</p><table><tr><th>#</th><th>Stock</th><th>Price</th>"
            f"<th>Change</th><th>Chg %</th><th>Volume</th><th>Shares</th><th>Mkt Cap</th></tr>{rows}</table></body></html>")


def af_html():
    rows = "".join(
        f'<tr><td><a href="https://africanfinancials.com/company/ke-{t.lower()}/" title="{t} PLC ({t}.ke)">{t}</a></td>'
        f'<td class="numeric">{"33.95" if t == "SCOM" else 10 + i}</td><td class="numeric">{"-0.44" if t == "SCOM" else "+1.00"}</td>'
        f'<td class="numeric">48,740,691</td><td class="numeric">1,435,661</td><td class="numeric">+19.75</td><td>Tech</td>'
        f'<td class="numeric">{"Nov 23, 2026" if t == "T00" else "Nov 24, 2026"}</td></tr>'
        for i, t in enumerate(TICKERS)
    )
    return ('<table><tr><th>Company</th><th>Price</th><th>% Change</th><th>Value</th><th>Volume</th><th>YTD %</th>'
            f'<th>Sector</th><th>Updated</th></tr>{rows}</table>')


def rokam_html():
    cells = "".join("<td>x</td>" for _ in range(14))
    rows = "".join(
        f'<tr class="equity-row" data-code="{t}" data-name-display="{t} Plc" data-price="{34.1 if t == "SCOM" else 10 + i}" '
        f'data-change-amt="-0.1">{cells}<td>4.6M</td></tr>'
        for i, t in enumerate(TICKERS)
    )
    index = '<tr data-is-index="1" data-code="^NASI" data-price="236.58" data-change-amt="-1.28"><td>NASI</td></tr>'
    return f"<title>Market Price List (2026-11-23) - Rokam</title><table>{index}{rows}</table>"


class ParserTests(SimpleTestCase):
    def test_mansa(self):
        snap = prices.parse_mansa(mansa_html(), today=TODAY)
        by = {r.ticker: r for r in snap.rows}
        self.assertEqual(snap.as_of, TODAY)
        self.assertEqual((by["SCOM"].price, by["SCOM"].change, by["SCOM"].volume), (33.95, -0.15, 1_050_000))
        self.assertIn("HFCB", by)  # HF Group's other code
        self.assertEqual(prices.parse_mansa(mansa_html(), today=dt.date(2027, 1, 3)).as_of, dt.date(2026, 11, 24))

    def test_africanfinancials(self):
        snap = prices.parse_africanfinancials(af_html())
        by = {r.ticker: r for r in snap.rows}
        self.assertEqual(snap.as_of, TODAY)
        self.assertEqual(by["SCOM"].price, 33.95)
        self.assertAlmostEqual(by["SCOM"].change, -0.15, places=2)  # from the % change
        self.assertEqual((by["T00"].change, by["T00"].volume), (0.0, 0))  # didn't trade today

    def test_rokam(self):
        snap = prices.parse_rokam(rokam_html())
        by = {r.ticker: r for r in snap.rows}
        self.assertEqual((snap.as_of, snap.nasi, snap.nasi_change), (dt.date(2026, 11, 23), 236.58, -1.28))
        self.assertEqual((by["SCOM"].price, by["SCOM"].change, by["SCOM"].volume), (34.1, -0.1, 4_600_000))
        self.assertNotIn("NASI", by)


def good_snapshot(as_of=TODAY, scale=1.0, n=47):
    rows = [afx.ListingRow(ticker=t, name=t, volume=100, price=(10 + i) * scale, change=0.1, suspended=False)
            for i, t in enumerate(TICKERS[:n])]
    return afx.MarketSnapshot(as_of=as_of, nasi=None, nasi_change=None, market_cap=None, rows=rows)


class CheckTests(TestCase):
    def test_rejects_short_stale_or_off_scale_lists(self):
        prices.check(good_snapshot(), today=TODAY)
        with self.assertRaisesMessage(prices.SourceRejected, "only 10 prices"):
            prices.check(good_snapshot(n=10), today=TODAY)
        with self.assertRaisesMessage(prices.SourceRejected, "dated"):
            prices.check(good_snapshot(as_of=TODAY - dt.timedelta(days=9)), today=TODAY)
        for i, t in enumerate(TICKERS[:30]):
            st = Stock.objects.create(ticker=t, name=t)
            PriceBar.objects.create(stock=st, date=TODAY - dt.timedelta(days=1), close=10 + i)
        prices.check(good_snapshot(scale=1.02), today=TODAY)  # normal moves pass
        with self.assertRaisesMessage(prices.SourceRejected, "don't match recent closes"):
            prices.check(good_snapshot(scale=100), today=TODAY)  # cents instead of shillings, wrong page...


class FailoverTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_falls_through_to_the_first_good_source_and_demotes_failures(self):
        def fake(name, retries=1):
            if name == "afx":
                raise requests.ConnectionError("HTTPSConnectionPool(host='afx.kwayisi.org', port=443): [Errno 101] Network is unreachable")
            if name == "mansa":
                return good_snapshot(n=12)
            return good_snapshot(as_of=timezone.localdate())

        with mock.patch.object(prices, "fetch_source", side_effect=fake) as fetch:
            snap, source, problems = prices.fetch_snapshot()
        self.assertEqual(source, "africanfinancials")
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["afx", "mansa", "africanfinancials"])
        self.assertEqual(problems, ["AFX: can't reach afx.kwayisi.org (network unreachable)", "Mansa Markets: only 12 prices"])
        self.assertTrue(prices.is_down("afx"))
        self.assertEqual(prices.order(), ["africanfinancials", "rokam", "afx", "mansa"])  # failing ones go last

    def test_all_sources_down(self):
        with mock.patch.object(prices, "fetch_source", side_effect=requests.ConnectionError("boom")):
            with self.assertRaisesMessage(RuntimeError, "all price sources failed"):
                prices.fetch_snapshot()

    def test_index_comes_from_rokam_when_the_source_has_none(self):
        snap = good_snapshot(as_of=timezone.localdate())
        rokam = afx.MarketSnapshot(as_of=timezone.localdate() - dt.timedelta(days=1), nasi=236.58, nasi_change=-1.28,
                                   market_cap=None, rows=[])
        with mock.patch.object(prices, "fetch_snapshot", return_value=(snap, "mansa", [])), \
             mock.patch.object(prices, "fetch_source", return_value=rokam):
            ingest.update_daily_prices()
        bar = PriceBar.objects.get(stock__ticker="NASI")
        self.assertEqual((bar.close, bar.date, bar.source), (236.58, rokam.as_of, "rokam-daily"))
        self.assertIn("NASI 236.58", JobLog.objects.get(job="prices").message)

    def test_update_records_the_source(self):
        snap = good_snapshot(as_of=timezone.localdate())
        with mock.patch.object(prices, "fetch_snapshot", return_value=(snap, "rokam", ["AFX: down"])):
            ingest.update_daily_prices()
        self.assertTrue(PriceBar.objects.filter(source="rokam-daily").exists())
        row = JobLog.objects.get(job="prices")
        self.assertIn("via Rokam", row.message)
        self.assertIn("skipped AFX: down", row.message)
        self.assertEqual(ingest.LAST_PRICE_SOURCE, "rokam")
        self.assertTrue(pipeline._reported_changes())  # day changes from any source are used


class BundleTests(TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        a = Stock.objects.create(ticker="AAA", name="Alpha", sector="Banking", history_backfilled_at=timezone.now())
        Stock.objects.create(ticker="ZZZ", name="Zed", sector="Other", is_suspended=True)
        for i in range(5):
            PriceBar.objects.create(stock=a, date=dt.date(2026, 1, 1) + dt.timedelta(days=i), close=10 + i,
                                    volume=100 * i, change=0.5 if i == 4 else None, source="afx-daily" if i == 4 else "afx-hist")

    def test_export_import_round_trip(self):
        self.assertEqual(ingest.export_history(self.folder), (2, 5))
        PriceBar.objects.all().delete()
        Stock.objects.all().delete()
        self.assertEqual(ingest.import_bundled_history(self.folder), 5)
        a = Stock.objects.get(ticker="AAA")
        self.assertIsNotNone(a.history_backfilled_at)
        self.assertTrue(Stock.objects.get(ticker="ZZZ").is_suspended)
        last = PriceBar.objects.filter(stock=a).order_by("-date").first()
        self.assertEqual((last.close, last.change, last.source, last.volume), (14.0, 0.5, "afx-daily", 400))
        self.assertIn("to 2026-01-05", JobLog.objects.get(job="history").message)
        self.assertEqual(ingest.import_bundled_history(self.folder / "missing"), 0)

    def test_new_server_starts_from_the_bundle_without_afx(self):
        ingest.export_history(self.folder)
        PriceBar.objects.all().delete()
        Stock.objects.all().delete()
        cache.set("bz:price-down:afx", "unreachable", 60)
        snap = good_snapshot(as_of=timezone.localdate())
        with mock.patch.object(ingest, "BUNDLE_DIR", self.folder), \
             mock.patch.object(prices, "fetch_snapshot", return_value=(snap, "mansa", [])), \
             mock.patch.object(ingest, "update_fundamentals"), mock.patch.object(ingest, "update_news"), \
             mock.patch("market.services.macro.update"), \
             mock.patch.object(ingest, "backfill_history") as history, \
             mock.patch.object(pipeline, "run_analysis") as analysis:
            analysis.return_value.payload = {"top5": []}
            pipeline.bootstrap()
        self.assertEqual(PriceBar.objects.filter(source="afx-hist").count(), 4)
        self.assertTrue(PriceBar.objects.filter(source="mansa-daily").exists())
        history.assert_not_called()  # history only comes from AFX, which is down
        self.assertFalse(pipeline.needs_bootstrap())
