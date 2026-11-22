"""News jobs before this release always queried every stock over 60 days: label them as full
sweeps so the scheduler starts with quick runs instead of repeating a full sweep."""

from django.db import migrations


def forwards(apps, schema_editor):
    apps.get_model("market", "JobLog").objects.filter(job="news").update(job="news-full")


class Migration(migrations.Migration):
    dependencies = [("market", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
