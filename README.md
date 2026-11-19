# Beezy Trader — NSE trading assistant

A Django + Django Ninja app for the **Nairobi Securities Exchange**. It does the following:

* scrapes **daily closing prices** and keeps about **10 years of history** per stock in the database
* scrapes **fundamentals** and computes the key investor metrics with the live price:
  P/E, PEG, P/B, P/S, EPS (and its trend), ROE, free cash flow, gross margin, debt-to-equity,
  dividend yield and payout/cover. Each metric gets a good/fair/poor verdict against the stock's sector.
* **projects prices** for the next **day, week, month and year**. Each projection blends recent momentum
  with long-run history, and for longer horizons also uses fundamentals and analyst targets. Each comes
  with a 90% range and a walk-forward backtest.
* pulls the **latest headlines** for every stock and labels them **positive (green)** or **negative (red)**
* tracks **volatility, drawdown, beta, RSI, gainers/losers/most active**
* combines all of it into **Five to Buy Now**, plus a **high risk · high gain** list and a
  **low risk · steady return** list
* includes a **portfolio calculator**. Enter an amount and a holding period in days, weeks, months or
  years, then pick **Low risk**, **Steady** or **High risk**. It builds the portfolio, buys whole
  shares after NSE trading costs, and runs 4,000 simulated futures (dividends included, after
  withholding tax). It shows the likely value, the bad and good cases, the chance of profit, and
  milestones along the way.
* shows a **disclaimer on first visit**, which must be acknowledged once and is remembered for a year

The front end is server-rendered HTML styled with the **Beezy theme family** shared with Beezy
Sales: **B&W** (the default), **Navy** and **Light**. The theme switcher is in the header, and the
choice is remembered. The app uses a monospace font, a floating bottom app menu, and charts that
re-colour when you switch theme. It is built with **vanilla JS + HTMX + Plotly**, with no build step
and no web fonts.

> **Not investment advice.** Projections are statistical estimates with wide error bars. Check
> company filings and speak to a CMA-licensed advisor before you trade.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py bootstrap        # first load: prices, fundamentals, news, ~10y history, analysis
python manage.py runserver        # http://127.0.0.1:8000
```

If your browser forces `https://` on 127.0.0.1 (`ERR_SSL_PROTOCOL_ERROR`), run
`python manage.py runserver_https` instead. It serves https://127.0.0.1:8000/ with a self-signed
certificate stored in `.certs/`. Accept the browser warning once (Advanced → Proceed).

`bootstrap` takes about **75 minutes**. The history source (afx.kwayisi.org) asks for a 60-second
crawl delay, and the app honours it. Fundamentals and news load in parallel in a few minutes. To
see the app sooner, run `python manage.py bootstrap --no-history`, then
`python manage.py backfill_history --missing` in another terminal.

## Commands

| Command | What it does |
|---|---|
| `scrape_prices` | Today's NSE price list (one request) |
| `backfill_history [TICKERS…] [--missing]` | ~10 years of daily closes per stock, plus the NASI index |
| `scrape_fundamentals [TICKERS…]` | Statistics and EPS history from stockanalysis.com |
| `scrape_news [TICKERS…] [--rescore]` | Google News headlines with sentiment labels (`--rescore` relabels stored headlines) |
| `analyze` | Recomputes metrics, projections, backtests, scores and picks |
| `refresh` | The daily job: prices → news → (fundamentals if older than 7 days) → analysis |
| `run_scheduler [--now daily\|news\|weekly]` | A built-in scheduler loop, if you'd rather not use cron |

### Scheduling with cron (Africa/Nairobi time; the NSE closes at 15:00)

```cron
30 15 * * 1-5  cd /path/to/Beezy-Trader && .venv/bin/python manage.py refresh
0 10,13,18 * * 1-5  cd /path/to/Beezy-Trader && .venv/bin/python manage.py scrape_news && .venv/bin/python manage.py analyze
0 8 * * 6  cd /path/to/Beezy-Trader && .venv/bin/python manage.py scrape_fundamentals && .venv/bin/python manage.py backfill_history --missing && .venv/bin/python manage.py analyze
```

The **Update prices** and **Update news** buttons in the app run the same jobs in the background.
The page polls the job's progress and reloads when it finishes.

## API (Django Ninja)

Interactive docs are at **`/api/docs`**. JSON endpoints:

| Endpoint | |
|---|---|
| `GET /api/market/summary` | NASI, breadth, data freshness |
| `GET /api/stocks?sector=&q=&sort=&order=` | All stocks with metrics and scores |
| `GET /api/stocks/{ticker}` | Metrics, verdicts, technicals, scores, projections, reasons and warnings |
| `GET /api/stocks/{ticker}/history?days=` | Daily closes |
| `GET /api/stocks/{ticker}/projections` | 1D / 1W / 1M / 1Y projections with ranges and backtests |
| `GET /api/stocks/{ticker}/chart` | Plot-ready series (closes, SMA50/200, projection fan, EPS) |
| `GET /api/stocks/{ticker}/news?label=` · `GET /api/news?label=` | Headlines (positive/negative/neutral) |
| `GET /api/movers?kind=gainers\|losers\|active\|volatile\|ytd` | Market movers |
| `GET /api/recommendations` · `GET /api/recommendations/scatter` | Top 5 and the risk-profile picks |
| `GET /api/calculator?amount=&profile=low\|steady\|high&horizon=&unit=days\|weeks\|months\|years&costs=` | Portfolio simulation, milestones and a comparison of the three styles |
| `GET /api/jobs` · `POST /api/jobs/refresh?kind=prices\|news\|analysis\|daily` | Job status and triggers |

HTMX fragments are served by the same Ninja API under `/api/ui/…`.

## How the picks are made

1. **Metrics.** Ratios are recalculated with today's price. Each gets a verdict using rules of thumb
   (ROE ≥ 15%, D/E > 2 is risky, PEG < 1, and so on) and the sector median. Banks and insurers are
   handled differently: free cash flow and gross margin don't apply to them, and their D/E counts
   borrowings only, not deposits.
2. **Projections.** These blend three drift estimates: recent (EWMA plus 20-day trend), last year, and
   the long run. Each estimate is shrunk toward zero according to its t-statistic. Short horizons lean
   on recent data and longer ones on history. 1M and 1Y also blend in EPS growth, a partial re-rating
   to the sector P/E, and any analyst target. Recent news tone nudges the 1D and 1W projections. Daily
   moves over 20% (splits and bonus issues in the unadjusted history) are ignored.
3. **Scores (0–100).** The pillars and weights are: value 20%, quality 20%, balance-sheet health 10%,
   income 12%, outlook 25%, news sentiment 13%. **Risk** combines volatility, drawdown, beta,
   illiquidity, leverage and losses.
4. **Picks.** The Top 5 have the best risk-adjusted scores among liquid, profitable ordinary shares
   with a positive 12-month projection, with at most two per sector. The high-risk list ranks
   above-median-risk names by projected gain. The low-risk list ranks names scoring at least 45 by
   lowest risk.

5. **Calculator.** It starts from the same eligible universe, applies each style's selection and
   weighting rules, and simulates correlated price paths. Because it picks the stocks with the best
   projections, which tend to be overestimated, it pulls each projection halfway toward a 7% market
   price return. Each simulated path also carries its own projection error of ±15% a year. Beyond one
   year, growth fades toward the long-run trend and is capped at 10% a year. Trading costs are about
   2.1% each way.

The **Methodology** page in the app shows the live weights.

## Layout

```
beezy/                 settings, urls
market/
  models.py            Stock, PriceBar, Fundamentals, NewsArticle, Projection, StockAnalytics, …
  api.py · ui.py       Django Ninja routers (JSON + HTMX fragments)
  queries.py           cached read helpers
  services/            afx.py, stockanalysis.py, news.py (scrapers) · sentiment.py · metrics.py
                       projections.py · scoring.py · pipeline.py · ingest.py · catalog.py
  templates/ static/   ledger theme, app.js, vendored htmx + plotly-basic
  tests/               python manage.py test market
```

## Production notes

Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS`, run `collectstatic`, and serve
with gunicorn. WhiteNoise then serves compressed static files with long cache lifetimes. SQLite runs
in WAL mode so the site can be read while jobs write.
