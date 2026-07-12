# Changelog

## 0.3.0

- Capture explicit HLS pre-roll before each motion event.
- Continue one recording until motion has been absent for the configured cooldown.
- Recover automatically when FFmpeg exits or stops writing data.
- Store temporary footage as crash-tolerant MPEG-TS segments.
- Detect and remove exact HLS overlap after reconnects.
- Preserve source segments until the final MP4 has been validated.
- Finalize recordings asynchronously so new motion is never blocked by thumbnails.
- Generate all preview images in a single FFmpeg pass.
- Write recordings metadata atomically.
- Show the build version in the UI and health endpoint.
