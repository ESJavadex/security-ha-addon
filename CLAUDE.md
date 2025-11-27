# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant add-on for motion detection and recording from HLS camera streams. Works with any camera that outputs HLS (m3u8), including EZVIZ cameras via ezviz-camera-addon.

## Deployment Environments

| Environment | Platform | Base Image | Config Source |
|------------|----------|------------|---------------|
| **Production** | Raspberry Pi 4 (Home Assistant OS) | Alpine (via `BUILD_FROM`) | bashio (config.yaml) |
| **Development** | macOS (local-test/) | Debian (python:3.11-slim) | Environment variables (.env) |

## Architecture

### Data Flow
```
HLS Stream → motion_detector (1 fps) → motion events → recording_manager
                                                              ↓
                                                       /share/security_recordings/*.mp4
                                                              ↓
                                                       ha_integration → security_state.json
                                                              ↓
                                                       http_server (port 8081)
```

### Core Components
- **motion_detector.py** - OpenCV MOG2 background subtraction, morphological noise filtering
- **recording_manager.py** - FFmpeg stream copy (no transcoding), pre/post roll, auto-cleanup
- **ha_integration.py** - JSON state file for HA sensor integration (no MQTT)
- **http_server.py** - REST API for recordings and state
- **run.sh** / **run_local.sh** - Orchestrators (bashio vs env vars)

### Key Design Decisions
1. **1 fps processing** - Sufficient for motion detection, minimal CPU
2. **Time threshold** - `motion_min_duration` filters brief walk-bys
3. **FFmpeg copy mode** - No transcoding = ~5-10% CPU on Pi 4
4. **JSON state file** - Simple HA integration without MQTT dependency

## Development Commands

```bash
# Local development (macOS)
cd local-test
cp .env.example .env          # Configure STREAM_URL
./start.sh                    # Builds image, copies Python files, starts container
docker-compose logs -f        # Monitor logs
curl localhost:8081/api/state # Check state
./stop.sh                     # Cleanup

# Rebuild after code changes
./start.sh  # Re-copies Python files from security-camera/
```

## Configuration

Environment variables (local) or config.yaml options (HA):

| Option | Default | Description |
|--------|---------|-------------|
| STREAM_URL | - | HLS stream URL (m3u8) |
| MOTION_THRESHOLD | 5000 | Pixel area to trigger (lower = more sensitive) |
| MOTION_MIN_DURATION | 3 | Seconds motion must persist |
| ROI_X_START | 33 | Detection zone left edge (0-100%) |
| ROI_X_END | 66 | Detection zone right edge (0-100%) |
| ROI_Y_START | 5 | Detection zone top edge (0-100%) |
| ROI_Y_END | 95 | Detection zone bottom edge (0-100%) |
| RECORDING_PRE_ROLL | 6 | Seconds before motion |
| RECORDING_POST_ROLL | 5 | Seconds after motion ends |
| MAX_RECORDINGS | 50 | Auto-cleanup threshold |
| LOG_LEVEL | info (HA) / debug (local) | debug/info/warning/error |

## Detection Zone (ROI)

The ROI (Region of Interest) defines where motion is detected. Recordings are always full-frame.

```
  0%                               100%
   ┌─────────────────────────────────┐  0%
   │ [timestamp - ignored]           │
   │─────────────────────────────────│  roi_y_start (5%)
   │    │                      │     │
   │    │   DETECTION ZONE     │     │
   │    │                      │     │
   │─────────────────────────────────│  roi_y_end (95%)
   │ [logo - ignored]                │
   └─────────────────────────────────┘  100%
        ↑                      ↑
   roi_x_start (33%)    roi_x_end (66%)
```

**Default:** Middle third horizontally (33-66%), full height minus edges (5-95%)

## Web UI

Access the recordings viewer at `http://<host>:8081/`

Features:
- **Grid view** of all recordings with thumbnails
- **Video player** - click any recording to play inline
- **Favorites** - mark important recordings with a star, filter to show only favorites
- **Delete recordings** - permanently remove with confirmation dialog
- **Date filter** - filter recordings by specific date
- **Sort order** - newest or oldest first
- **Live status** - shows current motion/recording state
- **Stats** - total recordings, favorites count, size
- **Keyboard navigation** - arrow keys to navigate, Escape to close

## API Endpoints

- `GET /` - Web UI for viewing recordings
- `GET /api/health` - Health check
- `GET /api/state` - Motion/recording state JSON
- `GET /api/recordings` - List recordings
- `DELETE /api/recordings/{filename}` - Delete a recording
- `POST /api/recordings/{filename}/favorite` - Toggle favorite status
- `GET /api/settings` - Current ROI and threshold settings
- `POST /api/settings` - Update settings (JSON body)
- `POST /api/settings/roi/{x1}/{x2}` - Quick set X axis ROI
- `POST /api/settings/roi_y/{y1}/{y2}` - Quick set Y axis ROI
- `POST /api/settings/threshold/{value}` - Quick set threshold
- `GET /<file>.mp4` - Download recording
- `GET /<file>.jpg` - Download thumbnail

### Live Settings API

Adjust detection zone without restart:

```bash
# Get current settings
curl localhost:8081/api/settings

# Set horizontal zone (X axis)
curl -X POST localhost:8081/api/settings/roi/33/66

# Set vertical zone (Y axis) - crop timestamp/logo
curl -X POST localhost:8081/api/settings/roi_y/10/90

# Set threshold
curl -X POST localhost:8081/api/settings/threshold/3000

# Set all at once
curl -X POST localhost:8081/api/settings \
  -H "Content-Type: application/json" \
  -d '{"roi_x_start":33, "roi_x_end":66, "roi_y_start":10, "roi_y_end":90}'
```

## Debugging

```bash
# Quick trigger testing
MOTION_THRESHOLD=1000 MOTION_MIN_DURATION=1 ./start.sh

# Check if stream is accessible from container
docker exec security-camera-test curl -I $STREAM_URL

# Monitor motion area values
docker-compose logs -f | grep "Motion area"

# Verify state updates
watch -n1 'curl -s localhost:8081/api/state | jq'
```

## Home Assistant Integration

The add-on writes to `/share/security_state.json`. Example HA configuration:

```yaml
command_line:
  - sensor:
      name: "Security Camera Motion"
      command: "cat /share/security_state.json | jq -r '.motion_detected'"
      scan_interval: 2
```
