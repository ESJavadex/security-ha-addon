#!/usr/bin/env bashio

# Security Camera Motion Detection Add-on
# Main orchestrator script

set -e

echo "[INFO] Starting Security Camera Motion Detection Add-on..."
echo "[INFO] Checking environment..."

# Read configuration from Home Assistant
if command -v bashio &> /dev/null && bashio::supervisor.ping 2>/dev/null; then
    echo "[INFO] Running as Home Assistant add-on"
    echo "[INFO] Reading configuration from Supervisor API..."

    # Debug: show raw config
    echo "[DEBUG] Raw addon config:"
    bashio::addon.config || echo "[DEBUG] Could not read raw config"

    STREAM_URL=$(bashio::config 'stream_url') || STREAM_URL=""
    echo "[DEBUG] stream_url = '${STREAM_URL}'"

    MOTION_THRESHOLD=$(bashio::config 'motion_threshold') || MOTION_THRESHOLD=""
    MOTION_MIN_DURATION=$(bashio::config 'motion_min_duration') || MOTION_MIN_DURATION=""
    RECORDING_PRE_ROLL=$(bashio::config 'recording_pre_roll') || RECORDING_PRE_ROLL=""
    RECORDING_POST_ROLL=$(bashio::config 'recording_post_roll') || RECORDING_POST_ROLL=""
    RECORDINGS_PATH=$(bashio::config 'recordings_path') || RECORDINGS_PATH=""
    MAX_RECORDINGS=$(bashio::config 'max_recordings') || MAX_RECORDINGS=""
    LOG_LEVEL=$(bashio::config 'log_level') || LOG_LEVEL=""

    echo "[INFO] Config read complete"
else
    echo "[INFO] Running in local/standalone mode"
fi

# Apply defaults for any missing values
STREAM_URL="${STREAM_URL:-http://localhost:8080/stream.m3u8}"
MOTION_THRESHOLD="${MOTION_THRESHOLD:-5000}"
MOTION_MIN_DURATION="${MOTION_MIN_DURATION:-3}"
RECORDING_PRE_ROLL="${RECORDING_PRE_ROLL:-6}"
RECORDING_POST_ROLL="${RECORDING_POST_ROLL:-5}"
RECORDINGS_PATH="${RECORDINGS_PATH:-/share/security_recordings}"
MAX_RECORDINGS="${MAX_RECORDINGS:-50}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# Export for Python scripts
export STREAM_URL
export MOTION_THRESHOLD
export MOTION_MIN_DURATION
export RECORDING_PRE_ROLL
export RECORDING_POST_ROLL
export RECORDINGS_PATH
export MAX_RECORDINGS
export LOG_LEVEL
export STATE_FILE="${RECORDINGS_PATH}/../security_state.json"
export HTTP_PORT=8081

# Create directories
mkdir -p "$RECORDINGS_PATH"

echo "========================================"
echo "Security Camera Motion Detection"
echo "========================================"
echo "Stream URL: $STREAM_URL"
echo "Motion threshold: $MOTION_THRESHOLD"
echo "Min duration: ${MOTION_MIN_DURATION}s"
echo "Pre-roll: ${RECORDING_PRE_ROLL}s"
echo "Post-roll: ${RECORDING_POST_ROLL}s"
echo "Recordings path: $RECORDINGS_PATH"
echo "Max recordings: $MAX_RECORDINGS"
echo "Log level: $LOG_LEVEL"
echo "========================================"

# Activate virtual environment if it exists
if [ -d "/app/venv" ]; then
    source /app/venv/bin/activate
fi

# Start HTTP server in background
echo "Starting HTTP server on port $HTTP_PORT..."
python3 /app/http_server.py &
HTTP_PID=$!

# Give HTTP server time to start
sleep 2

# Cleanup function
cleanup() {
    echo "Shutting down..."
    kill $HTTP_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# Start main detection service
echo "Starting motion detection service..."
python3 -c "
import os
import sys
import time
import logging

# Configure logging
log_level = os.environ.get('LOG_LEVEL', 'info').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)

from motion_detector import MotionDetector, MotionEvent
from recording_manager import RecordingManager
from ha_integration import HAIntegration

# Get configuration from environment
stream_url = os.environ['STREAM_URL']
motion_threshold = int(os.environ['MOTION_THRESHOLD'])
min_duration = float(os.environ['MOTION_MIN_DURATION'])
pre_roll = int(os.environ['RECORDING_PRE_ROLL'])
post_roll = int(os.environ['RECORDING_POST_ROLL'])
recordings_path = os.environ['RECORDINGS_PATH']
max_recordings = int(os.environ['MAX_RECORDINGS'])
state_file = os.environ['STATE_FILE']

# Initialize components
ha = HAIntegration(state_file=state_file, update_interval=1.0)
recorder = RecordingManager(
    stream_url=stream_url,
    recordings_path=recordings_path,
    pre_roll=pre_roll,
    post_roll=post_roll,
    max_recordings=max_recordings
)

# Motion callbacks
def on_motion_start(event: MotionEvent):
    logging.info(f'Motion started! Area: {event.motion_area}')
    recorder.start_recording(motion_start_time=event.timestamp)
    ha.update_recording_state(
        is_recording=True,
        total_recordings=len(recorder.get_recordings())
    )

def on_motion_end(event: MotionEvent):
    logging.info('Motion ended, scheduling recording stop')
    recorder.schedule_stop()

def on_motion_frame(event: MotionEvent):
    # Extend recording while motion continues
    recorder.extend_recording()

detector = MotionDetector(
    stream_url=stream_url,
    motion_threshold=motion_threshold,
    min_duration=min_duration,
    check_interval=1.0,
    on_motion_start=on_motion_start,
    on_motion_end=on_motion_end,
    on_motion_frame=on_motion_frame
)

# Start services
ha.start()
detector.start()

logging.info('Motion detection service started')

# Main loop - update HA state
try:
    while True:
        stats = detector.get_stats()
        ha.update_motion_state(
            detected=detector.is_motion_active,
            state=stats['state'],
            frames_processed=stats['frames_processed']
        )

        rec_stats = recorder.get_stats()
        latest = recorder.get_latest_recording()
        ha.update_recording_state(
            is_recording=rec_stats['is_recording'],
            total_recordings=rec_stats['total_recordings'],
            latest_recording=latest.filename if latest else None,
            latest_recording_time=latest.start_time if latest else None,
            latest_thumbnail=latest.thumbnail if latest else None
        )

        time.sleep(1)

except KeyboardInterrupt:
    logging.info('Shutting down...')
    detector.stop()
    recorder.stop_recording_immediate()
    ha.stop()
"

# Wait for background processes
wait $HTTP_PID
