#!/usr/bin/env bash
# Run the whole v0 on this machine: the concierge + the two example delegates
# (Brisa and Cobre). Needs a real Telegram bot and group:
#
#   export TERTULIA_BOT_TOKEN=123456:ABC...   # from @BotFather
#   export TERTULIA_CHAT_ID=-100...           # from `tertulia-concierge whoami`
#   scripts/dev-local.sh
#
# Logs go to data/logs/*.log and are tailed here. Ctrl-C stops everything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
CFG="${CONCIERGE_CONFIG:-$ROOT/examples/concierge.yaml}"
DELEGATES="${DELEGATES:-brisa cobre}"

: "${TERTULIA_BOT_TOKEN:?set TERTULIA_BOT_TOKEN (token from @BotFather)}"
[ -x "$PY" ] || { echo "python not found at $PY — run: uv venv .venv && uv pip install -e '.[dev]'" >&2; exit 1; }

mkdir -p data/logs

# Invite each example delegate once; the token lands in its (gitignored) token file.
# (If you wipe data/, delete these token files too — tokens live in the concierge DB.)
for d in $DELEGATES; do
  dir="examples/delegates/$d"
  if [ ! -s "$dir/token" ]; then
    owner="$(sed -n 's/^owner_name:[[:space:]]*//p' "$dir/delegate.yaml" | tr -d '"'"'"'')"
    echo "inviting $d (owner: $owner)"
    "$PY" -m tertulia.concierge -c "$CFG" invite --owner "$owner" --quiet > "$dir/token"
  fi
done

pids=()
cleanup() { echo; echo "stopping..."; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$PY" -m tertulia.concierge -c "$CFG" run >> data/logs/concierge.log 2>&1 &
pids+=($!)
sleep 2
for d in $DELEGATES; do
  "$PY" -m tertulia.delegate -c "examples/delegates/$d/delegate.yaml" run >> "data/logs/$d.log" 2>&1 &
  pids+=($!)
done

echo "running: concierge + delegates ($DELEGATES). Logs in data/logs/. Ctrl-C to stop."
tail -n +1 -F data/logs/concierge.log $(for d in $DELEGATES; do echo "data/logs/$d.log"; done)
