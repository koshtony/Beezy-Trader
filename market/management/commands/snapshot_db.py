import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Write a consistent copy of the database (safe while the site is running), e.g. to seed a "
        "server: railway volume files upload <file> /seed.sqlite3, then restart the service."
    )

    def add_arguments(self, parser):
        parser.add_argument("output", nargs="?", default="seed.sqlite3")

    def handle(self, *args, **opts):
        out = Path(opts["output"]).resolve()
        if out.exists():
            raise CommandError(f"{out} already exists")
        src = settings.DATABASES["default"]["NAME"]
        con = sqlite3.connect(src)
        try:
            con.execute("VACUUM INTO ?", (str(out),))
        finally:
            con.close()
        self.stdout.write(self.style.SUCCESS(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB)"))
