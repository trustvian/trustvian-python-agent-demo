#!/usr/bin/env python3
"""Deterministic local stand-ins for the services the support agent calls.

One process serves four logical services, told apart by the ``Host`` header:
``crm.localhost``, ``knowledge.localhost``, ``mail.localhost`` and
``export.localhost``. That is what gives each action a distinct
``server.address`` in the emitted telemetry, and therefore a distinct
behavioral identity in Trustvian.

Why hostnames rather than 127.0.0.2-127.0.0.5: those addresses cannot be
bound on macOS without ``sudo ifconfig lo0 alias``, and this demo must run
with no machine-wide configuration. ``*.localhost`` resolves to loopback on
macOS and on systemd Linux with no ``/etc/hosts`` edit and no DNS query.

Nothing here is random, timed or stateful across requests. A demo whose
evidence changed between runs would make a behavioral comparison impossible
to reason about.
"""

from __future__ import annotations

import argparse
import json
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler

# Fixed fixtures. Deterministic by construction: the same request always
# produces the same response, so two runs of the same agent behavior produce
# identical evidence.
CUSTOMERS = {
    "42": {"id": "42", "name": "Ada Lovelace", "tier": "gold", "open_tickets": 1},
    "43": {"id": "43", "name": "Alan Turing", "tier": "silver", "open_tickets": 3},
    "44": {"id": "44", "name": "Grace Hopper", "tier": "gold", "open_tickets": 0},
}

ARTICLES = [
    {"id": "kb-1", "title": "Resetting your password", "score": 0.91},
    {"id": "kb-2", "title": "Billing cycle explained", "score": 0.77},
]


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so connections are reusable; the agent uses a requests.Session
    # and a downgrade would open a socket per call for no reason.
    protocol_version = "HTTP/1.1"

    # Silence the default stderr access log. The demo's own output is the
    # story; a request log interleaved with it is noise.
    def log_message(self, *args):  # noqa: D102 - intentionally silent
        pass

    def _service(self) -> str:
        """Return the logical service from the Host header, without its port."""
        host = self.headers.get("Host", "")
        return host.split(":", 1)[0].lower()

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = self.path.split("?", 1)[0]

        # Answered on any Host: this is the readiness probe the demo scripts
        # wait on, and they have no reason to pick a service to ask.
        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return

        service = self._service()
        if service == "crm.localhost" and path.startswith("/crm/customers/"):
            customer_id = path.rsplit("/", 1)[-1]
            record = CUSTOMERS.get(customer_id)
            if record is None:
                self._send(404, {"error": "no such customer"})
                return
            self._send(200, {"customer": record})
            return

        if service == "knowledge.localhost" and path == "/knowledge/articles":
            self._send(200, {"articles": ARTICLES})
            return

        self._send(404, {"error": "no route", "service": service, "path": path})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        service = self._service()

        if service == "mail.localhost" and path == "/mail/send":
            self._send(202, {"queued": True, "to": body.get("to", "")})
            return

        if service == "export.localhost" and path == "/export/customers":
            self._send(200, {
                "exported": len(body.get("customer_ids", [])),
                "format": body.get("format", "csv"),
            })
            return

        self._send(404, {"error": "no route", "service": service, "path": path})


class DualStackServer(socketserver.ThreadingTCPServer):
    """Serves IPv4 and IPv6 on one socket.

    ``*.localhost`` resolves to ``::1`` first on macOS and to ``127.0.0.1``
    elsewhere, so a v4-only listener answers on some machines and not others.
    Binding ``[::]`` with ``IPV6_V6ONLY`` off accepts both.
    """

    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=0,
        help="port to bind; 0 lets the OS choose and the chosen port is printed")
    args = parser.parse_args()

    server = DualStackServer(("::", args.port), Handler)
    port = server.server_address[1]

    # Printed and flushed before serving, so a caller can treat this line as
    # the readiness signal rather than polling for a bind that may never
    # happen.
    print(f"listening {port}", flush=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
