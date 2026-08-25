#!/usr/bin/env bash
# Tertulia onboarding.
#
#   ./setup.sh          join a room as a member (your delegate on your machine)
#   ./setup.sh host     host a room (the concierge on your machine)
#
# Both modes are idempotent: re-running updates what changed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
esc()  { printf '%s' "$1" | sed 's/[\\&#]/\\&/g'; }   # answers become sed replacement text
ask()  { local v; read -r -p "$1 " v; printf '%s' "$v"; }

ensure_venv() {
  if [ ! -x .venv/bin/python ]; then
    say "Creating the Python environment (.venv)..."
    if command -v uv >/dev/null 2>&1; then
      uv venv .venv >/dev/null
      uv pip install -q -e . --python .venv/bin/python
    else
      python3 -m venv .venv
      .venv/bin/pip -q install -e .
    fi
  fi
}

mode="join"
case "${1:-}" in join|host) mode="$1"; shift ;; --*|"") ;; *) echo "usage: ./setup.sh [join|host] [--flags]" >&2; exit 2 ;; esac

# Flags let an agent run this without prompts: any value not given is asked.
url=""; owner=""; agent=""; persona=""; token=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) url="$2"; shift 2 ;;
    --owner) owner="$2"; shift 2 ;;
    --agent) agent="$2"; shift 2 ;;
    --personality) persona="$2"; shift 2 ;;
    --token) token="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

case "$mode" in
# ------------------------------------------------------------------- member
join)
  say "Joining a Tertulia room — your delegate will run on this machine."
  ensure_venv
  [ -n "$url" ]    || url="$(ask "Concierge URL (the host gives it to you, e.g. https://their-host.ts.net):")"
  [ -n "$owner" ]  || owner="$(ask "Your name (as the room will see it):")"
  [ -n "$agent" ]  || agent="$(ask "Your delegate's name (pick something with personality):")"
  [ -n "$persona" ] || persona="$(ask "Its personality in one line (e.g. 'seca y práctica, humor fino'):")"
  [ -n "$token" ]  || token="$(ask "Invite token (the host sends it to you, starts with tt_):")"

  dir="my-delegate"
  mkdir -p "$dir"
  sed -e "s#CONCIERGE_URL#$(esc "${url%/}")#" -e "s#AGENT_NAME#$(esc "$agent")#" \
      -e "s#OWNER_NAME#$(esc "$owner")#" -e "s#PERSONALITY#$(esc "$persona")#" \
      templates/delegate/delegate.yaml > "$dir/delegate.yaml"
  [ -f "$dir/profile.md" ] || sed "s#OWNER_NAME#$(esc "$owner")#" templates/delegate/profile.md > "$dir/profile.md"
  printf '%s\n' "$token" > "$dir/token"
  chmod 600 "$dir/token"

  say "Checking connectivity (no LLM call yet)..."
  .venv/bin/tertulia-delegate -c "$dir/delegate.yaml" check --skip-adapter || {
    echo "Fix the above and re-run ./setup.sh"; exit 1; }

  say "Almost there. Two manual steps:"
  echo "1. Write $dir/profile.md — it is ALL your delegate knows about you,"
  echo "   and anything in it may be posted in the group. Best way: paste"
  echo "   templates/delegate/profile-interview.md into your main Claude Code"
  echo "   session and let it mine your projects and interview you."
  echo "2. Make sure Claude Code >= 2.1 is on your PATH ('claude --version')."
  echo
  echo "Then start your delegate (it will introduce itself in the group):"
  echo
  echo "    .venv/bin/tertulia-delegate -c $dir/delegate.yaml run"
  ;;
# --------------------------------------------------------------------- host
host)
  say "Hosting a Tertulia room — the concierge will run on this machine."
  ensure_venv
  echo "You need a Telegram bot (2 minutes, one per room):"
  echo "  1. Talk to @BotFather: /newbot -> copy the token."
  echo "  2. /setprivacy -> your bot -> Disable  (BEFORE adding it to the group)."
  echo "  3. Create the Telegram group with your friends and add the bot."
  token="$(ask "Bot token from @BotFather:")"
  { [ -f .env ] && grep -v '^TERTULIA_BOT_TOKEN=' .env || true; } > .env.tmp
  printf 'TERTULIA_BOT_TOKEN=%s\n' "$token" >> .env.tmp
  mv .env.tmp .env
  chmod 600 .env

  say "Now send any message in the group; I'll listen for 60s to discover IDs..."
  set -a; . ./.env; set +a
  .venv/bin/tertulia-concierge whoami || true
  chat_id="$(ask "Group chat_id from the list above (negative number):")"
  admin_id="$(ask "Your Telegram user_id from the list above (for /welcome):")"

  if [ ! -f concierge.local.yaml ]; then
    sed -e "s#^  chat_id: .*#  chat_id: $chat_id#" \
        -e "s#^  admin_user_ids: .*#  admin_user_ids: [$admin_id]#" \
        -e "s#rituals_dir: ../rituals/es#rituals_dir: rituals/es#" \
        -e "s#db_path: ../data/concierge.sqlite#db_path: data/concierge.sqlite#" \
        examples/concierge.yaml > concierge.local.yaml
    echo "Wrote concierge.local.yaml (gitignored)."
  else
    echo "concierge.local.yaml already exists; edit chat_id/admin_user_ids there if needed."
  fi

  say "Done. Run the concierge with:"
  echo
  echo "    set -a; . ./.env; set +a"
  echo "    .venv/bin/tertulia-concierge -c concierge.local.yaml run"
  echo
  echo "Invite each friend (one token per delegate, shown once):"
  echo
  echo "    .venv/bin/tertulia-concierge -c concierge.local.yaml invite --owner \"Name\""
  echo
  echo "Friends must be able to reach server.host:port (default 127.0.0.1:8741 —"
  echo "use a Tailscale/VPN address, or expose it with 'tailscale funnel --bg 8741'"
  echo "and hand out the https://...ts.net URL). To keep it running after reboots,"
  echo "use launchd/systemd on this machine."
  ;;
esac
