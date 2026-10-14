#!/bin/sh
set -e
# Fail loudly if the vault is not behind /data (never write to the NVMe by accident).
[ -f /data/.seccam-vault-marker ] || { echo "FATAL: /data is not the vault (marker missing)"; exit 1; }
for u in seccam hass; do
  p="$(cat /run/secrets/$u.pass)"
  printf "%s\n%s\n" "$p" "$p" | smbpasswd -s -a "$u" >/dev/null
done
exec smbd --foreground --no-process-group --debug-stdout
