"""Macro data (CBK, EIA) and the macro/company factor views."""

import datetime as dt
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from market.models import JobLog, MacroPoint, NewsArticle, PriceBar, Stock
from market.services import factors, http, macro

HOME = """<html><body><div>Daily KES Exchange Rates US DOLLAR 129.98 STG POUND 170.42 EURO 142.78 More...
Posted On: 23-11-2026</div><div>Key Rates Central Bank Rate 8.75% 07/10/2026 KESONIA 8.7540% 20/11/2026
CBK Discount Window 9.25% 07/10/2026 91-Day T-Bill 8.742% 23/11/2026 REPO 9.25% 15/10/2025
Inflation Rate 7.3% October,2026 Lending Rate 14.4% September,2026 Savings Rate 3.53% September,2026
Deposit Rate 6.94% September,2026</div></body></html>"""

CBR_PAGE = """<table><tr><th>Date</th><th>Rate</th></tr><tr><td>27/05/2019</td><td>9.00</td></tr>
<tr><td>10/02/2026</td><td>8.75</td></tr><tr><td>07/10/2026</td><td>8.75</td></tr></table>"""

INFLATION_PAGE = """<table><tr><th>Year</th><th>Month</th><th>Annual Average Inflation</th><th>12-Month Inflation</th></tr>
<tr><td>2026</td><td>October</td><td>5.67</td><td>7.32</td></tr><tr><td>2026</td><td>September</td><td>5.44</td><td>6.76</td></tr></table>"""

RATES_CSV = """,,CENTRAL BANK RATES,,,,,,,
YEAR,MONTH,Repo,Reverse Repo,Interbank Rate,91-Day Tbill,182-days Tbill,364-days Tbill,Cash Reserve Requirement,Central Bank Rate
2026,January,9.03,-,8.99,7.72,7.8,9.2,3.25,9
,February,8.87,-,8.84,7.62,7.78,9.07,3.25,8.75
,March,8.7,-,                 -  ,7.54,7.83,8.51,3.25,8.75
,,,,,,,,,
Source: Central Bank of Kenya,,,,,,,,,
"""

BANKS_CSV = """,,COMMERCIAL BANKS' WEIGHTED AVERAGE INTEREST RATES (%) 1/,,,
YEAR,MONTH,Deposit,Savings,Lending ,Overdraft
2026,JANUARY,7.03,3.18,14.81,13.56
,JUlY,6.93,3.53,14.39,12.97
"""

FX_CSV = """,Kenya Shilling End Period Exchange Rates\\1,,,
Year,Month,United States dollar,Sterling pound,Euro
2026,9,129.79,171.68,147.22
2026,10,129.95,173.2,144.74
"""

EIA = """<table><tr><td>x</td></tr></table><table>
<tr><th>Week Of</th><th>Mon</th><th>Tue</th><th>Wed</th><th>Thu</th><th>Fri</th></tr>
<tr><td>2025 Dec-29 to Jan- 2</td><td>61.10</td><td>61.30</td><td>61.50</td><td></td><td>61.90</td></tr>
<tr><td>2026 Nov-16 to Nov-20</td><td>92.90</td><td>92.94</td><td></td><td></td><td></td></tr></table>"""

STATS_PAGE = """<a href="/uploads/interest_rates/648402325_Central Bank Rates_with Heads.csv">rates</a>
<a href="/uploads/interest_rates/1654955992_Commercial banks Weighted Average Rates.csv">banks</a>
<a href="/uploads/exchange_rates/1091858998_Monthly exchange rate (end period).csv">fx</a>"""


def by(points):
    return {(s, d): v for s, d, v in points}


class MacroParserTests(SimpleTestCase):
    def test_homepage_key_rates(self):
        p = by(macro.parse_home(HOME))
        self.assertEqual(p[("usdkes", dt.date(2026, 11, 23))], 129.98)
        self.assertEqual(p[("cbr", dt.date(2026, 10, 7))], 8.75)
        self.assertEqual(p[("kesonia", dt.date(2026, 11, 20))], 8.754)
        self.assertEqual(p[("tbill91", dt.date(2026, 11, 23))], 8.742)
        self.assertEqual(p[("inflation", dt.date(2026, 10, 31))], 7.3)
        self.assertEqual(p[("lending", dt.date(2026, 9, 30))], 14.4)
        self.assertEqual(p[("deposit", dt.date(2026, 9, 30))], 6.94)

    def test_pages_and_files(self):
        self.assertEqual(len(macro.parse_cbr_page(CBR_PAGE)), 3)
        self.assertEqual(by(macro.parse_inflation_page(INFLATION_PAGE))[("inflation", dt.date(2026, 9, 30))], 6.76)
        rates = by(macro.parse_rates_csv(RATES_CSV))
        self.assertEqual(rates[("tbill91", dt.date(2026, 2, 28))], 7.62)  # year carried down from January
        self.assertNotIn(("interbank", dt.date(2026, 3, 31)), rates)  # "-" is missing, not zero
        banks = by(macro.parse_bank_rates_csv(BANKS_CSV))
        self.assertEqual(banks[("lending", dt.date(2026, 7, 31))], 14.39)
        self.assertEqual(by(macro.parse_fx_csv(FX_CSV))[("usdkes", dt.date(2026, 10, 31))], 129.95)

    def test_eia_weeks_cross_the_year(self):
        p = by(macro.parse_eia_brent(EIA))
        self.assertEqual(p[("brent", dt.date(2025, 12, 29))], 61.10)
        self.assertEqual(p[("brent", dt.date(2026, 1, 2))], 61.90)
        self.assertNotIn(("brent", dt.date(2026, 1, 1)), p)
        self.assertEqual(p[("brent", dt.date(2026, 11, 17))], 92.94)

    def test_file_links_are_discovered(self):
        self.assertTrue(macro.find_link(STATS_PAGE, "central bank rates").endswith("Central Bank Rates_with Heads.csv"))
        self.assertIn("end period", macro.find_link(STATS_PAGE, "end period"))
        self.assertIsNone(macro.find_link(STATS_PAGE, "nothing like this"))


class MacroUpdateTests(TestCase):
    def fake_fetch(self, url, **kw):
        if url.endswith("/interest-rates/") or url.endswith("/exchange-rates/"):
            return STATS_PAGE
        if "Central" in url:
            return RATES_CSV
        if "Weighted" in url:
            return BANKS_CSV
        if "end period" in url:
            return FX_CSV
        if "central-bank-rate" in url:
            return CBR_PAGE
        if "inflation" in url:
            return INFLATION_PAGE
        if "eia.gov" in url:
            raise http.PermanentHTTPError("404 for EIA")  # one source down
        return HOME

    def test_update_stores_every_source_and_survives_a_failure(self):
        with mock.patch.object(http, "fetch", side_effect=self.fake_fetch):
            msg = macro.update()
        self.assertIn("failed: brent", msg)
        self.assertEqual(MacroPoint.objects.get(series="inflation", date=dt.date(2026, 10, 31)).value, 7.32)  # file beats homepage
        self.assertTrue(MacroPoint.objects.filter(series="usdkes", date=dt.date(2026, 11, 23)).exists())
        self.assertEqual(JobLog.objects.get(job="macro").status, JobLog.OK)
        with mock.patch.object(http, "fetch", side_effect=self.fake_fetch):
            macro.update()  # idempotent
        self.assertEqual(MacroPoint.objects.filter(series="cbr").count(), 3)


def months_back(n, today=None):
    today = today or timezone.localdate()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(dt.date(y, m, 28))
    return out[::-1]


class FactorTests(TestCase):
    def setUp(self):
        cache.clear()
        for t, sector in (("KCB", "Banking"), ("KPLC", "Energy & Petroleum"), ("WTK", "Agricultural"), ("NASI", "Index")):
            Stock.objects.create(ticker=t, name=t, sector=sector, kind=Stock.KIND_INDEX if t == "NASI" else Stock.KIND_EQUITY)

    def point(self, series, date, value):
        MacroPoint.objects.create(series=series, date=date, value=value)

    def test_rising_rates_help_banks_and_hurt_borrowers(self):
        today = timezone.localdate()
        self.point("tbill91", today - dt.timedelta(days=200), 7.5)
        self.point("tbill91", today, 8.7)
        env = {e["key"]: e for e in factors.environment()}
        r = env["rates"]
        self.assertEqual(r["direction"], "rising")
        self.assertIn("KCB", [t for h in r["helps"] for t in h["tickers"]])
        self.assertIn("KPLC", [t for h in r["hurts"] for t in h["tickers"]])
        self.assertFalse(env["oil"]["available"])
        status = {e["factor"]: e["status"] for e in factors.for_stock("KCB", "Banking")["exposures"]}
        self.assertEqual(status["rates"], "tailwind")

    def test_steady_factor_only_lists_who_would_feel_it(self):
        today = timezone.localdate()
        self.point("usdkes", today - dt.timedelta(days=95), 129.4)
        self.point("usdkes", today, 129.9)
        s = {e["key"]: e for e in factors.environment()}["shilling"]
        self.assertEqual((s["direction"], s["helps"], s["hurts"]), ("steady", [], []))
        self.assertTrue(s["watch"])
        wtk = factors.for_stock("WTK", "Agricultural")["exposures"][0]
        self.assertEqual(wtk["status"], "watch")
        self.assertTrue(wtk["why"].startswith("If the shilling weakens"))

    def test_measured_sensitivity_finds_a_real_link(self):
        rng = np.random.default_rng(7)
        months = months_back(62)
        fx = 110 * np.exp(np.cumsum(rng.normal(0.004, 0.02, len(months))))
        mkt = 100 * np.exp(np.cumsum(rng.normal(0.005, 0.04, len(months))))
        oil = 70 * np.exp(np.cumsum(rng.normal(0, 0.08, len(months))))
        tb = 8 + np.cumsum(rng.normal(0, 0.3, len(months)))
        r_kcb = 0.9 * np.diff(np.log(mkt)) - 1.5 * np.diff(np.log(fx)) + rng.normal(0, 0.01, len(months) - 1)
        kcb = 40 * np.exp(np.concatenate([[0], np.cumsum(r_kcb)]))
        nasi, k = Stock.objects.get(ticker="NASI"), Stock.objects.get(ticker="KCB")
        for i, d in enumerate(months):
            PriceBar.objects.create(stock=nasi, date=d, close=float(mkt[i]))
            PriceBar.objects.create(stock=k, date=d, close=float(kcb[i]))
            for s, v in (("usdkes", fx[i]), ("brent", oil[i]), ("tbill91", tb[i])):
                self.point(s, d, float(v))
        sens = factors.sensitivity()
        row = next(r for r in sens["rows"] if r["ticker"] == "KCB")
        self.assertLess(row["shilling_t"], -2)
        self.assertAlmostEqual(row["shilling"], -1.5 * 5, delta=1.5)  # % move for a 5% weaker shilling
        self.assertEqual(row["shilling_check"], "confirmed")
        self.assertAlmostEqual(row["market"], 0.9, delta=0.15)


class EventTests(TestCase):
    def test_tags(self):
        self.assertEqual(factors.tag("KCB posts profit after tax of Sh58bn"), ["results"])
        self.assertEqual(factors.tag("Carbacid issues profit warning"), ["warning"])
        self.assertIn("regulation", factors.tag("KRA slaps EABL with new excise tax"))
        self.assertIn("dividend", factors.tag("Co-op Bank declares interim dividend"))
        self.assertEqual(factors.tag("Safaricom shares rise"), [])

    def test_same_story_from_many_outlets_is_one_event(self):
        st = Stock.objects.create(ticker="KCB", name="KCB Group", sector="Banking")
        now = timezone.now()
        for i, title in enumerate(["KCB profit rises 24%", "KCB nine-month profit up 24%", "KCB Group posts higher profit"]):
            NewsArticle.objects.create(stock=st, title=title, url="https://ex.com/%d" % i, source="X",
                                       published_at=now - dt.timedelta(minutes=i), sentiment=0.5, label="positive")
        ev = factors.events(30)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["similar"], 2)


class FactorPageTests(TestCase):
    def setUp(self):
        cache.clear()
        Stock.objects.create(ticker="KCB", name="KCB Group", sector="Banking")
        today = timezone.localdate()
        for s, a, b in (("tbill91", 7.5, 8.7), ("inflation", 6.5, 7.3), ("usdkes", 129.4, 129.9), ("brent", 70, 92), ("cbr", 9, 8.75)):
            MacroPoint.objects.create(series=s, date=today - dt.timedelta(days=200), value=a)
            MacroPoint.objects.create(series=s, date=today, value=b)

    def test_page_api_fragment_and_download(self):
        r = self.client.get("/factors/")
        self.assertContains(r, "What today's environment means")
        self.assertContains(r, "▲ Rising")
        self.assertContains(r, 'id="fc-rates"')
        data = self.client.get("/api/factors").json()
        self.assertEqual({e["key"]: e["direction"] for e in data["environment"]}["oil"], "rising")
        self.assertEqual(self.client.get("/api/stocks/KCB/factors").status_code, 200)
        self.assertEqual(self.client.get("/api/stocks/NOPE/factors").status_code, 404)
        self.assertContains(self.client.get("/api/ui/factors/KCB"), "Macro right now")
        csv = b"".join(self.client.get("/api/download/macro?series=brent").streaming_content).decode()
        self.assertEqual(csv.count("brent"), 2)
        self.assertEqual(self.client.get("/api/download/macro?series=nope").status_code, 400)

    def test_empty_page(self):
        MacroPoint.objects.all().delete()
        cache.clear()
        self.assertContains(self.client.get("/factors/"), "No macro data yet")
