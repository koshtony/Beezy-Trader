"""End-to-end tests: seed a small synthetic market, run the pipeline, hit the API and pages."""

import datetime as dt
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from market.models import Fundamentals, NewsArticle, PriceBar, Projection, RecommendationRun, Stock, StockAnalytics
from market.services import afx, ingest, pipeline


def trading_days(end: dt.date, n: int) -> list[dt.date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


SPECS = {
    # ticker: (sector, drift/day, vol/day, eps, bvps, dps, fcf)
    "GOOD": ("Banking", 0.0006, 0.012, 6.0, 30.0, 3.0, None),
    "STDY": ("Manufacturing & Allied", 0.0003, 0.006, 4.0, 20.0, 2.5, 3e9),
    "WILD": ("Commercial & Services", 0.0012, 0.035, 1.0, 5.0, 0.0, 1e8),
    "LOSS": ("Commercial & Services", -0.0008, 0.03, -2.0, 3.0, 0.0, -2e9),
    "BANK": ("Banking", 0.0004, 0.014, 8.0, 60.0, 4.0, None),
    "TELE": ("Telecommunication", 0.0005, 0.013, 2.5, 6.0, 2.0, 1e11),
}


class PipelineApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        today = timezone.localdate()
        days = trading_days(today, 600)
        rng = np.random.default_rng(42)
        for t, (sector, mu, sig, eps, bvps, dps, fcf) in SPECS.items():
            st = Stock.objects.create(ticker=t, name=f"{t} Holdings Plc", sector=sector, history_backfilled_at=timezone.now())
            close = 50 * np.exp(np.cumsum(rng.normal(mu, sig, len(days))))
            bars = [
                PriceBar(stock=st, date=d, close=float(c), volume=int(2e6), source="afx-hist")
                for d, c in zip(days, close)
            ]
            PriceBar.objects.bulk_create(bars)
            Fundamentals.objects.create(
                stock=st, eps_ttm=eps, book_value_per_share=bvps, dividend_per_share=dps,
                shares_outstanding=1e9, revenue_ttm=60e9, gross_profit_ttm=30e9, net_income_ttm=eps * 1e9,
                equity=bvps * 1e9, total_debt=0.6 * bvps * 1e9, free_cash_flow=fcf,
                eps_history=[{"period": f"FY {2021 + i}", "eps": eps * (0.8 + 0.05 * i)} for i in range(5)],
            )
            NewsArticle.objects.create(
                stock=st, title=f"{t} profit rises 20% on strong demand", url=f"https://ex.com/{t}",
                source="Daily", published_at=timezone.now() - dt.timedelta(days=1), sentiment=0.7, label="positive",
            )
        NewsArticle.objects.create(
            stock=Stock.objects.get(ticker="LOSS"), title="LOSS issues profit warning", url="https://ex.com/w",
            source="Daily", published_at=timezone.now(), sentiment=-0.8, label="negative",
        )
        pipeline.run_analysis(with_backtest=True)

    def setUp(self):
        cache.clear()

    def test_pipeline_outputs(self):
        self.assertEqual(StockAnalytics.objects.count(), len(SPECS))
        self.assertEqual(Projection.objects.filter(stock__ticker="GOOD").count(), 4)
        run = RecommendationRun.objects.first()
        top = [p["ticker"] for p in run.payload["top5"]]
        self.assertNotIn("LOSS", top)  # loss-makers never qualify
        self.assertLessEqual(len(top), 5)
        self.assertLessEqual(sum(1 for p in run.payload["top5"] if p["sector"] == "Banking"), 2)
        for p in run.payload["low_risk_low_gain"]:
            self.assertGreater(p["exp_1y"], 0)

    def test_summary_and_list(self):
        r = self.client.get("/api/market/summary")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stocks"], len(SPECS))
        r = self.client.get("/api/stocks?sector=Banking&sort=pe&order=asc")
        rows = r.json()
        self.assertEqual({x["ticker"] for x in rows}, {"GOOD", "BANK"})
        self.assertLessEqual(rows[0]["pe"], rows[1]["pe"])

    def test_detail_projection_chart_history(self):
        d = self.client.get("/api/stocks/good").json()
        self.assertEqual(d["ticker"], "GOOD")
        self.assertEqual([p["horizon"] for p in d["projections"]], ["1D", "1W", "1M", "1Y"])
        self.assertIn("pe", d["metrics"]["verdicts"])
        self.assertEqual(len(self.client.get("/api/stocks/GOOD/history").json()), 600)
        chart = self.client.get("/api/stocks/GOOD/chart").json()
        self.assertEqual(len(chart["projection"]["dates"]), 5)
        self.assertEqual(len(chart["sma200"]), 600)
        self.assertEqual(self.client.get("/api/stocks/NOPE").status_code, 404)

    def test_news_movers_recs(self):
        neg = self.client.get("/api/news?label=negative").json()
        self.assertEqual([n["ticker"] for n in neg], ["LOSS"])
        self.assertEqual(self.client.get("/api/movers?kind=volatile&limit=1").json()[0]["ticker"], "WILD")
        recs = self.client.get("/api/recommendations").json()
        self.assertIn("top5", recs)
        pts = self.client.get("/api/recommendations/scatter").json()
        self.assertEqual(len(pts), len(SPECS))
        self.assertEqual(self.client.get("/api/movers?kind=bogus").status_code, 422)

    def test_htmx_fragments(self):
        r = self.client.get("/api/ui/news?label=negative", HTTP_HX_REQUEST="true")
        self.assertContains(r, "label-chip negative")
        self.assertContains(self.client.get("/api/ui/movers?kind=gainers"), "<table")
        self.assertContains(self.client.get("/api/ui/screener?q=tele"), "TELE")
        self.assertNotContains(self.client.get("/api/ui/screener?q=tele"), ">GOOD<")
        self.assertContains(self.client.get("/api/ui/status"), "statusbar")

    def test_pages(self):
        for url in ("/", "/screener/", "/stocks/GOOD/", "/calculator/", "/experiments/", "/data/", "/news/", "/method/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get("/stocks/NOPE/").status_code, 404)
        page = self.client.get("/").content.decode()
        self.assertIn("Five to Buy Now", page)
        self.assertIn("High risk · high expected gain", page)

    @mock.patch("market.api.pipeline.start_background", return_value=True)
    def test_refresh_requires_authorisation(self, start):
        from django.conf import settings
        from django.contrib.auth.models import User

        self.assertEqual(self.client.post("/api/jobs/refresh?kind=news").status_code, 401)
        self.assertEqual(self.client.post("/api/ui/refresh?kind=news").status_code, 401)
        start.assert_not_called()
        with self.settings(BEEZY={**settings.BEEZY, "API_KEY": "s3cret"}):
            self.assertEqual(self.client.post("/api/jobs/refresh?kind=news", HTTP_X_API_KEY="wrong").status_code, 401)
            r = self.client.post("/api/jobs/refresh?kind=news", HTTP_X_API_KEY="s3cret")
        self.assertEqual(r.json(), {"started": True, "kind": "news", "message": "started"})
        start.assert_called_once_with("news")
        User.objects.create_user("ops", password="pw-12345", is_staff=True)
        self.client.login(username="ops", password="pw-12345")
        self.assertEqual(self.client.post("/api/jobs/refresh?kind=prices").status_code, 200)
        self.assertContains(self.client.get("/api/ui/status"), "Update prices")

    def test_refresh_buttons_hidden_from_public(self):
        self.assertNotContains(self.client.get("/api/ui/status"), "Update prices")

    def test_calculator_is_rate_limited(self):
        cache.clear()
        codes = [self.client.get("/api/calculator?amount=100000&horizon=1&unit=years").status_code for _ in range(32)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)


class IngestTests(TestCase):
    def test_store_snapshot_upserts(self):
        snap = afx.MarketSnapshot(
            as_of=dt.date(2026, 11, 19), nasi=240.0, nasi_change=-1.0, market_cap=4e12,
            rows=[
                afx.ListingRow("SCOM", "Safaricom Plc", 1000, 34.4, -0.1, False),
                afx.ListingRow("ARM", "ARM Cement", None, 5.5, None, True),
            ],
        )
        self.assertEqual(ingest.store_snapshot(snap), 1)
        self.assertEqual(ingest.store_snapshot(snap), 1)  # idempotent
        self.assertEqual(PriceBar.objects.filter(stock__ticker="SCOM").count(), 1)
        self.assertTrue(Stock.objects.get(ticker="ARM").is_suspended)
        self.assertEqual(Stock.objects.get(ticker="SCOM").sector, "Telecommunication")
        self.assertEqual(PriceBar.objects.get(stock__ticker="NASI").close, 240.0)

    def test_history_keeps_daily_volume(self):
        st = Stock.objects.create(ticker="SCOM", name="Safaricom")
        PriceBar.objects.create(stock=st, date=dt.date(2026, 11, 19), close=34.4, volume=999, source="afx-daily")
        ingest.store_history(st, [(dt.date(2026, 11, 18), 34.5), (dt.date(2026, 11, 19), 34.4)])
        self.assertEqual(PriceBar.objects.filter(stock=st).count(), 2)
        self.assertEqual(PriceBar.objects.get(stock=st, date=dt.date(2026, 11, 19)).volume, 999)


class CalculatorTests(PipelineApiTests):
    """Reuses the synthetic market built by PipelineApiTests."""

    def test_profiles_and_math(self):
        from market.services import portfolio as pf

        for prof in pf.PROFILES:
            with self.subTest(profile=prof):
                r = pf.simulate(100_000, prof, 6, "months", n_paths=800)
                s = r["summary"]
                self.assertTrue(r["holdings"])
                w = [h["weight"] for h in r["holdings"]]
                self.assertAlmostEqual(sum(w), 1.0, places=6)
                if len(w) >= 3:
                    self.assertLessEqual(max(w), pf.MAX_W + 1e-9)
                spent = sum(h["cost"] for h in r["holdings"]) + s["cash_left"]
                self.assertAlmostEqual(spent, 100_000, places=2)
                self.assertGreaterEqual(s["cash_left"], 0)
                self.assertLessEqual(s["p5"], s["median"])
                self.assertLessEqual(s["median"], s["p95"])
                self.assertEqual(r["milestones"][-1]["days"], r["calendar_days"])
                self.assertNotIn("LOSS", [h["ticker"] for h in r["holdings"]])

    def test_costs_reduce_short_term_value(self):
        from market.services import portfolio as pf

        with_costs = pf.simulate(100_000, "steady", 1, "days", include_costs=True, n_paths=500)["summary"]["median"]
        no_costs = pf.simulate(100_000, "steady", 1, "days", include_costs=False, n_paths=500)["summary"]["median"]
        self.assertLess(with_costs, no_costs - 3_000)  # ~4.2% round trip

    def test_low_risk_is_calmer_than_high_risk(self):
        from market.services import portfolio as pf

        lo = pf.simulate(100_000, "low", 1, "years", n_paths=1500)["summary"]
        hi = pf.simulate(100_000, "high", 1, "years", n_paths=1500)["summary"]
        self.assertLess(lo["p95"] - lo["p5"], hi["p95"] - hi["p5"])

    def test_validation_and_units(self):
        from market.services import portfolio as pf

        self.assertEqual(pf.horizon_days(1, "years"), (365, 252))
        self.assertEqual(pf.horizon_days(30, "days")[1], 21)
        for bad in ((500, "steady", 1, "years"), (1e5, "yolo", 1, "years"), (1e5, "steady", 20, "years"), (1e5, "steady", 1, "fortnights")):
            with self.assertRaises(pf.CalculatorError):
                pf.simulate(*bad)

    def test_api_and_fragment(self):
        r = self.client.get("/api/calculator?amount=250000&profile=low&horizon=90&unit=days")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["profile"], "low")
        self.assertEqual(len(body["compare"]), 3)
        self.assertEqual(len(body["chart"]["dates"]), len(body["chart"]["p50"]))
        self.assertEqual(self.client.get("/api/calculator?amount=10").status_code, 422)
        frag = self.client.get("/api/ui/calculator?amount=100,000&profile=high&horizon=2&unit=years&costs=1")
        self.assertContains(frag, "High risk portfolio")
        self.assertContains(frag, 'id="calc-chart-data"')
        self.assertContains(self.client.get("/api/ui/calculator?amount=abc"), "error-box")
        self.assertContains(self.client.get("/calculator/"), "Portfolio calculator")

    def test_disclaimer_shows_until_accepted(self):
        self.assertContains(self.client.get("/"), "data-auto-open")
        self.client.cookies["bz_disclaimer"] = "1"
        page = self.client.get("/")
        self.assertContains(page, 'id="disclaimer"')  # still available from the footer link
        self.assertNotContains(page, "data-auto-open")

    def test_every_page_has_the_loader_and_credits(self):
        for url in ("/", "/screener/", "/method/"):
            page = self.client.get(url)
            self.assertContains(page, 'id="bz-loader"')
            self.assertContains(page, "Created by <strong>KoshTech Kenya</strong>", html=False)


class ExperimentTests(PipelineApiTests):
    def test_pca_eigen_markov_endpoints(self):
        pca = self.client.get("/api/experiments/pca").json()
        self.assertEqual(pca["n_stocks"], len(SPECS))
        self.assertAlmostEqual(sum(pca["eigenvalues"]), len(SPECS), places=3)  # trace of a correlation matrix
        self.assertAlmostEqual(pca["cumulative"][-1], 1.0, places=3)
        eig = self.client.get("/api/experiments/eigen").json()
        self.assertGreater(eig["lambda_plus"], 1.0)
        mk = self.client.get("/api/experiments/markov?ticker=GOOD").json()
        self.assertAlmostEqual(sum(mk["stationary"]), 1.0, places=3)
        for row in mk["P"]:
            self.assertAlmostEqual(sum(row), 1.0, places=3)
        self.assertEqual(len(mk["fan"]["dates"]), 22)
        self.assertEqual(self.client.get("/api/experiments/markov?ticker=NOPE").status_code, 400)
        self.assertEqual(self.client.get("/api/experiments/bogus").status_code, 422)

    def test_fragments_and_page(self):
        self.assertContains(self.client.get("/experiments/"), "Experiments")
        self.assertContains(self.client.get("/experiments/?tab=markov"), "/api/ui/experiments/markov")
        for kind, marker in (("pca", "exp-variance"), ("eigen", "exp-spectrum"), ("markov", "Transition matrix")):
            with self.subTest(kind=kind):
                self.assertContains(self.client.get(f"/api/ui/experiments/{kind}"), marker)
        self.assertContains(self.client.get("/api/ui/experiments/markov?ticker=NOPE"), "error-box")


class SnapshotSessionTests(TestCase):
    def _snap(self, day, price_shift=0.0, nasi=240.0, nasi_change=-1.0):
        rows = [afx.ListingRow(f"S{i}", f"Stock {i}", 1000, 10.0 + i + price_shift, 0.5, False) for i in range(8)]
        return afx.MarketSnapshot(as_of=day, nasi=nasi, nasi_change=nasi_change, market_cap=1e12, rows=rows)

    def test_repeated_session_is_not_a_new_day(self):
        thu, fri = dt.date(2026, 11, 19), dt.date(2026, 11, 20)
        ingest.store_snapshot(self._snap(thu))
        ingest.store_snapshot(self._snap(fri))  # same prices and changes: yesterday repeated
        self.assertFalse(PriceBar.objects.filter(date=fri).exists())
        ingest.store_snapshot(self._snap(fri, price_shift=0.25))  # genuine new prices
        self.assertEqual(PriceBar.objects.filter(date=fri).exclude(stock__ticker="NASI").count(), 8)

    def test_stale_index_and_weekends(self):
        thu, fri, sat = dt.date(2026, 11, 19), dt.date(2026, 11, 20), dt.date(2026, 11, 21)
        ingest.store_snapshot(self._snap(thu))
        ingest.store_snapshot(self._snap(fri, price_shift=0.25))  # stocks moved, NASI not yet updated
        self.assertFalse(PriceBar.objects.filter(stock__ticker="NASI", date=fri).exists())
        ingest.store_snapshot(self._snap(sat, price_shift=0.5, nasi=241.0, nasi_change=1.0))
        self.assertFalse(PriceBar.objects.filter(date=sat).exists())  # weekend folds onto Friday
        self.assertEqual(PriceBar.objects.get(stock__ticker="NASI", date=fri).close, 241.0)


class NotesTests(PipelineApiTests):
    def test_notes_tab(self):
        data = self.client.get("/api/experiments/notes").json()
        self.assertEqual([s["key"] for s in data["sections"]], ["pca", "eigen", "markov", "accuracy"])
        for s in data["sections"]:
            self.assertTrue(s["findings"] and s["meaning"] and s["caveats"])
        self.assertTrue(data["bottom_line"])
        frag = self.client.get("/api/ui/experiments/notes")
        self.assertContains(frag, "What the results say")
        self.assertContains(frag, "Glossary")
        self.assertContains(self.client.get("/experiments/?tab=notes"), "/api/ui/experiments/notes")


class DownloadTests(PipelineApiTests):
    def _csv(self, url):
        import csv as _csv
        import io

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        self.assertIn("attachment;", r["Content-Disposition"])
        body = b"".join(r.streaming_content).decode("utf-8-sig")
        return list(_csv.DictReader(io.StringIO(body)))

    def test_every_dataset_downloads(self):
        from market.downloads import DATASETS

        for key in DATASETS:
            with self.subTest(dataset=key):
                rows = self._csv(f"/api/download/{key}?ticker=GOOD" if key == "markov" else f"/api/download/{key}")
                self.assertTrue(rows or key in ("accuracy", "macro"), key)  # nothing matured / no macro data here
        js = self.client.get("/api/download/stocks?format=json")
        self.assertEqual(len(js.json()), len(SPECS))
        self.assertIn(".json", js["Content-Disposition"])

    def test_filters(self):
        rows = self._csv("/api/download/prices?ticker=good&start=2026-01-01&end=")
        self.assertTrue(rows)
        self.assertEqual({r["ticker"] for r in rows}, {"GOOD"})
        self.assertTrue(all(r["date"] >= "2026-01-01" for r in rows))
        neg = self._csv("/api/download/news?label=negative")
        self.assertEqual({r["ticker"] for r in neg}, {"LOSS"})
        self.assertEqual(self.client.get("/api/download/prices?start=yesterday").status_code, 400)
        self.assertEqual(self.client.get("/api/download/prices?ticker=../etc").status_code, 400)
        self.assertEqual(self.client.get("/api/download/secrets").status_code, 422)

    def test_csv_formula_injection_is_neutralised(self):
        st = Stock.objects.get(ticker="GOOD")
        NewsArticle.objects.create(stock=st, title='=HYPERLINK("http://evil","click")', url="https://ex.com/x",
                                   published_at=timezone.now(), label="neutral")
        rows = self._csv("/api/download/news?ticker=GOOD")
        titles = [r["title"] for r in rows]
        self.assertIn("'=HYPERLINK(\"http://evil\",\"click\")", titles)
        self.assertFalse(any(t.startswith("=") for t in titles))

    def test_data_page_and_glossary(self):
        from market.glossary import flat

        page = self.client.get("/data/")
        self.assertContains(page, "Download CSV")
        self.assertContains(page, "Marchenko–Pastur")
        self.assertContains(page, 'action="/api/download/prices"')
        self.assertGreater(len(self.client.get("/api/glossary").json()), 100)
        terms = {g["term"] for g in flat()}
        for must in ("P/E", "PEG", "ROE", "Free cash flow (FCF)", "Debt-to-equity (D/E)", "Dividend yield", "Volatility",
                     "Eigenvalue", "Markov chain", "Absorption ratio", "Beezy score", "Monte Carlo simulation"):
            self.assertIn(must, terms)

    def test_downloads_are_rate_limited(self):
        cache.clear()
        codes = [self.client.get("/api/download/glossary").status_code for _ in range(22)]
        self.assertIn(429, codes)


class DataPreviewTests(PipelineApiTests):
    def test_preview_fragment(self):
        r = self.client.get("/api/ui/data-preview/prices?ticker=GOOD&start=&end=")
        self.assertContains(r, "<table")
        self.assertContains(r, "GOOD")
        self.assertContains(r, "first 8 rows")
        self.assertContains(self.client.get("/api/ui/data-preview/news?label=negative"), "LOSS")
        self.assertContains(self.client.get("/api/ui/data-preview/prices?start=nope"), "error-box")
        self.assertContains(self.client.get("/api/ui/data-preview/bogus"), "Unknown dataset")
        page = self.client.get("/data/")
        self.assertContains(page, 'data-show="markov"')
        self.assertContains(page, 'hx-trigger="intersect once"')
