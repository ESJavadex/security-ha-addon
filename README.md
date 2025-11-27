# Security Camera Motion Detection Add-on

Home Assistant add-on for motion detection and recording from HLS camera streams. Works with any camera that outputs an HLS stream (m3u8), including EZVIZ cameras via the [ezviz-camera-addon](https://github.com/ESJavadex/ezviz-camera-addon).

## Features

- **Motion Detection**: OpenCV-based background subtraction (MOG2)
- **Smart Filtering**: Time threshold to ignore brief motion (walk-bys)
- **Automatic Recording**: Records clips when motion is detected
- **Pre/Post Roll**: Captures moments before and after motion
- **Home Assistant Integration**: Exposes sensors for automations
- **HTTP API**: Access recordings and state via REST endpoints
- **Low Resource Usage**: Optimized for Raspberry Pi 4

## Installation

### As Home Assistant Add-on

1. Add this repository to Home Assistant:
   - Go to **Settings** → **Add-ons** → **Add-on Store**
   - Click **⋮** → **Repositories**
   - Add: `https://github.com/ESJavadex/security-ha-addon`

2. Install "Security Camera Motion Detection"

3. Configure the add-on with your stream URL

### For Local Development

See [local-test/README.md](local-test/README.md)

## Configuration

All options are configurable via the Home Assistant add-on Configuration tab.

| Option | Default | Description |
|--------|---------|-------------|
| `stream_url` | - | HLS stream URL (m3u8) |
| `motion_threshold` | 5000 | Pixel area to trigger motion (lower = more sensitive) |
| `motion_min_duration` | 3 | Seconds motion must persist before triggering |
| `roi_x_start` | 33 | Detection zone left edge (0-100%) |
| `roi_x_end` | 66 | Detection zone right edge (0-100%) |
| `roi_y_start` | 5 | Detection zone top edge (0-100%) |
| `roi_y_end` | 95 | Detection zone bottom edge (0-100%) |
| `recording_pre_roll` | 6 | Seconds to capture before motion |
| `recording_post_roll` | 5 | Seconds to capture after motion ends |
| `recordings_path` | /share/security_recordings | Where to save recordings |
| `max_recordings` | 50 | Maximum recordings to keep |
| `log_level` | info | Logging level (debug/info/warning/error) |

## Detection Zone (ROI)

The ROI (Region of Interest) limits motion detection to a specific area of the frame. This helps ignore:
- **Timestamp overlays** (constantly changing time)
- **Camera logos/watermarks**
- **Edge noise** from lighting changes

**Recordings are always full-frame** - ROI only affects detection.

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

### Live Adjustment API

Adjust detection zone without restarting the add-on:

```bash
# Get current settings
curl http://<HA_IP>:8081/api/settings

# Set horizontal zone (X axis)
curl -X POST http://<HA_IP>:8081/api/settings/roi/20/80

# Set vertical zone (Y axis) - crop timestamp/logo
curl -X POST http://<HA_IP>:8081/api/settings/roi_y/10/90

# Set motion threshold
curl -X POST http://<HA_IP>:8081/api/settings/threshold/3000

# Set all at once
curl -X POST http://<HA_IP>:8081/api/settings \
  -H "Content-Type: application/json" \
  -d '{"roi_x_start":33, "roi_x_end":66, "roi_y_start":10, "roi_y_end":90}'
```

## Home Assistant Integration

The add-on exposes state via a JSON file that can be read by Home Assistant sensors.

### Example Configuration

```yaml
# configuration.yaml

command_line:
  - sensor:
      name: "Security Camera Motion"
      command: "cat /share/security_state.json | jq -r '.motion_detected'"
      scan_interval: 2

template:
  - binary_sensor:
      - name: "Doorbell Motion"
        state: "{{ states('sensor.security_camera_motion') == 'true' }}"
        device_class: motion

automation:
  - alias: "Motion Alert"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_motion
        to: "on"
    action:
      - service: notify.mobile_app
        data:
          title: "Motion Detected"
          message: "Someone at the door"
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/state` | GET | Current motion/recording state |
| `/api/recordings` | GET | List all recordings |
| `/api/settings` | GET | Current ROI and threshold settings |
| `/api/settings` | POST | Update settings (JSON body) |
| `/api/settings/roi/{x1}/{x2}` | POST | Set X axis detection zone |
| `/api/settings/roi_y/{y1}/{y2}` | POST | Set Y axis detection zone |
| `/api/settings/threshold/{value}` | POST | Set motion threshold |
| `/<filename>.mp4` | GET | Download recording |
| `/<filename>.jpg` | GET | Download thumbnail |

## Architecture

```
HLS Stream (m3u8)
       ↓
┌──────────────────────────────────────┐
│ motion_detector.py                    │
│ - OpenCV MOG2 background subtraction │
│ - Time threshold filtering            │
└──────────────────────────────────────┘
       ↓ motion events
┌──────────────────────────────────────┐
│ recording_manager.py                  │
│ - FFmpeg clip capture                 │
│ - Pre/post roll handling             │
│ - Automatic cleanup                   │
└──────────────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│ ha_integration.py                     │
│ - JSON state file                     │
│ - HA sensor exposure                  │
└──────────────────────────────────────┘
```

## Performance

On Raspberry Pi 4:
- CPU: ~5-10% during motion detection
- Memory: ~100MB
- Frame processing: 1 frame/second

## License

MIT
