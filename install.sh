#!/usr/bin/env bash
# Install on a Debian or Ubuntu machine, as root, from this folder:
#   sudo bash install.sh
# Sets up the user 'baeder', the fetch script, the page in /var/www/baeder and a timer that
# fetches every 30 minutes. A web server is not included: see README (Caddy example).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo bash install.sh)"; exit 1; }
command -v python3 >/dev/null || { apt-get update -q && apt-get install -y -q python3; }
command -v systemctl >/dev/null || { echo "systemd is needed for the timer"; exit 1; }

id baeder >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin baeder
install -d -m 755 /opt/baeder
install -m 755 "$SRC/fetch_baeder.py" /opt/baeder/fetch_baeder.py
install -d -o baeder -g baeder -m 755 /var/www/baeder /var/www/baeder/fonts
install -o baeder -g baeder -m 644 "$SRC/www/index.html" /var/www/baeder/index.html
install -o baeder -g baeder -m 644 "$SRC"/www/fonts/* /var/www/baeder/fonts/
if [ -f "$SRC/www/config.json" ]; then
  install -o baeder -g baeder -m 644 "$SRC/www/config.json" /var/www/baeder/config.json
fi
install -m 644 "$SRC/systemd/baeder-fetch.service" "$SRC/systemd/baeder-fetch.timer" /etc/systemd/system/
systemctl daemon-reload

if ! systemctl start baeder-fetch.service; then
  journalctl -u baeder-fetch.service -n 15 --no-pager -o cat
  echo "first fetch failed - see above"; exit 1
fi
journalctl -u baeder-fetch.service -n 2 --no-pager -o cat
systemctl enable --now baeder-fetch.timer
echo
echo "Done. Serve /var/www/baeder with any web server (see README), e.g. Caddy."
