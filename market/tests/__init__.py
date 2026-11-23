"""Tests never touch the network: a real HTTP request fails loudly (mock market.services.http.fetch)."""

import requests

from market.services import http


class _NoNetwork(requests.Session):
    def request(self, method, url, *args, **kwargs):
        raise RuntimeError(f"network access in tests ({method} {url}); mock market.services.http.fetch")


http._session = _NoNetwork()
