#!/usr/bin/env bashio

# Security Camera Motion Detection Add-on
# Main orchestrator script

set -e

if [ -s /app/VERSION ]; then
    export ADDON_VERSION="$(cat /app/VERSION)"
fi

echo "[INFO] Starting Security Camera Motion Detection Add-on..."
echo "[INFO] Checking environment..."

# Read configuration - try /data/options.json first (HA standard), then env vars
CONFIG_FILE="/data/options.json"

echo "[DEBUG] Checking for config file: ${CONFIG_FILE}"
if [ -f "$CONFIG_FILE" ]; then
    echo "[INFO] Found config file, reading options..."
    # Never print options.json: it may contain llm_api_key.

    # Read config using jq (more reliable than bashio)
    STREAM_URL=$(jq -r '.stream_url // empty' "$CONFIG_FILE")
    MOTION_THRESHOLD=$(jq -r '.motion_threshold // empty' "$CONFIG_FILE")
    MOTION_MIN_DURATION=$(jq -r '.motion_min_duration // empty' "$CONFIG_FILE")
    RECORDING_PRE_ROLL=$(jq -r '.recording_pre_roll // empty' "$CONFIG_FILE")
    RECORDING_POST_ROLL=$(jq -r '.recording_post_roll // empty' "$CONFIG_FILE")
    MOTION_COOLDOWN=$(jq -r '.motion_cooldown // empty' "$CONFIG_FILE")
    RECORDINGS_PATH=$(jq -r '.recordings_path // empty' "$CONFIG_FILE")
    MAX_RECORDINGS=$(jq -r '.max_recordings // empty' "$CONFIG_FILE")
    PURGE_RECORDINGS_ON_START=$(jq -r '.purge_recordings_on_start // empty' "$CONFIG_FILE")
    LOG_LEVEL=$(jq -r '.log_level // empty' "$CONFIG_FILE")
    ROI_X_START=$(jq -r '.roi_x_start // empty' "$CONFIG_FILE")
    ROI_X_END=$(jq -r '.roi_x_end // empty' "$CONFIG_FILE")
    ROI_Y_START=$(jq -r '.roi_y_start // empty' "$CONFIG_FILE")
    ROI_Y_END=$(jq -r '.roi_y_end // empty' "$CONFIG_FILE")
    LLM_ENABLED=$(jq -r '.llm_enabled // empty' "$CONFIG_FILE")
    LLM_AUTO_ANALYZE=$(jq -r '.llm_auto_analyze // empty' "$CONFIG_FILE")
    LLM_API_URL=$(jq -r '.llm_api_url // empty' "$CONFIG_FILE")
    LLM_API_KEY=$(jq -r '.llm_api_key // empty' "$CONFIG_FILE")
    LLM_MODEL=$(jq -r '.llm_model // empty' "$CONFIG_FILE")
    LLM_TIMEOUT=$(jq -r '.llm_timeout // empty' "$CONFIG_FILE")

    echo "[DEBUG] stream_url = '${STREAM_URL}'"
    echo "[INFO] Config read complete"
else
    echo "[INFO] No config file found, using environment variables"
fi

# Apply defaults for any missing values
STREAM_URL="${STREAM_URL:-http://localhost:8080/stream.m3u8}"
MOTION_THRESHOLD="${MOTION_THRESHOLD:-5000}"
MOTION_MIN_DURATION="${MOTION_MIN_DURATION:-5}"
RECORDING_PRE_ROLL="${RECORDING_PRE_ROLL:-10}"
RECORDING_POST_ROLL="${RECORDING_POST_ROLL:-0}"
MOTION_COOLDOWN="${MOTION_COOLDOWN:-10}"
RECORDINGS_PATH="${RECORDINGS_PATH:-/share/security_recordings}"
MAX_RECORDINGS="${MAX_RECORDINGS:-50}"
PURGE_RECORDINGS_ON_START="${PURGE_RECORDINGS_ON_START:-false}"
LOG_LEVEL="${LOG_LEVEL:-info}"
ROI_X_START="${ROI_X_START:-0}"
ROI_X_END="${ROI_X_END:-100}"
ROI_Y_START="${ROI_Y_START:-5}"
ROI_Y_END="${ROI_Y_END:-85}"
LLM_ENABLED="${LLM_ENABLED:-false}"
LLM_AUTO_ANALYZE="${LLM_AUTO_ANALYZE:-false}"
LLM_API_URL="${LLM_API_URL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-llava}"
LLM_TIMEOUT="${LLM_TIMEOUT:-60}"

# Export for Python scripts
export STREAM_URL
export MOTION_THRESHOLD
export MOTION_MIN_DURATION
export RECORDING_PRE_ROLL
export RECORDING_POST_ROLL
export MOTION_COOLDOWN
export RECORDINGS_PATH
export MAX_RECORDINGS
export LOG_LEVEL
export ROI_X_START
export ROI_X_END
export ROI_Y_START
export ROI_Y_END
export LLM_ENABLED
export LLM_AUTO_ANALYZE
export LLM_API_URL
export LLM_API_KEY
export LLM_MODEL
export LLM_TIMEOUT
export STATE_FILE="${RECORDINGS_PATH}/../security_state.json"
export SETTINGS_FILE="${RECORDINGS_PATH}/../security_settings.json"
export HTTP_PORT=8081

# Create directories
mkdir -p "$RECORDINGS_PATH"

if [ "$PURGE_RECORDINGS_ON_START" = "true" ]; then
    echo "[WARNING] Purging all existing security recordings on startup..."
    find "$RECORDINGS_PATH" -mindepth 1 -delete
    echo "[INFO] Recording purge completed"
fi

echo "========================================"
echo "Security Camera Motion Detection"
echo "========================================"
echo "Stream URL: $STREAM_URL"
echo "Motion threshold: $MOTION_THRESHOLD"
echo "Min duration: ${MOTION_MIN_DURATION}s"
echo "ROI: ${ROI_X_START}% - ${ROI_X_END}%"
echo "Pre-roll: ${RECORDING_PRE_ROLL}s"
echo "Post-roll: ${RECORDING_POST_ROLL}s"
echo "Motion cooldown: ${MOTION_COOLDOWN}s"
echo "Recordings path: $RECORDINGS_PATH"
echo "Max recordings: $MAX_RECORDINGS"
echo "Log level: $LOG_LEVEL"
echo "LLM enabled: $LLM_ENABLED"
if [ "$LLM_ENABLED" = "true" ]; then
    echo "LLM auto-analyze: $LLM_AUTO_ANALYZE"
    echo "LLM API URL: $LLM_API_URL"
    echo "LLM model: $LLM_MODEL"
fi
echo ""
echo "Live settings API: /api/settings"
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
python3 /app/service.py

# Wait for background processes
wait $HTTP_PID
