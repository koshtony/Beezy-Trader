"""Unit tests for scrapers (offline fixtures), sentiment, metrics, projections and scoring."""

import datetime as dt
import math
from types import SimpleNamespace

import numpy as np
from django.test import SimpleTestCase

from market.services import afx, metrics, news, projections, scoring, stockanalysis
from market.services.sentiment import score_headline

AFX_LISTING = """<!doctype html><html><body><main>
<time id=u datetime=2026-11-19T12:30:12+00:00>Nov 19</time>
<table><thead class=c><tr><th>NASI Index<th>Year-to-Date<th>Market Cap.<tbody class=c>
<tr><td>239.68 <span class=lo>(-0.89)</span><td class=hi>+53.10 (28.46%)<td>KES 4.09Tr</table>
<table><thead><tr><th>Ticker<th>Name<th>Volume<th>Price<th>Change<tbody>
<tr><td><a href=x>ABSA</a><td><a href=x>Absa Bank Kenya Plc</a><td>156,046<td>32.60<td class=lo>-0.30
<tr><td><a href=x>ALP</a><td><a href=x>ALP REIT</a><td><td>1.02<td>
<tr class=ss><td><a href=x>ARM</a><td><a href=x>ARM Cement Ltd</a><td><td>5.50<td>
<tr><td><a href=x>SCOM</a><td><a href=x>Safaricom Plc</a><td>9,461,176<td>34.40<td class=lo>-0.10
</table></main></body></html>"""

AFX_CHART = """$(function(){var d=function(d){return Date.parse(d)};$('x').highcharts('StockChart',
{series:[{name:'SCOM',data:[[d("2026-11-17"),34.7],[d("2026-11-18"),34.5],[d("2026-11-19"),34.4],]}]});})"""

AFX_PROFILE = """<main><div class=t><table><thead><tr><th colspan=2>Growth &amp; Valuation<tbody>
<tr><td>Earnings Per Share<td class=hi>2.3863<tr><td>Price/Earning Ratio<td class=hi>14.42
<tr><td>Dividend Per Share<td>2.30<tr><td>Dividend Yield<td>6.69%<tr><td>Shares Outstanding<td>40.1B
<tr><td>Market Capitalization<td>1.38T</table></div>
<div class=t><table data-hist><thead><tr><th>Date<th>Volume<th>Close<th>Change<th>Change%<tbody>
<tr><td>2026-11-19<td>9,461,176<td>34.40<td class=lo>-0.10<td class=lo>-0.29%</table></div>
<div class=t data-fact><dl><div><div><dt>Sector<dd>Telecommunications</div><div><dt>Industry<dd>Mobile Telecommunications</div></div></dl></div>
</main>"""

SA_STATS = """<html><body><main>
<div>Valuation Ratios</div><table>
<tr><td>PE Ratio</td><td>12.44</td></tr><tr><td>Forward PE</td><td>11.76</td></tr>
<tr><td>PS Ratio</td><td>3.04</td></tr><tr><td>PB Ratio</td><td>6.54</td></tr><tr><td>PEG Ratio</td><td>0.74</td></tr>
<tr><td>Debt / Equity</td><td>0.87</td></tr><tr><td>Return on Equity (ROE)</td><td>41.97%</td></tr>
</table><table>
<tr><td>Revenue</td><td>452.84B</td></tr><tr><td>Gross Profit</td><td>336.77B</td></tr>
<tr><td>Net Income</td><td>110.98B</td></tr><tr><td>Earnings Per Share (EPS)</td><td>2.77</td></tr>
<tr><td>Total Debt</td><td>206.73B</td></tr><tr><td>Equity (Book Value)</td><td>236.32B</td></tr>
<tr><td>Book Value Per Share</td><td>5.25</td></tr><tr><td>Operating Cash Flow</td><td>197.09B</td></tr>
<tr><td>Capital Expenditures</td><td>-78.68B</td></tr><tr><td>Free Cash Flow</td><td>118.41B</td></tr>
<tr><td>Gross Margin</td><td>74.37%</td></tr><tr><td>Dividend Per Share</td><td>2.30</td></tr>
<tr><td>Payout Ratio</td><td>71.89%</td></tr><tr><td>Price Target</td><td>43.70</td></tr>
<tr><td>Analyst Consensus</td><td>Strong Buy</td></tr><tr><td>Current Ratio</td><td>n/a</td></tr>
</table></main></body></html>"""

SA_FIN = """<table><thead><tr><th>Fiscal Year</th><th>TTM</th><th>FY 2026</th><th>FY 2025</th></tr></thead><tbody>
<tr><td>Period Ending</td><td>Sep '26 Sep 30, 2026</td><td>Mar '26</td><td>Mar '25</td></tr>
<tr><td>Revenue</td><td>452,841</td><td>423,866</td><td>385,233</td></tr>
<tr><td>Earnings Per Share</td><td>2.77</td><td>2.39</td><td>1.74</td></tr></tbody></table>"""

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Safaricom profit rises 12% on M-Pesa growth - Business Daily</title><link>https://ex.com/1</link>
<pubDate>Wed, 18 Nov 2026 08:00:00 GMT</pubDate><source url="https://bd">Business Daily</source></item>
<item><title>Safaricom profit rises 12% on M-Pesa growth - The Star</title><link>https://ex.com/2</link>
<pubDate>Wed, 18 Nov 2026 09:00:00 GMT</pubDate><source url="https://st">The Star</source></item>
<item><title>Unrelated football result - Sports</title><link>https://ex.com/3</link>
<pubDate>Wed, 18 Nov 2026 09:00:00 GMT</pubDate><source url="https://s">Sports</source></item>
</channel></rss>"""


class AfxParserTests(SimpleTestCase):
    def test_listing(self):
        snap = afx.parse_listing(AFX_LISTING)
        self.assertEqual(snap.as_of, dt.date(2026, 11, 19))
        self.assertAlmostEqual(snap.nasi, 239.68)
        self.assertAlmostEqual(snap.nasi_change, -0.89)
        self.assertAlmostEqual(snap.market_cap, 4.09e12)
        self.assertEqual([r.ticker for r in snap.rows], ["ABSA", "ALP", "ARM", "SCOM"])
        absa, alp, arm, scom = snap.rows
        self.assertEqual(absa.volume, 156046)
        self.assertAlmostEqual(absa.change, -0.30)
        self.assertIsNone(alp.volume)
        self.assertTrue(arm.suspended)
        self.assertAlmostEqual(scom.price, 34.40)

    def test_chart(self):
        pts = afx.parse_chart(AFX_CHART)
        self.assertEqual(pts[0], (dt.date(2026, 11, 17), 34.7))
        self.assertEqual(len(pts), 3)

    def test_profile(self):
        p = afx.parse_profile(AFX_PROFILE)
        self.assertEqual(p.sector, "Telecommunications")
        self.assertAlmostEqual(p.eps, 2.3863)
        self.assertAlmostEqual(p.shares, 40.1e9)
        self.assertEqual(p.recent[0], (dt.date(2026, 11, 19), 9461176, 34.40))


class StockAnalysisParserTests(SimpleTestCase):
    def test_statistics(self):
        v = stockanalysis.parse_statistics(SA_STATS)
        self.assertAlmostEqual(v["pe"], 12.44)
        self.assertAlmostEqual(v["roe"], 41.97)
        self.assertAlmostEqual(v["revenue"], 452.84e9)
        self.assertAlmostEqual(v["capex"], -78.68e9)
        self.assertEqual(v["consensus"], "Strong Buy")
        self.assertIsNone(v["current_ratio"])

    def test_financials_eps_history(self):
        hist, fiscal = stockanalysis.parse_financials(SA_FIN)
        self.assertEqual([h["period"] for h in hist], ["FY 2025", "FY 2026", "TTM"])
        self.assertEqual(hist[-1]["eps"], 2.77)
        self.assertIn("2026", fiscal)


class NewsAndSentimentTests(SimpleTestCase):
    def test_feed_filters_and_dedupes(self):
        items = news.parse_feed(RSS, ["safaricom"])
        self.assertEqual(len(items), 1)  # duplicate story + unrelated item dropped
        self.assertEqual(items[0].source, "Business Daily")
        self.assertEqual(items[0].label, "positive")

    def test_labels(self):
        cases = {
            "KCB Group net profit rises by 23.9% to KSh 58.63 billion": "positive",
            "Kenya Airways narrows loss to Sh5bn": "positive",
            "Bamburi issues profit warning": "negative",
            "EABL shares fall 5% after weak demand": "negative",
            "Stanbic Bank freezes Vodacom's dividend amid dispute": "negative",
            "Safaricom names new chief financial officer": "neutral",
            "Kenya Airways net loss widens": "negative",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(score_headline(text)[1], want)

    def test_unsafe_links_are_dropped(self):
        from market.security import safe_url

        bad = RSS.replace("https://ex.com/1", "javascript:alert(1)")
        items = news.parse_feed(bad, ["safaricom"])
        self.assertEqual(items[0].url, "")
        self.assertEqual(safe_url(" HTTPS://ok.example/x "), "HTTPS://ok.example/x")
        self.assertEqual(safe_url("data:text/html,hi"), "")

    def test_negation_flips(self):
        self.assertLess(score_headline("Bank fails to grow profit")[0], score_headline("Bank grows profit")[0])


def _fund(**kw):
    base = dict(
        eps_ttm=2.0, eps_history=[{"period": f"FY {y}", "eps": e} for y, e in zip(range(2021, 2026), [1.0, 1.2, 1.4, 1.6, 1.9])],
        revenue_ttm=100e9, gross_profit_ttm=60e9, net_income_ttm=20e9, equity=100e9, book_value_per_share=10.0,
        total_debt=50e9, cash=5e9, operating_cash_flow=30e9, capex=10e9, free_cash_flow=20e9,
        dividend_per_share=1.0, shares_outstanding=10e9, src_pe=None, src_forward_pe=None, src_peg=None,
        src_pb=None, src_ps=None, src_roe=None, src_roa=None, src_debt_to_equity=None, src_gross_margin=None,
        src_profit_margin=None, src_payout_ratio=None, src_current_ratio=1.4, eps_growth_forecast=None,
        revenue_growth_forecast=None, analyst_target=None, analyst_consensus="", analyst_count=None, fiscal_period="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class MetricsTests(SimpleTestCase):
    def test_valuation_uses_live_price(self):
        m = metrics.valuation(20.0, _fund(), is_financial=False, is_bank=False)
        self.assertAlmostEqual(m["pe"], 10.0)
        self.assertAlmostEqual(m["pb"], 2.0)
        self.assertAlmostEqual(m["ps"], 2.0)  # 20 * 10e9 / 100e9
        self.assertAlmostEqual(m["roe"], 20.0)
        self.assertAlmostEqual(m["gross_margin"], 60.0)
        self.assertAlmostEqual(m["debt_to_equity"], 0.5)
        self.assertAlmostEqual(m["dividend_yield"], 5.0)
        self.assertAlmostEqual(m["payout_ratio"], 50.0)
        self.assertAlmostEqual(m["dividend_cover"], 2.0)
        self.assertAlmostEqual(m["fcf_yield"], 10.0)
        # PEG = P/E / historical EPS CAGR (1.0 -> 1.9 over 4 years ≈ 17.4%)
        cagr = (1.9 ** 0.25 - 1) * 100
        self.assertAlmostEqual(m["peg"], 10.0 / cagr, places=3)
        self.assertEqual(m["eps_consistency"], 1.0)

    def test_loss_maker_has_no_pe(self):
        m = metrics.valuation(20.0, _fund(eps_ttm=-1.0), is_financial=False, is_bank=False)
        self.assertIsNone(m["pe"])
        self.assertTrue(m["loss_making"])
        self.assertEqual(metrics.verdicts(m, 8, 1)["pe"]["verdict"], "poor")

    def test_growth_from_low_base_is_capped(self):
        hist = [{"period": "FY 2021", "eps": 0.02}, {"period": "FY 2025", "eps": 2.18}]
        m = metrics.valuation(18.0, _fund(eps_ttm=2.5, eps_history=hist), is_financial=True, is_bank=False)
        self.assertEqual(m["growth"], 40.0)
        self.assertIn("capped", m["growth_source"])

    def test_bank_verdicts(self):
        m = metrics.valuation(30.0, _fund(gross_profit_ttm=100e9), is_financial=True, is_bank=True)
        v = metrics.verdicts(m, 6, 1)
        self.assertEqual(v["fcf"]["verdict"], "na")
        self.assertEqual(v["ps"]["verdict"], "na")

    def test_technicals(self):
        closes = 100 * np.exp(np.cumsum(np.full(300, 0.001)))
        dates = np.arange(np.datetime64("2025-01-01"), np.datetime64("2025-01-01") + 300)
        s = metrics.Series(dates, closes, np.full(300, np.nan))
        t = metrics.technicals(s)
        self.assertGreater(t["ret_1y"], 0.25)
        self.assertEqual(t["rsi_14"], 100.0)  # only up days
        self.assertAlmostEqual(t["max_drawdown_1y"], 0.0)
        self.assertLess(t["vol_1y"], 0.01)


class ProjectionTests(SimpleTestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        r = rng.normal(0.0004, 0.015, 1500)
        self.close = 50 * np.exp(np.cumsum(r))

    def test_horizons_and_bands(self):
        out = projections.project(self.close, dt.date(2026, 11, 19))
        self.assertEqual(list(out), ["1D", "1W", "1M", "1Y"])
        for hz, p in out.items():
            self.assertLess(p["low"], p["expected_price"])
            self.assertLess(p["expected_price"], p["high"])
            self.assertTrue(0 <= p["prob_up"] <= 1)
        # uncertainty widens with horizon
        widths = [(p["high"] - p["low"]) / p["base_price"] for p in out.values()]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(out["1D"]["target_date"], "2026-11-20")
        self.assertEqual(out["1W"]["target_date"], "2026-11-26")

    def test_drift_is_capped(self):
        close = 10 * np.exp(np.cumsum(np.full(600, 0.01)))  # +250%/yr straight line
        p = projections.project(close, dt.date(2026, 1, 1))["1Y"]
        self.assertLessEqual(p["components"]["statistical_return"], math.exp(0.35) - 1 + 1e-9)

    def test_corporate_action_jump_is_ignored(self):
        close = np.concatenate([np.full(300, 100.0), np.full(300, 50.0)])  # 2-for-1 split
        p = projections.project(close, dt.date(2026, 1, 1))["1Y"]
        self.assertAlmostEqual(p["components"]["statistical_return"], 0.0, places=6)

    def test_fundamental_and_analyst_anchor(self):
        val = {"available": True, "growth": 15.0, "pe": 5.0, "analyst_target": 80.0, "analyst_count": 3}
        p = projections.project(self.close, dt.date(2026, 1, 1), valuation=val, sector_pe=8.0)["1Y"]
        self.assertIsNotNone(p["components"]["fundamental_return"])
        self.assertIsNotNone(p["components"]["analyst_return"])
        self.assertAlmostEqual(sum(p["components"]["weights"].values()), 1.0, places=2)

    def test_news_tilt_short_horizons_only(self):
        a = projections.project(self.close, dt.date(2026, 1, 1), news_score=0.8)
        b = projections.project(self.close, dt.date(2026, 1, 1), news_score=0.0)
        self.assertGreater(a["1W"]["expected_price"], b["1W"]["expected_price"])
        self.assertAlmostEqual(a["1Y"]["expected_price"], b["1Y"]["expected_price"])

    def test_backtest_reports(self):
        bt = projections.backtest(self.close, "1W")
        self.assertGreater(bt["samples"], 50)
        self.assertTrue(0 <= bt["hit_rate"] <= 1)
        self.assertGreater(bt["band_coverage"], 0.6)


class ScoringTests(SimpleTestCase):
    def test_ranges(self):
        self.assertEqual(scoring.lower_better(4, 5, 20), 100)
        self.assertEqual(scoring.lower_better(25, 5, 20), 0)
        self.assertAlmostEqual(scoring.higher_better(12.5, 0, 25), 50)
        self.assertIsNone(scoring.weighted([(None, 1)]))

    def test_cheap_quality_stock_beats_expensive_loss_maker(self):
        good_val = metrics.valuation(20.0, _fund(), is_financial=False, is_bank=False)
        bad_val = metrics.valuation(200.0, _fund(eps_ttm=-1.0, free_cash_flow=-5e9, dividend_per_share=0), is_financial=False, is_bank=False)
        tech = {"vol_1y": 0.25, "max_drawdown_1y": -0.15, "rsi_14": 55, "vs_sma200": 0.05, "avg_turnover": 5e6, "active_ratio": 0.9}
        proj = {"1Y": {"expected_return": 0.12, "prob_up": 0.7}, "1M": {"expected_return": 0.01, "prob_up": 0.55}}
        neutral = {"score": 0.0}
        med = {"pe": 9.0, "pb": 1.5}
        a = scoring.score_stock(good_val, tech, proj, neutral, med, is_financial=False, is_bank=False)
        b = scoring.score_stock(bad_val, tech, proj, neutral, med, is_financial=False, is_bank=False)
        self.assertGreater(a["composite"], b["composite"] + 15)
        self.assertGreater(b["risk"], a["risk"])

    def test_pick_diversifies_sectors(self):
        rows = [{"ticker": f"B{i}", "sector": "Banking", "risk_adjusted": 90 - i} for i in range(5)]
        rows += [{"ticker": "T1", "sector": "Telecom", "risk_adjusted": 50}, {"ticker": "M1", "sector": "Manufacturing", "risk_adjusted": 40}, {"ticker": "I1", "sector": "Insurance", "risk_adjusted": 30}]
        picks = [r["ticker"] for r in scoring.pick(rows, 5)]
        self.assertEqual(picks, ["B0", "B1", "T1", "M1", "I1"])

    def test_liquidity(self):
        self.assertTrue(scoring.is_liquid({"avg_turnover": 2e6, "volume_days": 10, "active_ratio": 0.8}, 5e5))
        self.assertFalse(scoring.is_liquid({"avg_turnover": 1e5, "volume_days": 10, "active_ratio": 0.8}, 5e5))
        self.assertFalse(scoring.is_liquid({"avg_turnover": 5e4, "volume_days": 1, "active_ratio": 0.9}, 5e5))
        self.assertTrue(scoring.is_liquid({"avg_turnover": None, "volume_days": 0, "active_ratio": 0.7}, 5e5))
