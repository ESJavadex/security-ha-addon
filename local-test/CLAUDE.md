# CLAUDE.md - Local Test Environment

This is the local development environment for the Security Camera Motion Detection add-on.

## Quick Start

```bash
# 1. Configure your stream URL
cp .env.example .env
# Edit .env with your camera's HLS URL

# 2. Start the container
./start.sh

# 3. Monitor
docker-compose logs -f

# 4. Stop
./stop.sh
```

## Default Stream URL

The default is configured for the EZVIZ camera add-on:
```
STREAM_URL=http://192.168.1.156:8080/stream.m3u8
```

Change this in `.env` to match your setup.

## Testing Motion Detection

1. Start the container
2. Watch logs: `docker-compose logs -f`
3. Walk in front of the camera
4. Check state: `curl http://localhost:8081/api/state`
5. Check recordings: `ls -la ./recordings/`

## Tuning Parameters

For a **doorbell** with neighbors walking by:
- `MOTION_THRESHOLD=5000` - Ignore small movements
- `MOTION_MIN_DURATION=3` - Only trigger if motion persists 3+ seconds

For **high sensitivity** testing:
- `MOTION_THRESHOLD=1000`
- `MOTION_MIN_DURATION=1`

## Output Locations

- Recordings: `./recordings/*.mp4`
- Thumbnails: `./recordings/*.jpg`
- State: `./state/security_state.json`

## API Testing

```bash
# Health check
curl http://localhost:8081/api/health

# Current state
curl http://localhost:8081/api/state

# List recordings
curl http://localhost:8081/api/recordings
```

## Troubleshooting

**"Could not open stream"**: Check STREAM_URL is accessible from Docker
```bash
docker exec security-camera-test curl -I $STREAM_URL
```

**No motion detected**: Lower MOTION_THRESHOLD or check stream is working
```bash
docker-compose logs -f | grep "Motion area"
```

**Container exits immediately**: Check logs for errors
```bash
docker-compose logs
```
