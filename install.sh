#!/usr/bin/env bash
# Content Machine installer for Ubuntu 22.04 / 24.04 (run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/main/install.sh | sudo REPO=https://github.com/<user>/<repo>.git bash
#
# Private repo:  add  GITHUB_TOKEN=<token>  before bash.
# Skip Tailscale: add NO_TAILSCALE=1 (the dashboard then listens only on 127.0.0.1:8080).
set -euo pipefail

REPO="${REPO:-}"
BRANCH="${BRANCH:-main}"
APP=/opt/content-machine
DATA=/var/lib/content-machine
SVC_USER=machine

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mخطأ: %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "شغّل الأمر بـ sudo"
command -v apt-get >/dev/null || die "السكربت ده لـ Ubuntu بس"

say "1/7 تثبيت الأدوات (ffmpeg, python, ...)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg poppler-utils git curl ca-certificates tzdata ufw

say "2/7 مستخدم الخدمة"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DATA"
chown -R "$SVC_USER:$SVC_USER" "$DATA"
chmod 750 "$DATA"

say "3/7 تنزيل البرنامج"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only
else
  [ -n "$REPO" ] || die "حدد رابط الريبو: REPO=https://github.com/<user>/<repo>.git"
  URL="$REPO"
  if [ -n "${GITHUB_TOKEN:-}" ]; then URL="${REPO/https:\/\//https://x-access-token:${GITHUB_TOKEN}@}"; fi
  git clone --branch "$BRANCH" "$URL" "$APP"
  chmod 600 "$APP/.git/config"
fi
chmod +x "$APP/install.sh" "$APP/update.sh"

say "4/7 بيئة Python"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

say "5/7 الإعدادات والخدمات"
DASH_HOST=127.0.0.1
if [ -z "${NO_TAILSCALE:-}" ]; then DASH_HOST=0.0.0.0; fi
cat > /etc/content-machine.env <<EOF
MACHINE_DATA=$DATA
DASH_HOST=$DASH_HOST
DASH_PORT=8080
PYTHONUNBUFFERED=1
EOF
for unit in web worker; do
  cp "$APP/deploy/content-machine-$unit.service" /etc/systemd/system/
done
cat > /etc/sudoers.d/content-machine <<EOF
$SVC_USER ALL=(root) NOPASSWD: $APP/update.sh
EOF
chmod 440 /etc/sudoers.d/content-machine
systemctl daemon-reload
systemctl enable --now content-machine-worker content-machine-web

say "6/7 فحص الرندر"
sudo -u "$SVC_USER" env MACHINE_DATA="$DATA" "$APP/.venv/bin/python" -m machine selftest || die "فحص الرندر فشل. ابعت الرسالة دي لـClaude."

say "7/7 الأمان والوصول للوحة"
ufw allow OpenSSH >/dev/null
if [ -z "${NO_TAILSCALE:-}" ]; then
  if ! command -v tailscale >/dev/null; then curl -fsSL https://tailscale.com/install.sh | sh; fi
  ufw allow in on tailscale0 to any port 8080 proto tcp >/dev/null
  ufw --force enable >/dev/null
  echo
  echo "هيظهر لك رابط دلوقتي. افتحه وسجّل دخول بنفس حساب Tailscale اللي على موبايلك:"
  tailscale up --hostname content-machine || true
  TSIP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  echo
  echo "=========================================================="
  echo " ✅ التثبيت خلص"
  echo " افتح اللوحة من موبايلك أو جهازك (وTailscale شغال عليه):"
  echo "     http://content-machine:8080"
  [ -n "$TSIP" ] && echo "  أو  http://$TSIP:8080"
  echo "=========================================================="
else
  ufw --force enable >/dev/null
  echo
  echo "✅ التثبيت خلص. اللوحة شغالة على 127.0.0.1:8080 (من غير Tailscale)."
  echo "افتحها بنفق SSH:  ssh -L 8080:127.0.0.1:8080 root@IP  ثم افتح http://localhost:8080"
fi
