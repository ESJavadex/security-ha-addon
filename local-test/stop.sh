#!/bin/bash
# Stop local test environment

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Stopping containers..."
docker-compose down

echo "Cleaning up copied Python files..."
rm -f motion_detector.py recording_manager.py ha_integration.py http_server.py

echo "Done. Recordings are preserved in ./recordings/"
