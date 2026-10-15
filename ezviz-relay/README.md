# EZVIZ relay (VM `seccam`)

Replaces the streaming part of the EZVIZ add-on (`ESJavadex/ezviz-ha-addon`) on the
seccam VM since 2026-10-15.

- `ezviz_stream.py`: copied unchanged from ezviz-ha-addon `2048d72` (protocol + login).
- `stream_relay.py`: one login, one ffmpeg, and the next VTDU session opened before
  EZVIZ cuts the current one (~30 s). Output switches when the new session has video.
  The camera timestamps are continuous across sessions; the new session restarts at
  the previous keyframe (~2 s back), so ffmpeg runs with `-copyts` and a `select`
  filter that keeps only frames moving time forward.
- `hls_server.py`: serves the HLS dir on :8080 with CORS (hls.js cards in HA).

The old add-on lost ~8 s every 30 s (≈20 % of the footage) reconnecting from scratch.

Session files: `/var/lib/seccam/ezviz/{feature_code,session.json}` (taken from the
add-on's /data so EZVIZ sees the same terminal and does not ask for a code).
Credentials: `/etc/seccam/ezviz.env` (EZVIZ_EMAIL, EZVIZ_PASSWORD, EZVIZ_SERIAL, EZVIZ_REGION).
