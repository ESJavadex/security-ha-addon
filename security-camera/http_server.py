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

# Embedded HTML for the recordings viewer UI
INDEX_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Security Camera Recordings</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        .header {
            background: #16213e;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
            border-bottom: 1px solid #0f3460;
        }
        .header h1 {
            font-size: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #4ade80;
        }
        .status-dot.recording { background: #ef4444; animation: pulse 1s infinite; }
        .status-dot.motion { background: #f59e0b; animation: pulse 0.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .filters {
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        .filters label { font-size: 0.9rem; color: #aaa; }
        .filters input, .filters select {
            background: #0f3460;
            border: 1px solid #1a4a7a;
            color: #eee;
            padding: 0.5rem;
            border-radius: 4px;
        }
        .container { padding: 1.5rem; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1.5rem;
        }
        .card {
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            border: 1px solid #0f3460;
        }
        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 25px rgba(0,0,0,0.3);
        }
        .card-thumb {
            position: relative;
            aspect-ratio: 16/9;
            background: #0a0a1a;
        }
        .card-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .card-thumb .play-icon {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 60px;
            height: 60px;
            background: rgba(0,0,0,0.7);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .card:hover .play-icon { opacity: 1; }
        .play-icon::after {
            content: '';
            border-style: solid;
            border-width: 12px 0 12px 20px;
            border-color: transparent transparent transparent #fff;
            margin-left: 4px;
        }
        .card-thumb .duration {
            position: absolute;
            bottom: 8px;
            right: 8px;
            background: rgba(0,0,0,0.8);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.8rem;
        }
        .card-info {
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }
        .card-details { flex: 1; }
        .card-date { font-size: 1rem; font-weight: 500; }
        .card-time { font-size: 0.85rem; color: #aaa; margin-top: 0.25rem; }
        .card-size { font-size: 0.8rem; color: #666; margin-top: 0.5rem; }
        .btn-delete {
            background: transparent;
            border: 1px solid #dc2626;
            color: #dc2626;
            padding: 0.4rem 0.6rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.2s;
            opacity: 0;
        }
        .card:hover .btn-delete { opacity: 1; }
        .btn-delete:hover {
            background: #dc2626;
            color: #fff;
        }
        .confirm-dialog {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }
        .confirm-dialog.active { display: flex; }
        .confirm-box {
            background: #16213e;
            padding: 2rem;
            border-radius: 8px;
            text-align: center;
            max-width: 400px;
            border: 1px solid #0f3460;
        }
        .confirm-box h3 { margin-bottom: 1rem; color: #ef4444; }
        .confirm-box p { margin-bottom: 1.5rem; color: #aaa; }
        .confirm-buttons { display: flex; gap: 1rem; justify-content: center; }
        .confirm-buttons button {
            padding: 0.6rem 1.5rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1rem;
            border: none;
        }
        .btn-cancel { background: #374151; color: #fff; }
        .btn-cancel:hover { background: #4b5563; }
        .btn-confirm-delete { background: #dc2626; color: #fff; }
        .btn-confirm-delete:hover { background: #b91c1c; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.9);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            max-width: 90vw;
            max-height: 90vh;
            position: relative;
        }
        .modal video {
            max-width: 90vw;
            max-height: 80vh;
            border-radius: 8px;
        }
        .modal-close {
            position: absolute;
            top: -40px;
            right: 0;
            background: none;
            border: none;
            color: #fff;
            font-size: 2rem;
            cursor: pointer;
            padding: 0.5rem;
        }
        .modal-info {
            color: #aaa;
            text-align: center;
            margin-top: 1rem;
        }
        .modal-actions {
            display: flex;
            justify-content: center;
            gap: 1rem;
            margin-top: 1rem;
        }
        .modal-delete {
            background: #dc2626;
            border: none;
            color: #fff;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9rem;
        }
        .modal-delete:hover { background: #b91c1c; }
        .modal-nav {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background: rgba(255,255,255,0.1);
            border: none;
            color: #fff;
            font-size: 2rem;
            padding: 1rem;
            cursor: pointer;
            border-radius: 4px;
        }
        .modal-nav:hover { background: rgba(255,255,255,0.2); }
        .modal-nav.prev { left: -60px; }
        .modal-nav.next { right: -60px; }
        .empty {
            text-align: center;
            padding: 4rem;
            color: #666;
        }
        .empty-icon { font-size: 4rem; margin-bottom: 1rem; }
        .stats {
            display: flex;
            gap: 2rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
        }
        .stat {
            background: #16213e;
            padding: 1rem 1.5rem;
            border-radius: 8px;
            border: 1px solid #0f3460;
        }
        .stat-value { font-size: 1.5rem; font-weight: bold; }
        .stat-label { font-size: 0.8rem; color: #aaa; }
    </style>
</head>
<body>
    <div class="header">
        <h1>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
            </svg>
            Security Recordings
        </h1>
        <div class="status">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Connecting...</span>
        </div>
        <div class="filters">
            <label>Filter by date:</label>
            <input type="date" id="filterDate">
            <select id="filterSort">
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
            </select>
        </div>
    </div>
    <div class="container">
        <div class="stats" id="stats"></div>
        <div class="grid" id="grid"></div>
        <div class="empty" id="empty" style="display:none;">
            <div class="empty-icon">📹</div>
            <p>No recordings found</p>
        </div>
    </div>
    <div class="modal" id="modal">
        <div class="modal-content">
            <button class="modal-close" onclick="closeModal()">&times;</button>
            <button class="modal-nav prev" onclick="navVideo(-1)">&#8249;</button>
            <video id="player" controls></video>
            <button class="modal-nav next" onclick="navVideo(1)">&#8250;</button>
            <div class="modal-info" id="modalInfo"></div>
            <div class="modal-actions">
                <button class="modal-delete" onclick="confirmDeleteCurrent()">Delete Recording</button>
            </div>
        </div>
    </div>
    <div class="confirm-dialog" id="confirmDialog">
        <div class="confirm-box">
            <h3>Delete Recording?</h3>
            <p id="confirmMessage">This will permanently delete the recording.</p>
            <div class="confirm-buttons">
                <button class="btn-cancel" onclick="cancelDelete()">Cancel</button>
                <button class="btn-confirm-delete" onclick="executeDelete()">Delete</button>
            </div>
        </div>
    </div>
    <script>
        let recordings = [];
        let filteredRecordings = [];
        let currentIndex = 0;

        function formatDate(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
        }
        function formatTime(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        function formatDuration(secs) {
            if (!secs) return '--:--';
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60);
            return `${m}:${s.toString().padStart(2, '0')}`;
        }
        function formatSize(bytes) {
            if (!bytes) return '';
            const mb = bytes / 1024 / 1024;
            return `${mb.toFixed(1)} MB`;
        }
        function getDateString(ts) {
            const d = new Date(ts * 1000);
            return d.toISOString().split('T')[0];
        }

        async function loadRecordings() {
            try {
                const res = await fetch('/api/recordings');
                recordings = await res.json();
                applyFilters();
            } catch (e) {
                console.error('Failed to load recordings:', e);
            }
        }

        async function loadState() {
            try {
                const res = await fetch('/api/state');
                const state = await res.json();
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                dot.className = 'status-dot';
                if (state.recording?.is_recording) {
                    dot.classList.add('recording');
                    text.textContent = 'Recording...';
                } else if (state.motion?.detected) {
                    dot.classList.add('motion');
                    text.textContent = 'Motion detected';
                } else {
                    text.textContent = 'Monitoring';
                }
            } catch (e) {
                document.getElementById('statusText').textContent = 'Offline';
            }
        }

        function applyFilters() {
            const dateFilter = document.getElementById('filterDate').value;
            const sort = document.getElementById('filterSort').value;

            filteredRecordings = [...recordings];

            if (dateFilter) {
                filteredRecordings = filteredRecordings.filter(r => getDateString(r.start_time) === dateFilter);
            }

            filteredRecordings.sort((a, b) => sort === 'newest' ? b.start_time - a.start_time : a.start_time - b.start_time);

            renderGrid();
            renderStats();
        }

        function renderStats() {
            const totalSize = recordings.reduce((sum, r) => sum + (r.filesize || 0), 0);
            const totalDuration = recordings.reduce((sum, r) => sum + (r.duration || 0), 0);
            document.getElementById('stats').innerHTML = `
                <div class="stat"><div class="stat-value">${recordings.length}</div><div class="stat-label">Total Recordings</div></div>
                <div class="stat"><div class="stat-value">${formatSize(totalSize)}</div><div class="stat-label">Total Size</div></div>
                <div class="stat"><div class="stat-value">${Math.floor(totalDuration / 60)}m</div><div class="stat-label">Total Duration</div></div>
                <div class="stat"><div class="stat-value">${filteredRecordings.length}</div><div class="stat-label">Showing</div></div>
            `;
        }

        function renderGrid() {
            const grid = document.getElementById('grid');
            const empty = document.getElementById('empty');

            if (filteredRecordings.length === 0) {
                grid.innerHTML = '';
                empty.style.display = 'block';
                return;
            }
            empty.style.display = 'none';

            grid.innerHTML = filteredRecordings.map((r, i) => {
                const thumbName = r.thumbnail ? r.thumbnail.split('/').pop() : r.filename.replace('.mp4', '.jpg');
                return `
                    <div class="card">
                        <div class="card-thumb" onclick="openModal(${i})">
                            <img src="/${thumbName}" alt="Thumbnail" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 320 180%22><rect fill=%22%23333%22 width=%22320%22 height=%22180%22/><text x=%22160%22 y=%2290%22 fill=%22%23666%22 text-anchor=%22middle%22>No preview</text></svg>'">
                            <div class="play-icon"></div>
                            <div class="duration">${formatDuration(r.duration)}</div>
                        </div>
                        <div class="card-info">
                            <div class="card-details" onclick="openModal(${i})">
                                <div class="card-date">${formatDate(r.start_time)}</div>
                                <div class="card-time">${formatTime(r.start_time)}</div>
                                <div class="card-size">${formatSize(r.filesize)}</div>
                            </div>
                            <button class="btn-delete" onclick="confirmDelete('${r.filename}', event)">Delete</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function openModal(index) {
            currentIndex = index;
            const r = filteredRecordings[index];
            const player = document.getElementById('player');
            player.src = '/' + r.filename;
            document.getElementById('modalInfo').textContent = `${formatDate(r.start_time)} at ${formatTime(r.start_time)} - ${formatDuration(r.duration)}`;
            document.getElementById('modal').classList.add('active');
            player.play();
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('active');
            document.getElementById('player').pause();
        }

        function navVideo(dir) {
            currentIndex = (currentIndex + dir + filteredRecordings.length) % filteredRecordings.length;
            openModal(currentIndex);
        }

        document.addEventListener('keydown', (e) => {
            if (!document.getElementById('modal').classList.contains('active')) return;
            if (e.key === 'Escape') closeModal();
            if (e.key === 'ArrowLeft') navVideo(-1);
            if (e.key === 'ArrowRight') navVideo(1);
        });

        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') closeModal();
        });

        document.getElementById('filterDate').addEventListener('change', applyFilters);
        document.getElementById('filterSort').addEventListener('change', applyFilters);

        let deleteFilename = null;

        function confirmDelete(filename, event) {
            if (event) event.stopPropagation();
            deleteFilename = filename;
            document.getElementById('confirmMessage').textContent = `This will permanently delete "${filename}"`;
            document.getElementById('confirmDialog').classList.add('active');
        }

        function confirmDeleteCurrent() {
            const r = filteredRecordings[currentIndex];
            if (r) confirmDelete(r.filename);
        }

        function cancelDelete() {
            deleteFilename = null;
            document.getElementById('confirmDialog').classList.remove('active');
        }

        async function executeDelete() {
            if (!deleteFilename) return;
            try {
                const res = await fetch(`/api/recordings/${deleteFilename}`, { method: 'DELETE' });
                if (res.ok) {
                    cancelDelete();
                    closeModal();
                    await loadRecordings();
                } else {
                    const err = await res.text();
                    alert(`Failed to delete: ${err}`);
                }
            } catch (e) {
                alert(`Error: ${e.message}`);
            }
        }

        loadRecordings();
        loadState();
        setInterval(loadState, 5000);
        setInterval(loadRecordings, 30000);
    </script>
</body>
</html>
'''


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
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')

    def do_OPTIONS(self):
        """Handle preflight CORS requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        # Serve index page at root
        if self.path == '/' or self.path == '/index.html':
            self.handle_index()
        # API endpoints
        elif self.path == '/api/state':
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

    def handle_index(self):
        """Serve the recordings viewer UI."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(INDEX_HTML.encode('utf-8'))

    def do_POST(self):
        """Handle POST requests."""
        if self.path == '/api/settings':
            self.handle_api_set_settings()
        elif self.path.startswith('/api/settings/'):
            self.handle_api_quick_settings()
        else:
            self.send_error(404, "API endpoint not found")

    def do_DELETE(self):
        """Handle DELETE requests."""
        if self.path.startswith('/api/recordings/'):
            self.handle_api_delete_recording()
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

    def handle_api_delete_recording(self):
        """Delete a recording by filename: DELETE /api/recordings/{filename}"""
        try:
            # Extract filename from path
            parts = self.path.split('/')
            if len(parts) < 4:
                self.send_error(400, "Filename required")
                return

            filename = parts[3]
            if not filename.endswith('.mp4'):
                self.send_error(400, "Invalid filename")
                return

            # Load metadata
            metadata_file = Path(self.recordings_path) / "recordings.json"
            if not metadata_file.exists():
                self.send_error(404, "No recordings found")
                return

            with open(metadata_file, 'r') as f:
                recordings = json.load(f)

            # Find and remove the recording
            recording_to_delete = None
            for r in recordings:
                if r.get('filename') == filename:
                    recording_to_delete = r
                    break

            if not recording_to_delete:
                self.send_error(404, f"Recording not found: {filename}")
                return

            # Delete video file
            video_path = Path(self.recordings_path) / filename
            if video_path.exists():
                video_path.unlink()
                logger.info(f"Deleted video: {video_path}")

            # Delete thumbnail
            thumb_path = video_path.with_suffix('.jpg')
            if thumb_path.exists():
                thumb_path.unlink()
                logger.info(f"Deleted thumbnail: {thumb_path}")

            # Update metadata
            recordings.remove(recording_to_delete)
            with open(metadata_file, 'w') as f:
                json.dump(recordings, f, indent=2)

            logger.info(f"Recording deleted: {filename}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "deleted": filename}).encode())

        except Exception as e:
            logger.error(f"Error deleting recording: {e}")
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
