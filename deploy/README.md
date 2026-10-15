# Standalone deploy (minipc16, VM 150 `seccam`)

Since 2026-10-15 the detector runs in a dedicated VM instead of the Home Assistant
add-on (the Raspberry Pi was overloaded). The add-on is stopped with `boot: manual`
and kept as a rollback.

```
EZVIZ cloud (VTM :8554, VTDU :6000-6099)
        │  HEVC, sessions of ~30 s
        ▼
VM 150 seccam (192.168.1.148)
  seccam-stream   ezviz-relay/stream_relay.py: handover between sessions, one ffmpeg → H.264 HLS
  seccam-hls      ezviz-relay/hls_server.py :8080 (CORS)
  seccam-detector service.py (reads 127.0.0.1:8080, records with RECORDING_MODE=hls_follow)
  seccam-http     http_server.py :8081
        │
        └── SMB 3.1.1 (seal) ──► minipc32 WD /mnt/vault/seccam/recordings (container seccam-smb)

Home Assistant: camera.ezviz_camera → http://192.168.1.148:8080/stream.m3u8
                sensors curl http://192.168.1.148:8081/api/state
                recordings mounted read-only at /share/security_recordings
Tailnet: UI http://100.78.128.94:8081/ · HLS http://100.78.128.94:8080/stream.m3u8
         (systemd-socket-proxyd on minipc16)
```

Both HA add-ons (motion detection and EZVIZ stream) are stopped with `boot: manual`
and kept as rollback.

## VM

- Debian 13 cloud image, 2 vCPU (`cpu: host`), 1.5 GiB, 16 GB disk, `onboot`, `protection`.
- Proxmox firewall (only this VM has `firewall=1`; the host firewall is explicitly
  disabled in `host.fw`): in 22/8080/8081 from the host, 8080/8081 from HA; out only to
  the SMB share, DNS, NTP, EZVIZ (443, 8554, 6000-6099) and 80 to the internet (no LAN).
- EZVIZ relay: see `ezviz-relay/README.md`. Session files in `/var/lib/seccam/ezviz/`.
- SSH: `ssh seccam` (ProxyJump minipc16, key only, no root login).
- Services run as `seccamd` with systemd sandboxing (`deploy/systemd/`).
- Config: `/etc/seccam/seccam.env` (root:seccamd 640), see `seccam.env.example`.
- State/settings: `/var/lib/seccam/`.
- The SMB mount point `/share/security_recordings` is `chattr +i` while unmounted,
  so if the WD is missing recordings fail loudly instead of filling the VM disk.

## Deploy

```bash
deploy/install-vm.sh                    # code + units; does not touch the relay
RESTART_STREAM=1 deploy/install-vm.sh   # also restart the relay (short gap in the video)
ssh seccam journalctl -fu seccam-detector
```

## Tuning against real footage

```bash
python3 tools/replay.py --model yolox_s.onnx --snapshots /tmp/snaps motion_*.mp4
```

Live knobs (no restart) in `/var/lib/seccam/security_settings.json` or `POST /api/settings`:
`person_score`, `motion_fallback`, ROI and `motion_threshold`.
