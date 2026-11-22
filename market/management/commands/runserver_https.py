"""Development server over HTTPS, for browsers that force https:// on 127.0.0.1/localhost.

Usage:  python manage.py runserver_https            # https://127.0.0.1:8000/
        python manage.py runserver_https 8443        # any addr:port, like runserver

A self-signed certificate for localhost/127.0.0.1 is created in .certs/ on first run, so the
browser will warn once ("Your connection is not private") — choose Advanced → Proceed.
"""

import shutil
import ssl
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import Command as StaticRunserver
from django.core.management.base import CommandError
from django.core.servers.basehttp import WSGIServer

CERT_DIR = Path(settings.BASE_DIR) / ".certs"


def ensure_cert() -> tuple[Path, Path]:
    cert, key = CERT_DIR / "localhost.pem", CERT_DIR / "localhost-key.pem"
    if cert.exists() and key.exists():
        return cert, key
    if not shutil.which("openssl"):
        raise CommandError("openssl is needed to create a local certificate (apt install openssl).")
    CERT_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
            "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=Beezy Markets (local)",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


class SecureWSGIServer(WSGIServer):
    ssl_context: ssl.SSLContext | None = None

    def get_request(self):
        sock, addr = self.socket.accept()
        # Handshake lazily in the request thread so one slow client can't stall the server.
        return self.ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def setup_environ(self):
        super().setup_environ()
        self.base_environ["HTTPS"] = "on"  # request.is_secure() → True

    def handle_error(self, request, client_address):
        # Plain-http requests to the https port and aborted handshakes are not worth a traceback.
        import sys

        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Command(StaticRunserver):
    help = "Run the development server over HTTPS with a self-signed localhost certificate."
    protocol = "https"
    server_cls = SecureWSGIServer

    def inner_run(self, *args, **options):
        cert, key = ensure_cert()
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        SecureWSGIServer.ssl_context = ctx
        self.stdout.write(f"Using self-signed certificate {cert} — accept the browser warning once.")
        super().inner_run(*args, **options)
