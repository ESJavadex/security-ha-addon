#!/usr/bin/env python3
"""
HTTP server for serving recordings and API endpoints.
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class SecurityHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler with CORS and API endpoints."""

    recordings_path = "/share/security_recordings"
    state_file = "/share/security_state.json"

    def __init__(self, *args, **kwargs):
        # Set directory to recordings path
        super().__init__(*args, directory=self.recordings_path, **kwargs)

    def log_message(self, format, *args):
        """Log to stderr for Home Assistant."""
        logger.info("%s - %s", self.address_string(), format % args)

    def send_cors_headers(self):
        """Add CORS headers for cross-origin requests."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')

    def do_OPTIONS(self):
        """Handle preflight CORS requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        # API endpoints
        if self.path == '/api/state':
            self.handle_api_state()
        elif self.path == '/api/recordings':
            self.handle_api_recordings()
        elif self.path == '/api/health':
            self.handle_api_health()
        elif self.path.startswith('/api/'):
            self.send_error(404, "API endpoint not found")
        else:
            # Serve static files (recordings, thumbnails)
            super().do_GET()

    def end_headers(self):
        """Add CORS headers to all responses."""
        self.send_cors_headers()
        super().end_headers()

    def handle_api_state(self):
        """Return current sensor state."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
            else:
                state = {"error": "State file not found"}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(state, indent=2).encode())

        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_recordings(self):
        """Return list of recordings."""
        try:
            metadata_file = Path(self.recordings_path) / "recordings.json"
            if metadata_file.exists():
                with open(metadata_file, 'r') as f:
                    recordings = json.load(f)
            else:
                recordings = []

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(recordings, indent=2).encode())

        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_health(self):
        """Health check endpoint."""
        health = {
            "status": "ok",
            "service": "security-camera-motion",
            "recordings_path": self.recordings_path,
            "state_file": self.state_file
        }

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(health, indent=2).encode())


def run_server(port: int = 8081, recordings_path: str = "/share/security_recordings", state_file: str = "/share/security_state.json"):
    """Run the HTTP server."""
    SecurityHTTPHandler.recordings_path = recordings_path
    SecurityHTTPHandler.state_file = state_file

    # Ensure directory exists
    Path(recordings_path).mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('0.0.0.0', port), SecurityHTTPHandler)
    logger.info(f"HTTP server starting on port {port}")
    logger.info(f"Serving recordings from: {recordings_path}")
    logger.info(f"API endpoints: /api/state, /api/recordings, /api/health")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down")
        server.shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stderr
    )

    port = int(os.environ.get('HTTP_PORT', 8081))
    recordings = os.environ.get('RECORDINGS_PATH', '/share/security_recordings')
    state = os.environ.get('STATE_FILE', '/share/security_state.json')

    run_server(port, recordings, state)
