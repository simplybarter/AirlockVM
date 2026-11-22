#!/usr/bin/env bash
# Backward-compatibility wrapper for connect_airlock.sh
echo "[*] Notice: DevBox has been rebranded to AirlockVM. Redirecting to ./connect_airlock.sh..." >&2
exec "$(dirname "$0")/connect_airlock.sh" "$@"
