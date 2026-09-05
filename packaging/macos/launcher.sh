#!/bin/bash
# "Silk Code.app" - what Finder runs when the app is opened.
#
# The app carries its own Python (Contents/Resources/python) with Silk Code
# installed into it. Opening the app starts the GUI daemon (`silkcode gui`)
# in the background; the daemon opens the GUI in the default browser. Opening
# the app again while the daemon is running just brings the GUI back. Nothing
# stays in the Dock: the browser tab is the app, and the daemon keeps serving
# it until it is stopped or the Mac restarts.
#
#   log:   ~/Library/Logs/SilkCode/gui.log
#   port:  8377, or $SILKCODE_PORT
#   stop:  pkill -f "silkcode gui"

set -u

contents="$(cd "$(dirname "$0")/.." && pwd)"
python="$contents/Resources/python/bin/python3"
port="${SILKCODE_PORT:-8377}"
url="http://127.0.0.1:$port"
logdir="$HOME/Library/Logs/SilkCode"
log="$logdir/gui.log"

# Finder starts apps with a bare environment: no Homebrew on PATH (so no git,
# no ollama) and none of the API keys exported in the shell profile. Re-enter
# once through the login shell so the daemon sees what a terminal would.
# Only for shells whose -c / $0 / "$@" behaviour is known.
if [ -z "${SILKCODE_APP_SHELL:-}" ] && [ -x "${SHELL:-}" ]; then
  export SILKCODE_APP_SHELL=1
  case "$(basename "$SHELL")" in
    bash|zsh) exec "$SHELL" -l -i -c 'exec "$0" "$@"' "$0" "$@" </dev/null ;;
  esac
fi

export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; }

if listening; then
  open "$url"
  exit 0
fi

mkdir -p "$logdir"
echo "=== $(date) starting $contents" >>"$log"
nohup "$python" -m silkcode gui --port "$port" </dev/null >>"$log" 2>&1 &
daemon=$!
disown "$daemon"
# The Chromium the agent uses to look at pages is a separate download. Fetch
# it in the background (a no-op once it is there) without holding up the GUI.
nohup "$python" -m playwright install chromium </dev/null >>"$log" 2>&1 &
disown $!

# Wait for the daemon to come up, so a failure to start is reported rather than
# silently doing nothing. A slow first start is not a failure: the daemon opens
# the browser itself once it is listening.
for _ in $(seq 1 120); do
  listening && exit 0
  kill -0 "$daemon" 2>/dev/null || {
    osascript -e "display dialog \"Silk Code could not start. Details are in $log\" \
      with title \"Silk Code\" with icon stop buttons {\"OK\"} default button 1" \
      </dev/null >/dev/null 2>&1
    exit 1
  }
  sleep 0.25
done
exit 0
