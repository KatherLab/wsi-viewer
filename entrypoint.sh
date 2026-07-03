#!/bin/sh
# WSI Browser entrypoint.
#
# aclcheckd must run as root to switch credentials to arbitrary users (numeric
# UID/GID from FreeIPA) for per-user filesystem access checks. The FastAPI app
# itself stays non-root (wsi, UID 1000) and talks to aclcheckd over a socket.
#
# Flow:
#   1. Create the socket dir, owned by the app user.
#   2. Start aclcheckd as root in the background.
#   3. Drop to the wsi user and exec uvicorn.
set -e

SOCKET_DIR="/run/wsi"
SOCKET_PATH="${SOCKET_DIR}/aclcheck.sock"

mkdir -p "$SOCKET_DIR"
chown wsi:wsi "$SOCKET_DIR"
rm -f "$SOCKET_PATH"

# Trust the FreeIPA CA (mounted read-only into the system CA drop-in dir) so
# StartTLS cert validation succeeds. update-ca-certificates merges it into the
# bundle Python's ssl reads via create_default_context(). No-op if absent.
IPA_CA="/usr/local/share/ca-certificates/ipa-ca.crt"
if [ -f "$IPA_CA" ]; then
  update-ca-certificates >/dev/null 2>&1 || true
fi

# Start the privileged access-check daemon (root).
python -m app.aclcheckd "$SOCKET_PATH" &

# Give it a moment to bind the socket.
for i in 1 2 3 4 5 6 7 8 9 10; do
  [ -S "$SOCKET_PATH" ] && break
  sleep 0.2
done

# Drop privileges and run the app.
exec gosu wsi python -m uvicorn app.main:app --host 0.0.0.0 --port 8010 "$@"
