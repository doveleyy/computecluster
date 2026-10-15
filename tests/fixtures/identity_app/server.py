import html
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

REQUIRED = (
    "APP_ID",
    "APP_BASE_PATH",
    "PLATFORM_API_URL",
    "APP_CREDENTIAL_FILE",
    "APP_STATE_DIR",
    "TZ",
)


class IdentityServer(ThreadingHTTPServer):
    def __init__(self, address, configuration):
        self.configuration = configuration
        self.streams = 0
        self.stream_lock = threading.Lock()
        self.requests = 0
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, format, *args):
        pass

    def respond(self, status, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, status, value, headers=()):
        self.respond(
            status,
            json.dumps(value).encode(),
            [("Content-Type", "application/json"), *headers],
        )

    def user(self):
        config = self.server.configuration
        assertion = self.headers.get("X-Platform-User-Context")
        if not assertion:
            return 401, None
        try:
            credential = Path(config["APP_CREDENTIAL_FILE"]).read_text().strip()
            upstream = Request(
                config["PLATFORM_API_URL"] + "/user",
                headers={
                    "Authorization": "Bearer " + credential,
                    "X-Platform-User-Context": assertion,
                },
            )
            with build_opener(ProxyHandler({})).open(upstream, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, None
        except (OSError, URLError, ValueError):
            return 503, None

    def do_GET(self):
        config = self.server.configuration
        url = urlsplit(self.path)
        with self.server.stream_lock:
            self.server.requests += 1
            count = self.server.requests
        if url.path == "/request-count":
            self.json_response(200, {"requests": count})
            return
        if url.path == "/health":
            self.respond(200, b"ok")
            return
        if url.path == "/ready":
            state = Path(config["APP_STATE_DIR"])
            self.respond(
                200 if state.is_dir() and os.access(state, os.W_OK) else 503, b"ready"
            )
            return
        if url.path == "/version":
            self.json_response(
                200, {"service": config["APP_ID"], "version": "fixture-v1"}
            )
            return
        status, user = self.user()
        if status != 200:
            self.respond(
                status, f'<main data-refused="{status}">Access refused</main>'.encode()
            )
            return
        if url.path == "/echo":
            excluded = ("tailscale-", "x-platform-", "x-forwarded-")
            blocked = {
                "authorization",
                "x-api-token",
                "x-service-identity-token",
                "cookie",
            }
            headers = {
                name.lower(): value
                for name, value in self.headers.items()
                if name.lower() not in blocked and not name.lower().startswith(excluded)
            }
            self.json_response(
                200,
                {
                    "method": self.command,
                    "path": url.path,
                    "query": url.query,
                    "headers": headers,
                    "context_count": len(
                        self.headers.get_all("X-Platform-User-Context", [])
                    ),
                    "platform_cookie_present": "home_platform"
                    in self.headers.get("Cookie", "").lower(),
                    "app_cookie_present": "fixture_session="
                    in self.headers.get("Cookie", ""),
                    "user": user,
                },
            )
            return
        if url.path == "/cookie":
            case = parse_qs(url.query).get("case", ["inside"])[0]
            values = {
                "inside": "fixture_session=one; Path=" + config["APP_BASE_PATH"],
                "reserved": "home_platform_dashboard=bad; Path="
                + config["APP_BASE_PATH"],
                "outside": "fixture_session=one; Path=/",
                "domain": "fixture_session=one; Domain=platform.test; Path="
                + config["APP_BASE_PATH"],
                "pathless": "fixture_session=one",
            }
            self.respond(
                200, b"cookie", [("Set-Cookie", values.get(case, values["inside"]))]
            )
            return
        if url.path == "/redirect":
            self.respond(
                302,
                b"",
                [("Location", "http://" + self.headers["Host"] + "/landed?q=yes")],
            )
            return
        if url.path == "/stream-status":
            with self.server.stream_lock:
                active = self.server.streams
            self.json_response(200, {"active": active})
            return
        if url.path in {"/stream", "/long-stream", "/broken-stream"}:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = (b"first\n", b"second\n", b"third\n")
            if url.path == "/long-stream":
                chunks *= 20
            with self.server.stream_lock:
                self.server.streams += 1
            try:
                for chunk in chunks:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    if url.path == "/broken-stream":
                        self.close_connection = True
                        return
                    time.sleep(0.2)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                self.close_connection = True
            finally:
                with self.server.stream_lock:
                    self.server.streams -= 1
            return
        rendered = (
            f'<main data-user-id="{html.escape(user["id"], quote=True)}">'
            f"{html.escape(user['display_name'])}</main>"
        )
        self.respond(
            200, rendered.encode(), [("Content-Type", "text/html; charset=utf-8")]
        )

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        status, user = self.user()
        if status != 200:
            self.json_response(status, {"detail": "Access refused"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.json_response(
            200, {"method": self.command, "body": body.decode(), "user": user}
        )


def main():
    configuration = {name: os.environ[name] for name in REQUIRED}
    IdentityServer(("0.0.0.0", 8080), configuration).serve_forever()


if __name__ == "__main__":
    main()
