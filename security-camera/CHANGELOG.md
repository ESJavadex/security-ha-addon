# Changelog

## 0.4.0

Detector rewrite. Measured on the 200 recordings of 14-oct-2026: 167 had nobody
in them (hallway lights switching on/off, auto-exposure, the peephole door
moving). Replaying a sample of 54 of those videos through the new detector:
25/25 recordings with people raise a `person` event, and none of the empty
ones raises an event.

- Persistent ffmpeg reader at 4 fps with a stall watchdog. The old detector
  reopened the HLS stream every second and, in practice, analysed the same
  frame twice every 2 s with ~6 s of lag.
- Light and exposure filter: a brightness jump, a frame that changes almost
  everywhere at once, or a brightness ramp rebuilds the background and
  ignores motion for a few seconds.
- Camera-motion filter (sparse optical flow): when most tracked points move,
  the door holding the camera moved; the background is rebuilt.
- Optional person detection (YOLOX-s, Apache-2.0, `PERSON_MODEL`) at 416 px,
  run only when something is happening, after the light turns on, during an
  event, and every 5 s as a check. A confirmed person starts the event
  immediately. Recordings store `trigger` (`person`/`motion`) and
  `person_score`, and the boxed person frame becomes the thumbnail.
- Motion without a person only records when it lasts `MOTION_FALLBACK`
  seconds (default 8, 0 = people only), stays inside the zone and travels.
- State JSON adds `person_detected`, `event_trigger`, `last_person_score`,
  `scene`, `brightness`, `light_changes`, `camera_moves`.
- UI: person badge with score and "People only" / "Motion without person" /
  "Old detector" filters.
- The state file is written atomically: `/api/state` no longer answers 500
  while it is being rewritten. `motion_events_today` counts events again.
- Runs outside Home Assistant: `service.py` (shared with `run.sh`), systemd
  units and an install script in `deploy/`, `tools/replay.py` to replay
  recordings through the detector.

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
