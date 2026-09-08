#!/usr/bin/with-contenv bashio
set -euo pipefail

export CUL868_FLASHER_OPTIONS=/data/options.json
export PYTHONPATH="/:${PYTHONPATH:-}"

cd /
exec /usr/bin/python3 -B -m app.main
