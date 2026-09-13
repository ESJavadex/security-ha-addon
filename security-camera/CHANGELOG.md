# Changelog

## 0.3.2

- Serve each HTTP request in its own thread. The single-threaded server handled
  one request at a time, so a single client that opened a connection and stopped
  reading blocked every other request, including the motion detector's own stream
  reads. On 2026-09-13 this left the camera 18 hours without recording while the
  add-on still reported `started`.
- Drop connections that stall for longer than 30 seconds so a dead client cannot
  hold a thread forever.

## 0.3.1

- Persist the Supervisor build version into the image so the UI always shows
  the exact installed release.

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
- Add an explicit startup option for authorized full recording resets.
