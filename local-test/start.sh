#!/bin/bash
# Start local test environment

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Security Camera Motion Detection - Local Test ==="

# Check for .env file
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "Please edit .env with your stream URL and settings"
fi

# Load environment variables
export $(grep -v '^#' .env | xargs)

# Create output directories
mkdir -p recordings state

# Copy Python files from security-camera add-on
echo "Copying Python files..."
cp ../security-camera/motion_detector.py .
cp ../security-camera/recording_manager.py .
cp ../security-camera/ha_integration.py .
cp ../security-camera/http_server.py .

# Build and start container
echo "Building Docker image..."
docker-compose build

echo "Starting container..."
docker-compose up -d

echo ""
echo "=== Container started ==="
echo "Recordings will be saved to: $SCRIPT_DIR/recordings/"
echo "State file: $SCRIPT_DIR/state/security_state.json"
echo ""
echo "API endpoints:"
echo "  - Health:     http://localhost:8081/api/health"
echo "  - State:      http://localhost:8081/api/state"
echo "  - Recordings: http://localhost:8081/api/recordings"
echo "  - Settings:   http://localhost:8081/api/settings"
echo ""
echo "Quick ROI adjustment:"
echo "  curl -X POST localhost:8081/api/settings/roi/20/80   # wider zone"
echo "  curl -X POST localhost:8081/api/settings/roi/40/60   # narrower zone"
echo "  curl -X POST localhost:8081/api/settings/threshold/3000  # more sensitive"
echo ""
echo "View logs: docker-compose logs -f"
echo "Stop: ./stop.sh"
