"""Template context shared by every page."""

import os

from django.contrib.staticfiles import finders

ASSETS = ("market/app.js", "market/app.css")


def asset_version(request):
    """Version stamp for our CSS/JS (latest modification time) so browsers never run stale copies."""
    stamps = []
    for name in ASSETS:
        path = finders.find(name)
        if path:
            stamps.append(int(os.path.getmtime(path)))
    return {"asset_v": max(stamps) if stamps else 0}
