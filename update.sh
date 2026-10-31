#!/usr/bin/env bash
# Update to the latest version (called from the dashboard via sudo, or by hand: sudo /opt/content-machine/update.sh)
set -euo pipefail
APP=/opt/content-machine
# Run detached from the web service, so restarting it doesn't kill this script.
if [ -z "${CM_DETACHED:-}" ] && command -v systemd-run >/dev/null; then
  exec systemd-run --quiet --collect --unit="cm-update-$(date +%s)" --setenv=CM_DETACHED=1 "$0"
fi
log() { sudo -u machine env MACHINE_DATA=/var/lib/content-machine "$APP/.venv/bin/python" -c \
  "from machine import events; events.$1('''$2''', notify=False)" 2>/dev/null || true; }
if git -C "$APP" pull --ff-only && "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"; then
  cp "$APP"/deploy/content-machine-*.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl restart content-machine-worker content-machine-web
  sleep 3
  log info "التحديث خلص ✅ النسخة: $(git -C "$APP" log -1 --format=%h)"
else
  log error "التحديث فشل. شغّل sudo /opt/content-machine/update.sh وابعت الرسالة لـClaude."
  exit 1
fi
