# Beezy Markets — NSE stocks

**Beezy Markets** is a Django + Django Ninja stock analysis app for the **Nairobi Securities Exchange**. It does the following:

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
* has an **Experiments** page with three tabs:
  * **PCA**: the market factor, independent bets, a stock map and loadings
  * **Eigenvalues**: the eigenvalue spectrum against the Marchenko–Pastur noise bound, plus the absorption ratio over time
  * **Markov chain**: the 5-state transition matrix, the long-run distribution, a χ² memory test, memory half-life and simulated paths
* has a **Data** page for downloading every dataset as CSV (Excel-ready) or JSON, with ticker, date and tone filters, plus a searchable **glossary** of about 120 terms
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
| `scrape_news [TICKERS…] [--quick] [--rescore]` | Google News headlines with sentiment labels (`--quick`: combined queries over recent days; `--rescore` relabels stored headlines) |
| `analyze [--backtest]` | Recomputes metrics, projections, scores and picks (`--backtest` recomputes today's backtests too) |
| `refresh [--full-news] [--wait N]` | The scheduled job: prices → news → (fundamentals if older than 7 days) → analysis if anything changed |
| `run_scheduler [--now daily\|news\|weekly]` | The scheduler in the foreground, if you'd rather not use cron |
| `snapshot_db [FILE]` | A consistent copy of the database (safe while running), e.g. to seed a server |

Only one data job runs at a time, whether it comes from the web server, the scheduler, cron or a
command. A second job waits for the first (commands) or is refused (buttons).

### Scheduling (08:30, 12:30 and 17:30 Nairobi time, every day)

Each run does prices → news → analysis, and refreshes fundamentals once they are older than 7 days.
NSE trading hours are 09:30–15:00, so the three runs fetch the previous close, the intraday prices and the official close.

What a routine run costs:

* **Prices**: one request.
* **News**: about 20 requests and 30 s. Busy names get their own query; quiet ones share combined
  `"A" OR "B" …` queries that cover only the days since the last run. Once a week a full sweep
  queries every stock over 60 days. If Google answers 429, the run waits and slows down instead of
  dropping headlines.
* **Analysis**: about 0.6 s. Walk-forward backtests are reused until a new session is added. The
  analysis is skipped when prices, news and fundamentals are all unchanged, unless the last
  analysis is more than 6 hours old.
* **Pruning**: once a day, old analysis runs (90 days), job logs (60), projections (400) and
  headlines (2 years) are removed. Price history is never pruned.

On a server the scheduler runs inside the web process (see [Deploying](#deploying-railway)). On a
laptop, use cron:

```cron
30 8,12,17 * * * cd /path/to/Beezy-Trader && .venv/bin/python manage.py refresh >> logs/cron.log 2>&1 # beezy-trader
```

Cron skips runs while the machine is asleep. `python manage.py run_scheduler` catches up on a
missed slot as soon as it can.

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
| `GET /api/experiments/pca\|eigen\|markov\|accuracy\|notes?ticker=` | PCA, eigenvalue spectrum and absorption ratio, Markov chain, forecast accuracy, plain-language notes |
| `GET /api/download/{dataset}?format=csv\|json&ticker=&start=&end=&label=` | Downloads: stocks, prices, fundamentals, projections, accuracy, news, picks, pca, eigenvalues, markov, glossary |
| `GET /api/glossary` | Every term with its definition |
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

## Deploying (Railway)

The repo ships a `Dockerfile`, `railway.json` and `gunicorn.conf.py`. One container serves the site
and runs the 08:30 / 12:30 / 17:30 refreshes in-process. SQLite lives on a volume. A separate cron
service can't work, because a Railway volume attaches to only one service.

1. **Create the service.** New project → Deploy from GitHub repo (or `railway up` from this folder).
   Railway finds the Dockerfile and `railway.json`, which sets the `/healthz` health check and
   restart-on-failure.
2. **Add a volume** to the service, mounted at **`/data`**.
3. **Set variables.** `DJANGO_SECRET_KEY` is required; generate one with
   `python -c "import secrets; print(secrets.token_urlsafe(50))"`. Everything else is optional; see
   `.env.example`. The generated `*.up.railway.app` domain is trusted automatically. For a custom
   domain, add it to `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
4. **Generate a domain** (Settings → Networking) and deploy.
5. **Load the data**, choosing one of:
   * *Automatic*: an empty database bootstraps itself on first start (about 75 minutes because of
     AFX's crawl delay; resumes after a restart). The site shows data as it arrives.
   * *Seed from your machine*: `python manage.py snapshot_db seed.sqlite3`, then
     `railway volume files upload seed.sqlite3 /seed.sqlite3` and restart the service. The seed is
     installed only while production has no price data yet.

Start-up runs `migrate`, then gunicorn: one process with 8 threads, about 100 MB at rest. That is
enough for this traffic and keeps the scheduler, job runner and page cache in one process. Redeploys
are safe. An interrupted job is marked "interrupted", and a refresh slot missed while the service was
down runs as soon as it is back.

**Security in production** (`DJANGO_DEBUG=0`):

* HTTPS redirect, HSTS, secure cookies and `X-Frame-Options: DENY`.
* API docs are hidden (`BEEZY_API_DOCS=1` shows them).
* The refresh endpoints need staff login or `BEEZY_API_KEY`.
* Rate limits see the real client IP behind Railway's proxy.

**Your local database never reaches production.** `.gitignore`, `.railwayignore` and `.dockerignore`
all exclude `*.sqlite3`. The image reads only `/data/db.sqlite3` on the volume. Deploys change the
code and run migrations; the data itself is untouched. On every start (`manage.py prepare_data`):

* An uploaded `seed.sqlite3` is **refused** once production has price data. It is set aside as
  `seed.sqlite3.refused-*`. To replace production on purpose, set `BEEZY_ALLOW_SEED=1`, upload,
  restart, then remove the variable. The replaced database is kept as `db.sqlite3.before-seed-*`.
* When a deploy brings new migrations, the database is first copied to
  `/data/backups/before-migrate-*.sqlite3` (newest 5 kept).

**Backups:** use Railway's volume backups, or download a snapshot. In `railway ssh`, run
`python manage.py snapshot_db /data/backup.sqlite3`. Then, on your machine, run
`railway volume files download /backup.sqlite3 ./backup.sqlite3`.

### Other hosts

Any Docker host works: `docker build -t beezy . && docker run -p 8000:8000 -v beezy-data:/data -e DJANGO_SECRET_KEY=… beezy`.
Without Docker, set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `BEEZY_DATA_DIR`
and `BEEZY_SCHEDULER=1`, run `collectstatic` and `migrate`, then `gunicorn beezy.wsgi -c gunicorn.conf.py`.
WhiteNoise serves hashed, compressed static files with long cache lifetimes. SQLite runs in WAL mode,
so the site stays readable while jobs write.
