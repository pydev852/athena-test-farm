# athena-test-farm

Network test suite for [Kin69/Athena](https://github.com/Kin69/Athena), the open-source Android firewall. This repository is
CI for that project: it runs Athena builds on Android emulators in GitHub Actions and measures how its VPN behaves when the
device switches between Wi-Fi and cellular data.

Everything runs on the GitHub-hosted runner. No accounts, secrets or external test services are used: the MQTT broker and the
HTTP probe target run on the runner itself.

## What a job does

1. Boots a `google_apis` x86_64 emulator (KVM) with Wi-Fi ("AndroidWifi") and simulated cellular data.
2. Installs the APK from a release in this repository and starts Athena's VPN through its UI (uiautomator, taps gated on the
   focused window).
3. Starts the workloads: an MQTT subscription to the local broker with a host-side publisher every 5 s, a fresh TCP + HTTP
   probe every 2 s, a DNS + HTTP probe every 2 s, and a sampler for the default network, the VPN network and Google Play
   services' push (GCM/mtalk) connection. The clients run as an app uid (via `su`) when the image allows it. Android 14 and
   later skip the shell uid when they kill sockets, so a shell client does not behave like an app.
4. Toggles Wi-Fi with `svc wifi disable/enable`:
   - `a`: Wi-Fi off, cellular default, 5 s, Wi-Fi on; 90 s tail
   - `b`: 5 flaps 10 s apart; 90 s tail
   - `c`: Wi-Fi off, stay on cellular for 180 s
5. Uploads logcat (all buffers), dumpsys snapshots, UI dumps and `summary.json`. A final job merges all summaries into one
   markdown table (`farm-table` artifact and the run summary page).

## Running

```sh
gh workflow run farm.yml -R pydev852/athena-test-farm \
  -f apk_tag=mainlog-d7a54c9,old-180 -f api_levels='[34, 36]' -f patterns='["a", "b"]' -f runs=2
```

Each combination of APK, API level, pattern and run is one emulator job; GitHub runs up to 20 at once on a free account.

To test another build, upload its APK as a release asset (one APK per release) and pass the tag as `apk_tag`.

## Files

- `.github/workflows/farm.yml`: matrix, emulator, local services, artifacts, merge
- `scripts/run.py`: one job on one emulator
- `scripts/samp.sh`, `scripts/dnsprobe.sh`: device-side sampler and DNS probe
- `scripts/mqflap.c`: device-side MQTT client and TCP probe (static binary built in the job)
- `scripts/pub.py`: host-side MQTT publisher
- `scripts/analyze.py`, `scripts/merge.py`: per-job summary and the merged table


## Boot start test (`boot.yml`)

Does Athena's VPN come back after `adb reboot`, and which path starts it? Per job: install, start the VPN through the UI,
take Athena off the power-save allowlist (unless the scenario keeps it), write "Start on Boot" into Athena's DataStore file
(run-as on debug builds, su otherwise), set or clear Always-on VPN in Settings.Secure, reboot (`svc power reboot`, a clean
framework shutdown like the power menu; `-f reboot=adb` for `adb reboot`), then for 120 s after
`sys.boot_completed` sample `dumpsys connectivity` for Athena's VPN, probe HTTP as an app uid, and collect logcat (all
buffers). Scenarios: A start-on-boot, B always-on, C both, D always-on + lockdown, E both + lockdown, F start-on-boot with
the battery exemption kept, U `adb install -r` of the same APK while the VPN runs (no reboot), K `kill -9` of Athena's process 4 times in a row (sticky
restart and its limit; no reboot).

```sh
gh workflow run boot.yml -R pydev852/athena-test-farm -f apk_tag=fix6-1ebf659,old-180 -f scenarios='["A", "D"]'
```

Files: `scripts/boot.py` (one job), `scripts/bootmerge.py` (API x scenario table).

## AI exploratory testers (`ai-testers.yml`)

Adapted from [mobile-agent-testers](https://github.com/pierridotite/mobile-agent-testers) (MIT, see
`ai-testers/LICENSE-mobile-agent-testers`). One emulator per persona (`ai-testers/personas.json`), each driven by a headless
Claude Code agent that follows `ai-testers/brief.md`. The agent step is the only one with the `ANTHROPIC_API_KEY` secret; the
agent may only use adb and the screenshot/typing helpers, with a turn, dollar and time limit. Screenshots, reports, logcat
and transcripts are encrypted with `ai-testers/results_public.pem` before upload; only a status line (turns, cost, tokens) is
public.

```sh
gh workflow run ai-testers.yml -R pydev852/athena-test-farm -f personas=emma,jozef -f max_minutes=20
bash ai-testers/scripts/collect.sh <run_id> <private_key.pem> <out_dir>
```
