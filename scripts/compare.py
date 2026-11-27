#!/usr/bin/env python3
"""compare.py <dir> [<dir> ...] -> markdown: per Android version, the builds side by side (last = new). Only status ok jobs."""
import glob, json, statistics as st, sys

AV = {"24": "7.0", "25": "7.1", "26": "8.0", "27": "8.1", "28": "9", "29": "10", "30": "11", "31": "12", "32": "12L", "33": "13",
      "34": "14", "35": "15", "36": "16", "37.0": "17", "37": "17"}


def load(d):
    g = {}
    for f in glob.glob(d + "/**/summary.json", recursive=True):
        r = json.load(open(f))
        if r.get("status") == "ok":
            g.setdefault(str(r["api"]), []).append(r)
    return g


def med(v):
    v = [x for x in v if x is not None]
    return f"{st.median(v):.1f}" if v else "-"


def stats(rr):
    ps = [p for r in rr for p in r.get("per_switch", [])]
    o = {}
    o["jobs"] = len(rr)
    o["build"] = rr[0]["build"]
    for act in ("off", "on"):
        x = [p for p in ps if p["act"] == act]
        o[f"clr_{act}"] = sum(p["athena_clear"] for p in x) / max(len(x), 1)
        o[f"swr_{act}"] = sum(p.get("athena_switchreset", 0) for p in x) / max(len(x), 1)
        dr = [p for p in x if p["gcm_close"] > 0]
        o[f"gcm_{act}"] = f"{med([p['gcm_conn_after_s'] for p in dr])} [{len(dr)}/{len(x)}, {sum(1 for p in dr if p['gcm_conn_after_s'] is None)}]"
    b = [r["gcm_ev_down_s"] for r in rr if r["pattern"] == "b"]
    o["gcm_b"] = f"{st.mean(b):.0f}" if b else "-"
    c = [r["gcm_ev_down_s"] for r in rr if r["pattern"] == "c"]
    o["gcm_c"] = f"{st.mean(c):.1f}" if c else "-"
    sw = sum(r["n_default_switches"] for r in rr)
    o["mq"] = f"{sum(r['mq_lost'] for r in rr)}/{sum(r['mq_pubs'] for r in rr)}"
    o["mqrc"] = f"{sum(r['mq_reconnects'] for r in rr) / max(sw, 1):.2f}"
    o["mqgap"] = max((r["mq_max_gap"] or 0) for r in rr)
    o["probe"] = f"{100 * sum(r['probe_ip']['fail'] for r in rr) / max(sum(r['probe_ip']['n'] for r in rr), 1):.1f}"
    o["vpn"] = f"{sum(r['vpn_none_samples'] for r in rr)}/{sum(r.get('nvpn_multi_samples', 0) for r in rr)}"
    o["crash"] = sum(len(r["crash_lines"]) for r in rr)
    o["never"] = sum(len(r.get("fix5_never", [])) for r in rr)
    o["uidres"] = sum(len(r.get("uid_resets", [])) for r in rr)
    k = {}
    for r in rr:
        for kk, v in r.get("conn_kinds", {}).items():
            k[kk] = k.get(kk, 0) + v
    o["kinds"] = " ".join(f"{a}:{b}" for a, b in sorted(k.items())) or "-"
    o["sw"] = sw
    return o


G = [load(d) for d in sys.argv[1:]]
B = G[-1]
cols = [("jobs", "jobs ok"), ("sw", "switches"), ("clr_off", "full clears per Wi-Fi off"), ("clr_on", "full clears per Wi-Fi return"), ("swr_on", "switch resets (connections opened before the change) per Wi-Fi return"),
        ("gcm_off", "GCM after Wi-Fi off: reconnect s median [dropped/switches, not back before next action]"),
        ("gcm_on", "GCM after Wi-Fi return: same"), ("gcm_b", "GCM down s over 5 flaps (pattern b, mean)"),
        ("gcm_c", "GCM down s, pattern c (mean)"), ("mq", "MQTT lost/pubs"), ("mqrc", "MQTT reconnects per switch"),
        ("mqgap", "MQTT max gap s"), ("probe", "IP probe fail %"), ("vpn", "VPN gone / 2-VPN samples"), ("crash", "crash lines"),
        ("never", "FIX5 must-never lines"), ("uidres", "uid resets"), ("kinds", "[Conn] change kinds")]
print("| Android (API) | build | " + " | ".join(c[1] for c in cols[:-1]) + " |")
print("|---" * (len(cols) - 1 + 2) + "|")
for api in sorted(set().union(*G), key=float):
    for g in G:
        if api not in g:
            continue
        o = stats(g[api])
        vals = [o[c[0]] if not isinstance(o[c[0]], float) else f"{o[c[0]]:.2f}" for c in cols[:-1]]
        print(f"| {AV.get(api, '?')} ({api}) | {o['build']} | " + " | ".join(str(v) for v in vals) + " |")
print()
print(f"| Android (API) | {stats(next(iter(B.values())))['build']} [Conn] change kinds inside the windows |")
print("|---|---|")
for api in sorted(B, key=float):
    print(f"| {AV.get(api, '?')} ({api}) | {stats(B[api])['kinds']} |")
