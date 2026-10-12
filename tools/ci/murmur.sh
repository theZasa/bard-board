#!/bin/sh
# Mumble test server for CI: "start" (first run sets the SuperUser password) and "restart" (reconnect test).
# Usage: sh tools/ci/murmur.sh start|restart  (MURMUR_DIR, MURMUR_SUPERUSER_PASSWORD from the environment)
set -eu
dir="${MURMUR_DIR:-/tmp/murmur-ci}"
bin="$(command -v mumble-server || command -v murmurd || true)"
[ -n "$bin" ] || { echo "mumble-server/murmurd nicht installiert" >&2; exit 1; }
mkdir -p "$dir"
cat > "$dir/murmur.ini" <<INI
database=$dir/murmur.sqlite
logfile=$dir/murmur.log
pidfile=$dir/murmur.pid
port=64738
host=127.0.0.1
welcometext="Testserver"
users=50
bandwidth=72000
textmessagelength=5000
allowhtml=true
autobanAttempts=0
INI

stop() {
  if [ -f "$dir/murmur.pid" ]; then
    kill "$(cat "$dir/murmur.pid")" 2>/dev/null || true
    rm -f "$dir/murmur.pid"
    sleep 1
  fi
}

case "${1:-start}" in
  start)
    stop
    "$bin" -ini "$dir/murmur.ini" -supw "${MURMUR_SUPERUSER_PASSWORD:?}" >/dev/null 2>&1 || true
    "$bin" -ini "$dir/murmur.ini" >/dev/null 2>&1
    ;;
  restart)
    stop
    "$bin" -ini "$dir/murmur.ini" >/dev/null 2>&1
    ;;
  *) echo "start|restart" >&2; exit 2 ;;
esac
# Wait until the port accepts connections.
for _ in $(seq 1 50); do
  if python3 -c "import socket; socket.create_connection(('127.0.0.1', 64738), 1)" 2>/dev/null; then exit 0; fi
  sleep 0.2
done
echo "Mumble-Server startet nicht:" >&2
cat "$dir/murmur.log" >&2 || true
exit 1
