"""Background jobs: locking, scheduling, news batching, analysis reuse, pruning, deploy endpoints."""

import datetime as dt
import subprocess
import sys
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from market import queries
from market.models import JobLog, NewsArticle, Projection, RecommendationRun, Stock
from market.services import http, ingest, locks, news, pipeline, projections, scheduler
from market.tests import test_api  # module import: don't re-collect its tests here

NAIROBI = dt.timezone(dt.timedelta(hours=3))


def at(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=NAIROBI)


def backdate(row, **delta):
    type(row).objects.filter(pk=row.pk).update(started_at=timezone.now() - dt.timedelta(**delta))


class LockTests(TestCase):
    def test_exclusive_blocks_a_second_job(self):
        with locks.exclusive():
            self.assertFalse(locks.acquire_jobs(0))
            with self.assertRaises(locks.Busy):
                with locks.exclusive():
                    pass
        self.assertTrue(locks.acquire_jobs(0))
        locks.release_jobs()

    def test_lock_is_shared_with_other_processes(self):
        path = locks.lock_path("jobs")
        holder = subprocess.Popen(
            [sys.executable, "-c", f"import fcntl,sys,time; f=open({str(path)!r},'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.assertFalse(locks.acquire_jobs(0))
        finally:
            holder.kill()
            holder.wait()
        self.assertTrue(locks.acquire_jobs(1))  # released when the process died
        locks.release_jobs()

    def test_orphaned_running_jobs_are_closed(self):
        JobLog.objects.create(job="news")  # left "running" by a killed process
        with locks.exclusive():
            pass
        row = JobLog.objects.get(job="news")
        self.assertEqual(row.status, JobLog.FAILED)
        self.assertIn("interrupted", row.message)

    def test_start_background_refuses_while_busy(self):
        with locks.exclusive():
            self.assertFalse(pipeline.start_background("analysis"))


class SchedulerTests(TestCase):
    def test_slots(self):
        self.assertEqual(scheduler.last_slot(at(2026, 11, 20, 7, 0)), at(2026, 11, 19, 17, 30))
        self.assertEqual(scheduler.last_slot(at(2026, 11, 20, 12, 30)), at(2026, 11, 20, 12, 30))
        self.assertEqual(scheduler.last_slot(at(2026, 11, 20, 23, 0)), at(2026, 11, 20, 17, 30))
        self.assertEqual(scheduler.next_slot(at(2026, 11, 20, 7, 0)), at(2026, 11, 20, 8, 30))
        self.assertEqual(scheduler.next_slot(at(2026, 11, 20, 18, 0)), at(2026, 11, 21, 8, 30))

    def test_due_until_a_refresh_starts_after_the_slot(self):
        now = timezone.now()
        slot = scheduler.last_slot(now)
        self.assertEqual(scheduler.due(now), slot)
        row = JobLog.objects.create(job="refresh", status=JobLog.OK)
        JobLog.objects.filter(pk=row.pk).update(started_at=slot - dt.timedelta(minutes=1))
        self.assertEqual(scheduler.due(now), slot)  # before the slot: doesn't count
        JobLog.objects.filter(pk=row.pk).update(started_at=slot + dt.timedelta(minutes=1))
        self.assertIsNone(scheduler.due(now))

    @mock.patch.object(pipeline, "needs_bootstrap", return_value=False)
    @mock.patch.object(pipeline, "daily_refresh", return_value="refreshed")
    def test_tick_runs_the_due_slot_once_lock_is_free(self, refresh, _):
        with locks.exclusive():
            self.assertIsNone(scheduler.tick())  # someone else is refreshing: wait
        refresh.assert_not_called()
        self.assertEqual(scheduler.tick(), "refreshed")
        self.assertTrue(locks.acquire_jobs(0))  # released afterwards
        locks.release_jobs()

    @mock.patch.object(pipeline, "bootstrap", return_value="seeded")
    @mock.patch.object(pipeline, "daily_refresh")
    def test_empty_database_bootstraps_first(self, refresh, boot):
        self.assertTrue(pipeline.needs_bootstrap())
        self.assertEqual(scheduler.tick(), "seeded")
        refresh.assert_not_called()
        JobLog.objects.create(job="history", status=JobLog.OK)
        self.assertFalse(pipeline.needs_bootstrap())  # at most one attempt a day
        with override_settings(BEEZY={**pipeline.settings.BEEZY, "AUTO_BOOTSTRAP": False}):
            refresh.return_value = "refreshed"
            JobLog.objects.all().delete()
            self.assertEqual(scheduler.tick(), "refreshed")


FEED = """<?xml version="1.0"?><rss><channel>{}</channel></rss>"""
ITEM = "<item><title>{t} - Daily</title><link>https://ex.com/{n}</link><source>Daily</source><pubDate>{d}</pubDate></item>"


def feed(*titles):
    d = timezone.now().strftime("%a, %d %b %Y %H:%M:%S GMT")
    return FEED.format("".join(ITEM.format(t=t, n=i, d=d) for i, t in enumerate(titles)))


class NewsBatchTests(TestCase):
    def setUp(self):
        self.stocks = [
            Stock.objects.create(ticker=t, name=n, sector="Other")
            for t, n in [("AAA", "Alpha Mills Plc"), ("BBB", "Beta Foods Plc"), ("CCC", "Gamma Paints Plc")]
        ]

    def test_query_combines_names(self):
        self.assertEqual(news.query(["Alpha", "Beta Foods"], "Kenya", 2), '"Alpha" OR "Beta Foods" Kenya when:2d')
        self.assertEqual(news.query_words(["Alpha", "Beta Foods"], "Kenya"), 6)

    def test_split_feed_assigns_by_name_and_detects_cap(self):
        xml = feed("Alpha Mills profit rises", "Beta Foods and Alpha Mills sign deal", "Beta Foods in India listing", "Unrelated")
        found, capped = news.split_feed(xml, {"AAA": ["alpha mills"], "BBB": ["beta foods"]})
        self.assertEqual(len(found["AAA"]), 2)
        self.assertEqual([h.title for h in found["BBB"]], ["Beta Foods and Alpha Mills sign deal"])  # foreign one dropped
        self.assertFalse(capped)
        _, capped = news.split_feed(feed(*[f"Alpha Mills {i}" for i in range(100)]), {"AAA": ["alpha mills"]})
        self.assertTrue(capped)

    def test_grouping(self):
        targets = ingest._news_targets(None)
        groups = ingest.group_targets(targets, solo_ids={self.stocks[0].id})
        self.assertEqual([[t.stock.ticker for t in g] for g in groups], [["AAA"], ["BBB", "CCC"]])
        many = [ingest._NewsTarget(self.stocks[0], f"Name {i}", [], "Kenya") for i in range(20)]
        for g in ingest.group_targets(many, set()):
            self.assertLessEqual(len(g), ingest.GROUP_MAX)
            self.assertLessEqual(news.query_words([t.phrase for t in g], "Kenya"), ingest.GROUP_WORDS)

    def test_plan_full_weekly_quick_otherwise(self):
        self.assertEqual(ingest.news_plan()[0], True)
        backdate(JobLog.objects.create(job="news-full", status=JobLog.OK), days=3)
        backdate(JobLog.objects.create(job="news", status=JobLog.OK), hours=5)
        self.assertEqual(ingest.news_plan(), (False, 2))
        JobLog.objects.filter(job="news-full").update(started_at=timezone.now() - dt.timedelta(days=8))
        self.assertEqual(ingest.news_plan()[0], True)

    @mock.patch.object(http, "fetch")
    def test_quick_run_uses_one_query_for_quiet_stocks(self, fetch):
        backdate(JobLog.objects.create(job="news-full", status=JobLog.OK), days=1)
        fetch.return_value = feed("Alpha Mills profit jumps 30%", "Gamma Paints faces loss warning")
        added = ingest.update_news()
        self.assertEqual(fetch.call_count, 1)
        url = fetch.call_args[0][0]
        self.assertIn("when%3A3d", url)
        self.assertIn("OR", url)
        self.assertEqual(added, 2)
        self.assertEqual(NewsArticle.objects.get(stock__ticker="AAA").label, "positive")
        self.assertEqual(NewsArticle.objects.get(stock__ticker="CCC").label, "negative")
        self.assertEqual(ingest.update_news(), 0)  # same headlines again: nothing new
        self.assertEqual(JobLog.objects.filter(job="news").first().status, JobLog.OK)

    @mock.patch.object(http, "fetch")
    def test_full_sweep_queries_each_stock(self, fetch):
        fetch.return_value = feed()
        ingest.update_news(full=True)
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(JobLog.objects.filter(job="news-full", status=JobLog.OK).exists())


class RateLimitTests(SimpleTestCase):
    def tearDown(self):
        http._penalty.clear()
        http._last_hit.clear()

    @mock.patch.object(http.time, "sleep")
    def test_429_waits_and_slows_the_host(self, sleep):
        limited = mock.Mock(status_code=429, headers={"Retry-After": "7"}, text="")
        ok = mock.Mock(status_code=200, headers={}, text="<rss/>")
        with mock.patch.object(http.session(), "get", side_effect=[limited, ok]):
            self.assertEqual(http.fetch("https://news.google.com/rss/x"), "<rss/>")
        self.assertIn(mock.call(7.0), sleep.call_args_list)
        self.assertGreater(http._penalty["news.google.com"], 0)

    @mock.patch.object(http.time, "sleep")
    def test_gives_up_after_retries(self, sleep):
        limited = mock.Mock(status_code=429, headers={}, text="")
        with mock.patch.object(http.session(), "get", return_value=limited):
            with self.assertRaises(requests.HTTPError):
                http.fetch("https://news.google.com/rss/y", retries=1)


class AnalysisJobTests(TestCase):
    setUpTestData = classmethod(test_api.PipelineApiTests.setUpTestData.__func__)

    def setUp(self):
        cache.clear()

    def test_backtests_are_reused_for_the_same_session(self):
        with mock.patch.object(projections, "backtest", wraps=projections.backtest) as bt:
            pipeline.run_analysis()
            self.assertEqual(bt.call_count, 0)
            pipeline.run_analysis(with_backtest=False)
            self.assertEqual(bt.call_count, 0)
        p = Projection.objects.filter(stock__ticker="GOOD", horizon="1M").order_by("-as_of").first()
        self.assertTrue(p.backtest)  # quick runs no longer wipe them
        Projection.objects.update(backtest={})
        with mock.patch.object(projections, "backtest", wraps=projections.backtest) as bt:
            pipeline.run_analysis()
            self.assertGreater(bt.call_count, 0)

    def test_analysis_writes_are_upserts(self):
        before = Projection.objects.count()
        pipeline.run_analysis()
        self.assertEqual(Projection.objects.count(), before)

    @mock.patch("market.services.macro.update", return_value="macro ok")
    @mock.patch.object(ingest, "update_fundamentals")
    @mock.patch.object(ingest, "update_news", return_value=0)
    @mock.patch.object(ingest, "update_daily_prices", return_value=0)
    def test_refresh_skips_analysis_when_nothing_changed(self, prices, news_, funds, macro_update):
        runs = RecommendationRun.objects.count()
        summary = pipeline.daily_refresh()
        self.assertIn("analysis skipped", summary)
        self.assertEqual(RecommendationRun.objects.count(), runs)
        funds.assert_not_called()  # fundamentals are fresh
        macro_update.assert_called_once()  # macro data refreshes once a day
        row = JobLog.objects.filter(job="refresh").first()
        self.assertEqual((row.status, row.message), (JobLog.OK, summary))
        self.assertTrue(JobLog.objects.filter(job="prune").exists())

        news_.return_value = 3
        self.assertIn("top5", pipeline.daily_refresh())
        self.assertEqual(RecommendationRun.objects.count(), runs + 1)

    def test_prune(self):
        now = timezone.now()
        keep_last = None
        for hours in (3, 2, 1):  # three runs ten days ago: keep the day's last
            r = RecommendationRun.objects.create(payload={})
            RecommendationRun.objects.filter(pk=r.pk).update(created_at=now - dt.timedelta(days=10, hours=hours))
            keep_last = r.pk
        ancient = RecommendationRun.objects.create(payload={})
        RecommendationRun.objects.filter(pk=ancient.pk).update(created_at=now - dt.timedelta(days=200))
        backdate(JobLog.objects.create(job="news", status=JobLog.OK), days=100)
        NewsArticle.objects.create(
            stock=Stock.objects.first(), title="old", url="", source="", published_at=now - dt.timedelta(days=800),
            sentiment=0, label="neutral",
        )
        latest = RecommendationRun.objects.order_by("-created_at").first().pk
        msg = pipeline.prune()
        ids = set(RecommendationRun.objects.values_list("id", flat=True))
        self.assertIn(keep_last, ids)
        self.assertIn(latest, ids)
        self.assertNotIn(ancient.pk, ids)
        self.assertEqual(len([i for i in ids if i not in (keep_last, latest)]), 0)
        self.assertFalse(NewsArticle.objects.filter(title="old").exists())
        self.assertIn("removed 3 runs, 1 job logs", msg)


class DeployTests(TestCase):
    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual((r.status_code, r.content), (200, b"ok"))
        self.assertIn("no-cache", r["Cache-Control"])

    def test_cache_keys_follow_new_analysis_runs(self):
        cache.clear()
        v0 = queries.data_version()
        run = RecommendationRun.objects.create(payload={})
        self.assertEqual(queries.data_version(), v0)  # re-read at most every 10 s
        cache.delete("bz:ver")
        self.assertEqual(queries.data_version(), run.pk)


class PrepareDataTests(SimpleTestCase):
    """A stray seed upload must never overwrite production data."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.db, self.seed = self.dir / "db.sqlite3", self.dir / "seed.sqlite3"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def make_db(self, path, marker, rows=1):
        import sqlite3

        con = sqlite3.connect(path)
        con.execute("CREATE TABLE market_pricebar (id INTEGER PRIMARY KEY, note TEXT)")
        con.executemany("INSERT INTO market_pricebar (note) VALUES (?)", [(marker,)] * rows)
        con.commit()
        con.close()

    def note(self, path):
        import sqlite3

        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT note FROM market_pricebar LIMIT 1").fetchone()[0]
        finally:
            con.close()

    def run_seed(self):
        from io import StringIO

        from market.management.commands.prepare_data import Command

        cmd = Command(stdout=StringIO())
        cmd._seed(self.db, self.seed)
        return cmd.stdout.getvalue()

    def test_refuses_seed_when_production_has_data(self):
        self.make_db(self.db, "production")
        self.make_db(self.seed, "local")
        out = self.run_seed()
        self.assertIn("NOT installed", out)
        self.assertEqual(self.note(self.db), "production")
        self.assertFalse(self.seed.exists())
        self.assertEqual(len(list(self.dir.glob("seed.sqlite3.refused-*"))), 1)

    def test_installs_seed_into_empty_production(self):
        import sqlite3

        sqlite3.connect(self.db).close()  # created by a first boot, no data yet
        self.make_db(self.seed, "local")
        self.assertIn("installed", self.run_seed())
        self.assertEqual(self.note(self.db), "local")
        self.assertEqual(len(list(self.dir.glob("db.sqlite3.before-seed-*"))), 1)

    def test_explicit_override_replaces_and_keeps_backup(self):
        self.make_db(self.db, "production")
        self.make_db(self.seed, "local")
        with mock.patch.dict("os.environ", {"BEEZY_ALLOW_SEED": "1"}):
            self.run_seed()
        self.assertEqual(self.note(self.db), "local")
        backup = next(self.dir.glob("db.sqlite3.before-seed-*"))
        self.assertEqual(self.note(backup), "production")


UNREACHABLE = ("HTTPSConnectionPool(host='afx.kwayisi.org', port=443): Max retries exceeded with url: /nse/ "
               "(Caused by NewConnectionError(\"Failed to establish a new connection: [Errno 101] Network is unreachable\"))")


class UnreachablePriceSourceTests(TestCase):
    """What Railway hit: the price list host can't be reached from the server."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        patcher = mock.patch.object(ingest, "BUNDLE_DIR", Path(tempfile.mkdtemp()))  # no bundled history here
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_bootstrap(self):
        with mock.patch.object(ingest, "update_daily_prices", side_effect=requests.ConnectionError(UNREACHABLE)), \
             mock.patch.object(ingest, "update_fundamentals") as funds, \
             mock.patch.object(ingest, "update_news") as news_, \
             mock.patch("market.services.macro.update") as macro_update, \
             mock.patch.object(ingest, "backfill_history") as history:
            with self.assertRaises(RuntimeError):
                pipeline.bootstrap()
        return funds, news_, macro_update, history

    def test_other_sources_still_load_and_the_error_is_recorded(self):
        funds, news_, macro_update, history = self.run_bootstrap()
        funds.assert_called_once()
        news_.assert_called_once_with(full=True)
        macro_update.assert_called_once()
        history.assert_not_called()  # same host as the price list
        self.assertTrue(Stock.objects.filter(ticker="SCOM").exists())  # stock list from the catalogue
        row = JobLog.objects.get(job="bootstrap")
        self.assertEqual(row.status, JobLog.FAILED)
        self.assertIn("can't reach afx.kwayisi.org (network unreachable)", row.message)

    def test_no_retry_storm(self):
        self.run_bootstrap()
        self.assertFalse(pipeline.needs_bootstrap())  # waits 30 minutes
        JobLog.objects.filter(job="bootstrap").update(started_at=timezone.now() - dt.timedelta(minutes=31))
        self.assertTrue(pipeline.needs_bootstrap())

    def test_pages_explain_the_problem(self):
        self.run_bootstrap()
        status = queries.job_status()
        self.assertFalse(status["running"])
        self.assertIn("afx.kwayisi.org", status["problem"]["text"])
        self.assertContains(self.client.get("/api/ui/status"), "Prices not updated: can&#x27;t reach afx.kwayisi.org")
        self.assertContains(self.client.get("/"), "The price source can't be reached from this server")

    def test_short_error(self):
        self.assertEqual(pipeline.short_error(UNREACHABLE), "can't reach afx.kwayisi.org (network unreachable)")
        self.assertEqual(pipeline.short_error("HTTPSConnectionPool(host='x.org', port=443): Read timed out."), "can't reach x.org (timed out)")
        self.assertEqual(pipeline.short_error("something else"), "something else")
