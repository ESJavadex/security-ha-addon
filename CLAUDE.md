# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant add-on for motion detection and recording from HLS camera streams. Designed to work with the EZVIZ camera streaming add-on but compatible with any HLS source. Primary target platform is **Raspberry Pi 4**.

## Architecture

### Core Components

1. **motion_detector.py** - OpenCV-based motion detection using MOG2 background subtraction
2. **recording_manager.py** - FFmpeg-based clip recording with pre/post roll
3. **ha_integration.py** - Home Assistant sensor state exposure via JSON file
4. **http_server.py** - HTTP server for recordings and API endpoints
5. **run.sh** - Main orchestrator (bashio for HA, env vars for local)

### Data Flow

```
HLS Stream → motion_detector (1 frame/sec) → motion events
                                                    ↓
                                           recording_manager
                                                    ↓
                                           /share/security_recordings/*.mp4
                                                    ↓
                                           ha_integration → /share/security_state.json
                                                    ↓
                                           http_server (port 8081)
```

### Motion Detection Algorithm

- Uses OpenCV `createBackgroundSubtractorMOG2`
- Reads 1 frame per second from HLS stream
- Applies morphological operations to clean noise
- Calculates total motion area from contours
- Time threshold: motion must persist N seconds before triggering

### Recording Strategy

- On motion_start: Begin FFmpeg recording from stream
- During motion: Extend recording (cancel pending stop)
- On motion_end: Schedule stop after post_roll seconds
- FFmpeg copies codec (no transcoding) for efficiency

## Key Design Decisions

1. **HLS input** (not raw frames): Works with any camera, easy testing
2. **MOG2 background subtraction**: Low CPU, handles lighting changes
3. **Time threshold**: Filters brief walk-bys (doorbell use case)
4. **JSON state file**: Simple HA integration without MQTT dependency
5. **FFmpeg copy mode**: No transcoding = minimal CPU for recording

## Local Development

```bash
cd local-test
cp .env.example .env  # Edit with your stream URL
./start.sh            # Start Docker container
# API at http://localhost:8081/api/state
./stop.sh             # Cleanup
```

## File Structure

```
security-camera/           # HA add-on
├── config.yaml           # Add-on metadata
├── build.yaml            # Multi-arch builds
├── Dockerfile            # Alpine + OpenCV
├── run.sh                # Bashio orchestrator
├── motion_detector.py    # Motion detection
├── recording_manager.py  # Clip recording
├── ha_integration.py     # HA sensors
└── http_server.py        # HTTP API

local-test/               # Development
├── docker-compose.yml
├── Dockerfile.local      # Debian + OpenCV
├── .env.example
├── start.sh
└── stop.sh
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| stream_url | string | - | HLS stream URL |
| motion_threshold | int | 5000 | Pixel area trigger |
| motion_min_duration | int | 3 | Seconds to confirm |
| recording_pre_roll | int | 6 | Seconds before |
| recording_post_roll | int | 5 | Seconds after |
| recordings_path | string | /share/security_recordings | Storage path |
| max_recordings | int | 50 | Cleanup threshold |

## API Endpoints

- `GET /api/health` - Health check
- `GET /api/state` - Motion/recording state JSON
- `GET /api/recordings` - List recordings
- `GET /<file>` - Download recording/thumbnail

## Testing Tips

1. Lower `motion_threshold` to 1000 for testing sensitivity
2. Set `motion_min_duration` to 1 for quick triggers
3. Use `LOG_LEVEL=debug` to see frame processing
4. Check `/api/state` to monitor detection in real-time
