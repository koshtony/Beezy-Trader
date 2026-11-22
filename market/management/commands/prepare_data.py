"""Container start-up for the production database (runs before the web server opens it).

1. Seed: install $DATA_DIR/seed.sqlite3 only if production has no price data yet, or if
   BEEZY_ALLOW_SEED=1 is set. Otherwise the upload is set aside, so a stray local copy can
   never overwrite production. A replaced database is always kept as a backup.
2. Back up before schema changes: when migrations are pending, snapshot the database first
   (newest BACKUPS_KEPT kept in $DATA_DIR/backups).
3. Migrate.
"""

import os
import shutil
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

BACKUPS_KEPT = 5


def _stamp() -> str:
    return timezone.now().strftime("%Y%m%d-%H%M%S")


def has_price_data(db: Path) -> bool:
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT 1 FROM market_pricebar LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:  # empty file or no tables yet
        return False


def snapshot(db: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute("VACUUM INTO ?", (str(out),))
    finally:
        con.close()


class Command(BaseCommand):
    help = "Prepare the production database: guarded seed install, backup before migrations, migrate."

    def handle(self, *args, **opts):
        db = Path(settings.DATABASES["default"]["NAME"])
        data = db.parent
        data.mkdir(parents=True, exist_ok=True)
        self._seed(db, data / "seed.sqlite3")
        self._backup_if_migrating(db, data / "backups")
        call_command("migrate", interactive=False, verbosity=1)

    def _seed(self, db: Path, seed: Path) -> None:
        if not seed.exists():
            return
        if has_price_data(db) and os.environ.get("BEEZY_ALLOW_SEED") != "1":
            kept = seed.with_name(f"seed.sqlite3.refused-{_stamp()}")
            seed.rename(kept)
            self.stdout.write(self.style.WARNING(
                f"seed: production already has data, so the uploaded seed was NOT installed (kept as {kept.name}). "
                "To replace production on purpose, set BEEZY_ALLOW_SEED=1, upload again, restart, then remove the variable."
            ))
            return
        if db.exists():
            old = db.with_name(f"{db.name}.before-seed-{_stamp()}")
            shutil.move(db, old)
            self.stdout.write(f"seed: previous database kept as {old.name}")
        for suffix in ("-wal", "-shm"):
            Path(f"{db}{suffix}").unlink(missing_ok=True)
        shutil.move(seed, db)
        self.stdout.write(self.style.SUCCESS("seed: installed"))

    def _backup_if_migrating(self, db: Path, folder: Path) -> None:
        if not has_price_data(db):
            return
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if not plan:
            return
        out = folder / f"before-migrate-{_stamp()}.sqlite3"
        snapshot(db, out)
        self.stdout.write(f"backup: {len(plan)} migration(s) pending; saved {out.name}")
        for old in sorted(folder.glob("before-migrate-*.sqlite3"))[:-BACKUPS_KEPT]:
            old.unlink()
