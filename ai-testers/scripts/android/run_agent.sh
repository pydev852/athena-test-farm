#!/usr/bin/env bash
# Adapted from mobile-agent-testers (MIT, see ../../LICENSE-mobile-agent-testers).
# Runs inside android-emulator-runner (working directory ai-testers/) once the emulator is up:
# applies the persona's phone settings, installs Athena and hands the emulator to a headless
# Claude Code agent. Everything the agent writes goes to results/ (encrypted before upload);
# only a status line (turns, cost, tokens) reaches the public log.
# No `set -x` here: the model API key is in this process's environment.
set -uo pipefail
adb wait-for-device
mkdir -p "results/$PERSONA"
# 1. Phone language (the emulator boots in en-US; google_apis images allow adb root)
if [ "$(adb shell getprop persist.sys.locale | tr -d '\r')" != "$LOCALE" ]; then
  adb root >/dev/null 2>&1; sleep 3; adb wait-for-device
  adb shell "setprop persist.sys.locale $LOCALE; setprop ctl.restart zygote"; sleep 10; adb wait-for-device
  for i in $(seq 1 60); do [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ] && break; sleep 3; done
  sleep 5
fi
# 2. App + persona settings
adb install -r -g ../apk/app.apk || adb install -r ../apk/app.apk || { echo "APK install failed"; exit 1; }
adb shell pm list packages | grep -qx "package:$APP_ID" || { echo "$APP_ID not installed"; exit 1; }
adb shell settings put system font_scale "${FONT_SCALE:-1.0}"
[ "${THEME:-light}" = dark ] && adb shell cmd uimode night yes
A11Y=""
if [ "${INCREASE_CONTRAST:-false}" = true ]; then
  adb shell settings put secure high_text_contrast_enabled 1 && A11Y="$A11Y, high contrast text on"
fi
if [ -n "${COLOR_FILTER:-}" ]; then
  export COLOR_FILTER
  A11Y="$A11Y, color filter $COLOR_FILTER (your screenshots show the colors as a person with $COLOR_FILTER sees them: report what can only be told apart by color, not the changed colors themselves)"
fi
adb emu geo fix "${GEO#*,}" "${GEO%,*}" >/dev/null 2>&1   # longitude first
adb shell svc wifi enable; adb shell svc data enable
# 3. Full logcat of the run (screen.py clears the buffers, a streaming reader keeps everything)
adb logcat -c
adb logcat -b main,system,crash -v threadtime > "results/$PERSONA/logcat.txt" 2>/dev/null &
LOGCAT=$!
adb shell monkey -p "$APP_ID" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 5
# 4. The agent: only the listed tools, a turn limit, a dollar limit and a hard time limit.
ALLOWED="Bash(adb:*),Bash(python3 scripts/android/screen.py:*),Bash(python3 scripts/android/type.py:*),Bash(sleep:*),Bash(date:*),Bash(ls:*),Read,Write,Edit"
DENIED="WebFetch,WebSearch,Task,Agent,NotebookEdit"
MAX_MIN=${MAX_MINUTES:-20}
NOW=$(date +%s); DEADLINE=$((NOW + MAX_MIN * 60 - 120))
SETTINGS="Device: ${DEVICE}, Android API ${API}, font scale x${FONT_SCALE:-1.0}, theme ${THEME:-light}${A11Y}, language $LOCALE."
PROMPT="Read brief.md and personas.json, then do the mission until the final report. PERSONA=$PERSONA PLATFORM=android APP_ID=$APP_ID. $SETTINGS Report anything that does not fit this screen (cut text, off-screen buttons, broken layout). You have $MAX_MIN minutes: it is now $NOW (date +%s) and your final report.md and findings.json must be written before $DEADLINE; the run is killed 2 minutes after that."
# Claude Code runs the agent's shell commands in its Linux sandbox (bubblewrap + socat, no network,
# writes only under ai-testers/). The sandbox has its own network namespace, so adb cannot reach the
# adb server on tcp:5037: for the agent's run the server listens on a unix socket in this folder,
# which the sandbox lets commands connect to.
SOCK="$PWD/.adb.sock"
adb kill-server >/dev/null 2>&1; rm -f "$SOCK"
adb -L "localfilesystem:$SOCK" start-server >/dev/null 2>&1
export ADB_SERVER_SOCKET="localfilesystem:$SOCK"
adb wait-for-device; adb devices
mkdir -p .tmp
# The per-socket filter needs a nested user namespace that GitHub's runners refuse (apply-seccomp:
# setgroups denied), so unix sockets are open in general; the workflow stops Docker first, so the
# adb socket is the only service of note left. Network and file writes stay fenced.
SANDBOX='{"sandbox":{"enabled":true,"failIfUnavailable":true,"network":{"allowAllUnixSockets":true}}}'
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
RUNNER_TEMP="$PWD/.tmp" timeout -k 20 "$((MAX_MIN * 60))" claude -p "$PROMPT" \
  --model "$MODEL" --max-turns "${MAX_TURNS:-150}" --max-budget-usd "${MAX_USD:-3}" \
  --output-format stream-json --verbose --strict-mcp-config \
  --settings "$SANDBOX" --allowedTools "$ALLOWED" --disallowedTools "$DENIED" \
  < /dev/null > results/agent-stream.jsonl 2> results/agent-errors.txt
CODE=$?
[ "$CODE" = 124 ] && echo "Agent stopped at the $MAX_MIN minute limit"
unset ANTHROPIC_API_KEY
adb kill-server >/dev/null 2>&1; unset ADB_SERVER_SOCKET; adb start-server >/dev/null 2>&1; adb wait-for-device
python3 scripts/usage.py results/agent-stream.jsonl results/agent-output.json "$PERSONA" || true
python3 scripts/agent_status.py results/agent-output.json results/agent-errors.txt "$PERSONA" || true
# 5. App state after the run (no agent text): crash buffer, VPN state, Athena process
{ echo "== crash buffer"; adb logcat -d -b crash
  echo "== athena pid"; adb shell pidof "$APP_ID"
  echo "== vpn"; adb shell dumpsys connectivity | grep -iE "VPN|NetworkAgentInfo" | head -40
} > "results/$PERSONA/after-run.txt" 2>&1
kill "$LOGCAT" 2>/dev/null; sleep 1
gzip -f "results/$PERSONA/logcat.txt" 2>/dev/null
adb shell svc wifi enable >/dev/null 2>&1
echo "Agent finished (exit $CODE), $(find "results/$PERSONA" -type f | wc -l) files"
