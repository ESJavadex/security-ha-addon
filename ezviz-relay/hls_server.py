#!/usr/bin/env python3
"""Serve the HLS directory with CORS (hls.js in HA dashboards) and no caching of the playlist."""
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.split("?")[0].endswith(".m3u8"):
            self.send_header("Cache-Control", "no-cache, no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    directory = os.environ.get("HLS_DIR", "/var/lib/seccam/hls")
    port = int(os.environ.get("HLS_PORT", 8080))
    os.makedirs(directory, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=directory))
    server.daemon_threads = True
    print(f"Serving {directory} on :{port}", file=sys.stderr)
    server.serve_forever()
