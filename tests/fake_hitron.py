"""A local stand-in for a Hitron CODA web API, serving the anonymised fixtures."""

import copy
import email.utils
import http.server
import json
import threading
import time
import urllib.parse
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "hitron"


def load(endpoint):
    return json.loads((FIXTURES / f"{endpoint}.json").read_text(encoding="utf-8"))


class FakeHitron:
    """Login sets a cookie; reads without it answer errCode 001 like the real
    modem. drop_ofdma_after makes OFDMA channel 1 leave OPERATE after that many
    UsOfdm reads. clock_offset shifts the Date header (modem clock skew)."""

    def __init__(self, username="admin", password="secret", drop_ofdma_after=None, clock_offset=0,
                 foreign_title=None):
        self.username, self.password = username, password
        self.drop_ofdma_after = drop_ofdma_after
        self.clock_offset = clock_offset
        self.reads = {}
        self.logins = 0
        self.login_attempts = 0
        self.answer_errors = False   # answer every read with errCode 002
        # Imitates another brand: HTML for everything, like a CBN login page.
        self.foreign_title = foreign_title
        self.foreign_status = 200
        self.token = "abc123"
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def date_time_string(self, timestamp=None):
                # send_response adds this Date header itself; shift it here.
                return email.utils.formatdate(time.time() - fake.clock_offset, usegmt=True)

            def _send(self, obj, cookie=None):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", f"session={cookie}; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self):
                body = (f"<html><head><title>{fake.foreign_title}</title></head>"
                        "<body>CBN login<form></form></body></html>").encode()
                self.send_response(fake.foreign_status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                if fake.foreign_title:
                    if self.path.startswith("/1/Device/Users/Login"):
                        fake.login_attempts += 1
                    return self._send_html()
                if self.path.startswith("/1/Device/Users/Login"):
                    fake.login_attempts += 1
                    creds = json.loads(form.get("model", ["{}"])[0])
                    if creds == {"username": fake.username, "password": fake.password}:
                        fake.logins += 1
                        self._send({"result": "success"}, cookie=fake.token)
                    else:
                        self._send({"result": "fail"})
                else:
                    self._send({"result": "success"})

            def do_GET(self):
                if fake.foreign_title:
                    return self._send_html()
                endpoint = self.path.split("/1/Device/CM/", 1)[-1].split("?", 1)[0]
                if f"session={fake.token}" not in (self.headers.get("Cookie") or ""):
                    return self._send({"errCode": "001", "errMsg": "not logged in"})
                fake.reads[endpoint] = fake.reads.get(endpoint, 0) + 1
                if fake.answer_errors:
                    return self._send({"errCode": "002", "errMsg": "busy"})
                data = copy.deepcopy(load(endpoint))
                if (endpoint == "UsOfdm" and fake.drop_ofdma_after is not None
                        and fake.reads[endpoint] > fake.drop_ofdma_after):
                    data["Freq_List"][1]["state"] = "   NOT_READY"
                self._send(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
