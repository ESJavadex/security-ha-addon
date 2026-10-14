# Standalone deploy (minipc16, VM 150 `seccam`)

Since 2026-10-15 the detector runs in a dedicated VM instead of the Home Assistant
add-on (the Raspberry Pi was overloaded). The add-on is stopped with `boot: manual`
and kept as a rollback.

```
EZVIZ add-on (Pi, :8080 HLS) ──► VM 150 seccam (192.168.1.148)
                                   seccam-detector  (service.py)
                                   seccam-http      (http_server.py :8081)
                                        │
                     SMB 3.1.1 (seal) ──┘──► minipc32 WD /mnt/vault/seccam/recordings
                                              (container seccam-smb, deploy/smb/)
Home Assistant: sensors curl http://192.168.1.148:8081/api/state
                recordings mounted read-only at /share/security_recordings (Supervisor mount)
UI on the tailnet: http://100.78.128.94:8081/  (systemd-socket-proxyd on minipc16)
                   https://minipc16.tail1ae474.ts.net:8443/ (tailscale serve)
```

## VM

- Debian 13 cloud image, 2 vCPU (`cpu: host`), 1.5 GiB, 16 GB disk, `onboot`, `protection`.
- Proxmox firewall (only this VM has `firewall=1`; the host firewall is explicitly
  disabled in `host.fw`): in 22/8081 from the host, 8081 from HA; out only to the HLS
  stream, the SMB share, DNS, NTP and 80/443 to the internet (no LAN).
- SSH: `ssh seccam` (ProxyJump minipc16, key only, no root login).
- Services run as `seccamd` with systemd sandboxing (`deploy/systemd/`).
- Config: `/etc/seccam/seccam.env` (root:seccamd 640), see `seccam.env.example`.
- State/settings: `/var/lib/seccam/`.
- The SMB mount point `/share/security_recordings` is `chattr +i` while unmounted,
  so if the WD is missing recordings fail loudly instead of filling the VM disk.

## Deploy

```bash
deploy/install-vm.sh          # from the repo root on the Mac
ssh seccam journalctl -fu seccam-detector
```

## Tuning against real footage

```bash
python3 tools/replay.py --model yolox_s.onnx --snapshots /tmp/snaps motion_*.mp4
```

Live knobs (no restart) in `/var/lib/seccam/security_settings.json` or `POST /api/settings`:
`person_score`, `motion_fallback`, ROI and `motion_threshold`.
