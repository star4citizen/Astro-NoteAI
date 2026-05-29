#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def read_password(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"Password file is empty: {path}")
    return value


class AuthProxyHandler(BaseHTTPRequestHandler):
    server_version = "AstroWikiAuthProxy/0.1"

    def expected_auth(self) -> str:
        token = f"{self.server.username}:{self.server.password}".encode("utf-8")  # type: ignore[attr-defined]
        return "Basic " + base64.b64encode(token).decode("ascii")

    def is_authorized(self) -> bool:
        return self.headers.get("Authorization") == self.expected_auth()

    def require_auth(self) -> None:
        body = b"Authentication required.\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Astro Wiki"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.proxy()

    def do_HEAD(self) -> None:
        self.proxy()

    def do_POST(self) -> None:
        self.proxy()

    def proxy(self) -> None:
        if not self.is_authorized():
            self.require_auth()
            return

        target_base = self.server.target.rstrip("/") + "/"  # type: ignore[attr-defined]
        target_url = urljoin(target_base, self.path.lstrip("/"))
        body = None
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        headers["X-Forwarded-Proto"] = "https"

        request = Request(target_url, data=body, headers=headers, method=self.command)
        try:
            with urlopen(request, timeout=self.server.timeout_seconds) as response:  # type: ignore[attr-defined]
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_BY_HOP_HEADERS:
                        self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response.read())
        except HTTPError as exc:
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(exc.read())
        except URLError as exc:
            message = f"Upstream unavailable: {exc.reason}\n".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class AuthProxyServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        target: str,
        username: str,
        password: str,
        timeout_seconds: int,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.target = target
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description="Basic Auth reverse proxy for the Astro-Note AI UI.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8766)
    parser.add_argument("--target", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="astro")
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    password = read_password(Path(args.password_file))
    AuthProxyServer.allow_reuse_address = True
    server = AuthProxyServer(
        (args.listen_host, args.listen_port),
        AuthProxyHandler,
        target=args.target,
        username=args.username,
        password=password,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        f"Astro Wiki auth proxy: http://{args.listen_host}:{args.listen_port} -> {args.target}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
