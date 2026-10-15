#!/usr/bin/env bash
# Deploy the seccam code to the VM (VM 150 on minipc16). Run from the repo root on the Mac:
#   deploy/install-vm.sh [ssh-host]   (default host alias: seccam)
set -euo pipefail
HOST="${1:-seccam}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ADDON_VERSION="$(sed -n 's/^version: "\(.*\)"/\1/p' "$ROOT/security-camera/config.yaml")"
VERSION="${ADDON_VERSION}-vm.$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet HEAD -- security-camera || echo -dirty)"

MODEL_URL="https://github.com/opencv/opencv_zoo/raw/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
MODEL_SHA256="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"

# Person model (YOLOX-s, Apache-2.0), downloaded once and verified
ssh "$HOST" "set -e
  if ! echo '$MODEL_SHA256  /opt/seccam/models/yolox_s.onnx' | sha256sum -c --status 2>/dev/null; then
    sudo install -d -m 755 /opt/seccam/models
    sudo curl -fsSL -o /opt/seccam/models/yolox_s.onnx.tmp '$MODEL_URL'
    echo '$MODEL_SHA256  /opt/seccam/models/yolox_s.onnx.tmp' | sha256sum -c --status
    sudo mv /opt/seccam/models/yolox_s.onnx.tmp /opt/seccam/models/yolox_s.onnx
    sudo chmod 644 /opt/seccam/models/yolox_s.onnx
  fi"

rsync -a --delete --exclude __pycache__ --rsync-path="sudo rsync" \
  "$ROOT/security-camera/" "$HOST:/opt/seccam/app/"
rsync -a --delete --exclude __pycache__ --exclude README.md --rsync-path="sudo rsync" \
  "$ROOT/ezviz-relay/" "$HOST:/opt/seccam/ezviz/"
rsync -a --rsync-path="sudo rsync" "$ROOT/deploy/systemd/" "$HOST:/etc/systemd/system/"
ssh "$HOST" "set -e
  printf '%s' '$VERSION' | sudo tee /opt/seccam/app/VERSION >/dev/null
  sudo chown -R root:root /opt/seccam/app /opt/seccam/ezviz /etc/systemd/system/seccam-*.service && sudo chmod -R u=rwX,go=rX /opt/seccam/app /opt/seccam/ezviz && sudo chmod 644 /etc/systemd/system/seccam-*.service
  if ! sudo test -f /etc/seccam/seccam.env; then echo 'missing /etc/seccam/seccam.env (see deploy/seccam.env.example)'; exit 1; fi
  sudo systemctl daemon-reload
  sudo systemctl enable seccam-hls seccam-stream seccam-detector seccam-http >/dev/null
  # The relay only restarts when its code changed: a restart is a short gap in the video
  sudo systemctl restart seccam-hls seccam-http seccam-detector
  ${RESTART_STREAM:+sudo systemctl restart seccam-stream}
  sleep 3; systemctl is-active seccam-hls seccam-stream seccam-http seccam-detector"
echo "Deployed $VERSION to $HOST"
