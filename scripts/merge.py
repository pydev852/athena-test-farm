#!/usr/bin/env python3
"""merge.py <dir> -> markdown on stdout: job status/timing, per-run results, per-switch rows, from <dir>/**/summary.json"""
import glob, json, os, sys
from collections import Counter

rows = []
for f in sorted(glob.glob(os.path.join(sys.argv[1], "**", "summary.json"), recursive=True)):
    try:
        r = json.load(open(f))
    except Exception as e:
        print(f"<!-- bad {f}: {e} -->"); continue
    r["_dir"] = os.path.relpath(os.path.dirname(f), sys.argv[1])
    rows.append(r)
rows.sort(key=lambda r: (str(r.get("build")), str(r.get("api")), str(r.get("pattern")), str(r.get("run"))))


def g(r, *ks, d="-"):
    for k in ks:
        r = r.get(k) if isinstance(r, dict) else None
        if r is None:
            return d
    return r


def gms(r):
    v = g(r, "device", "gms", d="-")
    v = " ".join(v) if isinstance(v, list) else str(v).splitlines()[0]
    return v.replace("versionName=", "") or "-"


print(f"## Jobs ({len(rows)})\n")
print("| job | build | API | pattern | run | status | client uid | GMS | GCM before VPN | GCM after VPN | emu start->booted s | booted->installed s | ->GCM s | ->VPN up s | ->done s |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    t = r.get("times", {})
    gc = next((t[k] for k in ("gcm_connected",) if k in t), "timeout" if "gcm_timeout" in t else "-")
    vp = t.get("vpn_up", "FAIL" if "vpn_fail" in t else "-")
    print(f"| {r['_dir']} | {r.get('build')} | {r.get('api')} | {r.get('pattern')} | {r.get('run')} | {r.get('status')} | {g(r, 'client', 'pkg')}/{g(r, 'client', 'uid')} | {gms(r)} | "
          f"{r.get('gcm_before_vpn', '-')} | {r.get('gcm_after_vpn', '-')} | {t.get('emulator_start_to_booted_s', '-')} | {t.get('installed', '-')} | {gc} | {vp} | {t.get('done', '-')} |")

ok = [r for r in rows if r.get("status") == "ok"]
print(f"\n## Results ({len(ok)} ok runs)\n")
print("| build | API | pattern | run | def. switches | Athena clears | replacing/replaced (ms) | GCM reconnects (dumpsys) | GCM ev conns | GCM down s (ev) | MQTT lost/pubs | MQTT max gap s | MQTT reconnects (close reasons) | IP probe fail | DNS probe fail | VPN gone samples | crash lines |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
short = {"recv-err:Connection reset by peer": "RST", "recv-err:Software caused connection abort": "abort", "ping-noresp-10s": "silent"}
for r in ok:
    cl = ", ".join(f"{k} x{v}" for k, v in Counter(short.get(c[1], c[1]) for c in r.get("mq_closes", [])).items()) or "-"
    print(f"| {r.get('build')} | {r.get('api')} | {r.get('pattern')} | {r.get('run')} | {r.get('n_default_switches')} | {r.get('athena_clears_owner')} | "
          f"{r.get('fix3_replacing')}/{len(r.get('fix3_replaced_ms', []))} {r.get('fix3_replaced_ms', [])} | {r.get('gcm_reconnects')} | {r.get('gcm_ev_conns')} | {r.get('gcm_ev_down_s')} | "
          f"{r.get('mq_lost')}/{r.get('mq_pubs')} | {r.get('mq_max_gap')} | {r.get('mq_reconnects')} ({cl}) | {g(r, 'probe_ip', 'fail')}/{g(r, 'probe_ip', 'n')} | "
          f"{g(r, 'probe_dns', 'fail')}/{g(r, 'probe_dns', 'n')} | {r.get('vpn_none_samples')} | {len(r.get('crash_lines', []))} |")

print("\n## Per switch\n")
print("| build | API | pattern | run | flap | action | t s | -> default | default after s | Athena clear | replacing | replaced | lost-clear | Athena lines by component (no [Pkt]) | GCM close | GCM conn after s | MQTT closes | MQTT first rx after s |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in ok:
    for p in r.get("per_switch", []):
        print(f"| {r.get('build')} | {r.get('api')} | {r.get('pattern')} | {r.get('run')} | {p['flap']} | wifi {p['act']} | {p['t']} | {p['to']} | {p['default_after_s']} | "
              f"{p['athena_clear']} | {p['athena_replacing']} | {p['athena_replaced']} | {p['athena_lost-clear']} | {' '.join(f'{k}:{v}' for k, v in sorted(p.get('athena_lines', {}).items())) or '-'} | {p['gcm_close']} | {p['gcm_conn_after_s']} | "
              f"{', '.join(f'{short.get(k, k)} x{v}' for k, v in Counter(p['mq_closes']).items()) or '-'} | {p['mq_first_rx_after_s']} |")

# ---- by build x Android version (all patterns) ----
import statistics as st
AV = {"24": "7.0", "25": "7.1", "26": "8.0", "27": "8.1", "28": "9", "29": "10", "30": "11", "31": "12", "32": "12L", "33": "13", "34": "14", "35": "15", "36": "16", "37": "17", "37.0": "17"}


def med(v):
    v = [x for x in v if x is not None]
    return f"{st.median(v):.1f}" if v else "-"


print("\n## By build and Android version (all patterns)\n")
print("| build | API (Android) | jobs ok/all | GmsCore GCM connected without account | switches | Athena clears per switch | "
      "GCM after Wi-Fi off: reconnect s median [dropped/switches, not back before next action] | GCM after Wi-Fi on: same | GCM down s per run (mean) | "
      "MQTT lost/pubs | MQTT reconnects per switch | MQTT max gap s (max) | IP/DNS probe fail % | VPN gone samples | multi-VPN samples | crash lines |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
grp = {}
for r in rows:
    grp.setdefault((str(r.get("build")), str(r.get("api"))), []).append(r)
for (b, a), rr in sorted(grp.items(), key=lambda kv: (kv[0][0], float(kv[0][1]) if kv[0][1].replace(".", "", 1).isdigit() else 0)):
    okr = [r for r in rr if r.get("status") == "ok"]
    gc = sum(1 for r in rr if str(r.get("gcm_before_vpn", "")).startswith("connected="))
    sw = sum(r.get("n_default_switches", 0) for r in okr)
    cl = sum(r.get("athena_clears_owner", 0) for r in okr)
    ps = [p for r in okr for p in r.get("per_switch", [])]
    def gcmcol(act):
        x = [p for p in ps if p["act"] == act]
        dr = [p for p in x if p["gcm_close"] > 0]
        return f"{med([p['gcm_conn_after_s'] for p in dr])} [{len(dr)}/{len(x)}, {sum(1 for p in dr if p['gcm_conn_after_s'] is None)}]"
    down = [r.get("gcm_ev_down_s") for r in okr if r.get("gcm_ev_down_s") is not None]
    ml = sum(r.get("mq_lost", 0) for r in okr); mp = sum(r.get("mq_pubs", 0) for r in okr)
    mr = sum(r.get("mq_reconnects", 0) for r in okr)
    mg = [r.get("mq_max_gap") for r in okr if r.get("mq_max_gap") is not None]
    ipn = sum(r["probe_ip"]["n"] for r in okr); ipf = sum(r["probe_ip"]["fail"] for r in okr)
    dn = sum(r["probe_dns"]["n"] for r in okr); dfl = sum(r["probe_dns"]["fail"] for r in okr)
    print(f"| {b} | {a} ({AV.get(a, '?')}) | {len(okr)}/{len(rr)} | {gc}/{len(rr)} | {sw} | {cl / sw if sw else 0:.1f} | "
          f"{gcmcol('off')} | {gcmcol('on')} | "
          f"{st.mean(down) if down else 0:.0f} | {ml}/{mp} | {mr / sw if sw else 0:.1f} | {max(mg) if mg else '-'} | "
          f"{100 * ipf / max(ipn, 1):.1f}/{100 * dfl / max(dn, 1):.1f} | {sum(r.get('vpn_none_samples', 0) for r in okr)} | "
          f"{sum(r.get('nvpn_multi_samples', 0) for r in okr)} | {sum(len(r.get('crash_lines', [])) for r in okr)} |")
