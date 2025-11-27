#!/usr/bin/env python3
"""
HTTP server for serving recordings and API endpoints.
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import logging

logger = logging.getLogger(__name__)


class SecurityHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler with CORS and API endpoints."""

    recordings_path = "/share/security_recordings"
    state_file = "/share/security_state.json"
    settings_file = "/share/security_settings.json"

    def __init__(self, *args, **kwargs):
        # Set directory to recordings path
        super().__init__(*args, directory=self.recordings_path, **kwargs)

    def handle(self):
        """Handle request with graceful connection error handling."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError) as e:
            # Client disconnected mid-transfer - expected behavior, don't spam logs
            logger.debug(f"Client disconnected: {e}")

    def log_message(self, format, *args):
        """Log to stderr for Home Assistant."""
        logger.info("%s - %s", self.address_string(), format % args)

    def send_cors_headers(self):
        """Add CORS headers for cross-origin requests."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
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
        elif self.path == '/api/settings':
            self.handle_api_get_settings()
        elif self.path.startswith('/api/'):
            self.send_error(404, "API endpoint not found")
        else:
            # Serve static files (recordings, thumbnails)
            super().do_GET()

    def do_POST(self):
        """Handle POST requests."""
        if self.path == '/api/settings':
            self.handle_api_set_settings()
        elif self.path.startswith('/api/settings/'):
            self.handle_api_quick_settings()
        else:
            self.send_error(404, "API endpoint not found")

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

    def _get_settings(self) -> dict:
        """Read current settings from file."""
        defaults = {
            "roi_x_start": 33,
            "roi_x_end": 66,
            "roi_y_start": 5,
            "roi_y_end": 95,
            "motion_threshold": 5000
        }
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    saved = json.load(f)
                    defaults.update(saved)
        except Exception as e:
            logger.warning(f"Error reading settings: {e}")
        return defaults

    def _save_settings(self, settings: dict):
        """Save settings to file."""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info(f"Settings saved: {settings}")
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            raise

    def handle_api_get_settings(self):
        """Return current motion detection settings."""
        try:
            settings = self._get_settings()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(settings, indent=2).encode())
        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_set_settings(self):
        """Update motion detection settings via JSON body."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            new_settings = json.loads(body)

            # Validate and merge settings
            current = self._get_settings()
            if 'roi_x_start' in new_settings:
                current['roi_x_start'] = max(0, min(100, int(new_settings['roi_x_start'])))
            if 'roi_x_end' in new_settings:
                current['roi_x_end'] = max(0, min(100, int(new_settings['roi_x_end'])))
            if 'roi_y_start' in new_settings:
                current['roi_y_start'] = max(0, min(100, int(new_settings['roi_y_start'])))
            if 'roi_y_end' in new_settings:
                current['roi_y_end'] = max(0, min(100, int(new_settings['roi_y_end'])))
            if 'motion_threshold' in new_settings:
                current['motion_threshold'] = max(0, int(new_settings['motion_threshold']))

            self._save_settings(current)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "settings": current}).encode())
        except Exception as e:
            self.send_error(400, str(e))

    def handle_api_quick_settings(self):
        """Quick settings via URL:
        /api/settings/roi/33/66 - set X axis ROI
        /api/settings/roi_y/5/95 - set Y axis ROI (crop timestamp)
        /api/settings/threshold/5000 - set threshold
        """
        try:
            parts = self.path.split('/')
            current = self._get_settings()

            if len(parts) >= 5 and parts[3] == 'roi':
                current['roi_x_start'] = max(0, min(100, int(parts[4])))
                if len(parts) >= 6:
                    current['roi_x_end'] = max(0, min(100, int(parts[5])))
            elif len(parts) >= 5 and parts[3] == 'roi_y':
                current['roi_y_start'] = max(0, min(100, int(parts[4])))
                if len(parts) >= 6:
                    current['roi_y_end'] = max(0, min(100, int(parts[5])))
            elif len(parts) >= 5 and parts[3] == 'threshold':
                current['motion_threshold'] = max(0, int(parts[4]))
            else:
                self.send_error(400, "Use /api/settings/roi/{x1}/{x2}, /api/settings/roi_y/{y1}/{y2}, or /api/settings/threshold/{value}")
                return

            self._save_settings(current)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "settings": current}).encode())
        except Exception as e:
            self.send_error(400, str(e))


def run_server(port: int = 8081, recordings_path: str = "/share/security_recordings", state_file: str = "/share/security_state.json", settings_file: str = "/share/security_settings.json"):
    """Run the HTTP server."""
    SecurityHTTPHandler.recordings_path = recordings_path
    SecurityHTTPHandler.state_file = state_file
    SecurityHTTPHandler.settings_file = settings_file

    # Ensure directory exists
    Path(recordings_path).mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('0.0.0.0', port), SecurityHTTPHandler)
    logger.info(f"HTTP server starting on port {port}")
    logger.info(f"Serving recordings from: {recordings_path}")
    logger.info(f"Settings file: {settings_file}")
    logger.info(f"API endpoints: /api/state, /api/recordings, /api/health, /api/settings")

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
