#!/usr/bin/env python3
"""analyze.py <rundir> -> summary.json (+ one-line print). All times are device epoch (host times corrected by the
measured clock offset). Window = from the first Wi-Fi action to the end of the run."""
import json, os, re, sys

R = sys.argv[1]
m = json.load(open(os.path.join(R, "meta.json")))
off = m.get("clock_offset_dev_minus_host", 0.0)
build = m["build"]
ev = m.get("events", [])
acts = []
for e in ev:
    for k in ("off", "on"):
        if e.get(k):
            acts.append((float(e[k]), k))
acts.sort()
w0 = acts[0][0] if acts else 0


def lines(n):
    p = os.path.join(R, n)
    return open(p, errors="replace").read().splitlines() if os.path.exists(p) else []


# ---- sampler ----
samp = []
for l in lines("samp.txt"):
    p = l.split()
    if not p or not re.match(r"^\d+\.\d+$", p[0]):
        continue
    f = dict(w.split("=", 1) for w in p[1:] if "=" in w)
    samp.append((float(p[0]), f))
t_end = samp[-1][0] if samp else w0
defsw = []  # default network transitions (transport) inside the window
prev = None
for t, f in samp:
    d = f.get("def", "none")
    tr = d.split(":")[1] if d.count(":") == 2 else "none"
    if prev is not None and tr != prev and t >= w0 - 1:
        defsw.append((round(t - w0, 1), prev, tr))
    prev = tr
max_nvpn = max([int(f.get("nvpn", 0)) for t, f in samp] or [0])
vpn_none = sum(1 for t, f in samp if t >= w0 and f.get("vpn") == "none")
owners = sorted({f.get("vpn", "none").split(":owner")[-1].split(":")[0] for t, f in samp if f.get("vpn", "none") != "none"})
vpn_transports = sorted({f.get("vpn", "").split(":owner")[-1].split(":", 1)[1] for t, f in samp if ":owner" in f.get("vpn", "") and f.get("vpn", "").split(":owner")[-1].count(":")})
pids = sorted({f.get("pids", "") for t, f in samp})


# GCM
def gc(f):
    g = f.get("gcm", "")
    mm = re.search(r"connects=(\d+)", g)
    return (g.split(",")[0], int(mm.group(1)) if mm else None)


gcm_conn = [(t,) + gc(f) for t, f in samp]
c_start = next((c for t, s, c in reversed(gcm_conn) if t < w0 and c is not None), None)
c_end = next((c for t, s, c in reversed(gcm_conn) if c is not None), None)
gcm_reconnects = (c_end - c_start) if c_start is not None and c_end is not None else None
gcm_notconn_s = 0.0
for i in range(1, len(gcm_conn)):
    t, s, c = gcm_conn[i]
    if t >= w0 and not gcm_conn[i - 1][1].startswith("connected="):
        gcm_notconn_s += t - gcm_conn[i - 1][0]
# per action: time to next connects increment (before the next action)
per_act = []
for i, (ta, k) in enumerate(acts):
    nxt = acts[i + 1][0] if i + 1 < len(acts) else t_end + 1
    c0 = None
    for t, s, c in gcm_conn:
        if t <= ta and c is not None:
            c0 = c
    hit = None
    for t, s, c in gcm_conn:
        if ta < t < nxt and c is not None and c0 is not None and c > c0 and s.startswith("connected="):
            hit = round(t - ta, 1); break
    per_act.append({"act": k, "t": round(ta - w0, 1), "gcm_reconnect_s": hit})
# GCM still disconnected at end?
gcm_end_state = gcm_conn[-1][1] if gcm_conn else ""

# ---- logcat ----
lc = lines("logcat-all.txt")
# farm: samp.sh logs "pids=<pid of the app package>"; every pid seen during the run counts as the VPN owner
owner_pids = {f.get("pids", "").strip() for t, f in samp} - {""}
lost_clears = 0
replacing = 0
replaced_ms = []
clears = {"owner": 0, "other": 0}
reset_tcp = []
crash = []
clear_ts = []
keeps = 0
unders = []
freeing = 0
repl_t = []
fix3_bad = []
gt_ev = []  # (t, "conn"|"close") from the GmsCore event tags
uid_resets = []  # FIX5: TCP connections reset per uid reset
fix5_never = []  # FIX5: lines that must never appear
conn_kinds = {}  # FIX5: [Conn] change kinds inside the window
switch_resets = []  # FIX5 85df47d: TCP connections opened before a switch that were reset
for l in lc:
    mm = re.match(r"^\s*(\d+\.\d+)\s+(\d+)\s+(\d+)\s+\w\s+(.*)$", l)
    if not mm:
        continue
    t, pid, msg = float(mm.group(1)), mm.group(2), mm.group(4)
    if t < w0 - 0.5:
        continue
    if re.search(r" gtalk_connection: 0\s*$", l):  # 0 = connected; other values are failed attempts
        gt_ev.append((t, "conn"))
    elif " gtalk_conn_close:" in l:
        gt_ev.append((t, "close"))
    if pid in owner_pids and "Freeing the sessions of the previous VPN interface" in msg:
        freeing += 1
    if pid in owner_pids and re.search(r"Could not establish the placeholder|Packet engine did not stop|replacement failed|Could not set underlying", msg):
        fix3_bad.append(msg[:160])
    if pid in owner_pids and "Replacing VPN interface" in msg:
        replacing += 1
    mrep = re.search(r"VPN interface replaced in (\d+) ms", msg)
    if pid in owner_pids and mrep:
        replaced_ms.append(int(mrep.group(1))); repl_t.append(t)
    if pid in owner_pids and "was lost, clearing sessions" in msg:
        lost_clears += 1
    if pid in owner_pids and "keeping sessions" in msg:
        keeps += 1
    if pid in owner_pids and "ConnectionStateManager: Underlying network" in msg and "->" in msg and "clearing" not in msg:
        unders.append(round(t - w0, 2))
    # main: "Clearing all active sessions due to network change"; FIX5: native "Cleared all sessions on the event thread"
    if ("Athena" in msg and ("Clearing all active sessions due to network change" in msg or "Clearing sessions opened more than" in msg)) \
            or "Cleared all sessions on the event thread" in msg:
        if pid in owner_pids:
            clears["owner"] += 1; clear_ts.append(round(t - w0, 2))
        else:
            clears["other"] += 1
    mr = re.search(r"(?:Clearing (?:all active sessions|sessions opened more than \S+ ms ago)|Cleared all sessions on the event thread:), reset (\d+) TCP", msg)
    if mr and pid in owner_pids:
        reset_tcp.append(int(mr.group(1)))
    mu = re.search(r"Reset (\d+) TCP connections of uids blocked on the new network", msg)
    if mu and pid in owner_pids:
        uid_resets.append(int(mu.group(1)))
    if pid in owner_pids and re.search(r"stopSelf after clear|Failed to lock mutex for (?:clearing|resetting) sessions|wake-up error|"
                                         r"Cannot register the default network callback|Could not set the VPN's underlying network|ignoring VPN network", msg):
        fix5_never.append(l[:200])
    mk = re.search(r"\[Conn\] (?:on(?:Available|Lost) .*kind=(\w+)|Underlying default network .*?\((\w+)[:), ])", msg)
    if mk and pid in owner_pids and t >= w0 - 0.5:
        k = mk.group(1) or mk.group(2)
        conn_kinds[k] = conn_kinds.get(k, 0) + 1
    mo = re.search(r"Reset (\d+) TCP connections opened before the network change", msg)
    if mo and pid in owner_pids:
        switch_resets.append(int(mo.group(1)))
    if re.search(r"FATAL|Fatal signal|has died|Process com\.kin\.athena[^ ]* .*died|ANR in com\.kin|am_proc_died.*com\.kin|am_crash.*com\.kin|Unhandled action|No action specified", msg) and "com.kin" in msg + l:
        crash.append(l[:200])

# GCM from event tags: time from each action to the next gtalk_connection (before the next action),
# and not-connected time = close -> next connection, inside the window
ev_per_act = []
for i, (ta, k) in enumerate(acts):
    nxt = acts[i + 1][0] if i + 1 < len(acts) else t_end + 1
    hit = next((round(t - ta, 1) for t, e in gt_ev if e == "conn" and ta < t < nxt), None)
    ev_per_act.append(hit)
ev_down = 0.0
down_since = None
for t, e in sorted(gt_ev):
    if t < w0:
        down_since = t if e == "close" else None
        continue
    if e == "close" and down_since is None:
        down_since = t
    elif e == "conn" and down_since is not None:
        ev_down += t - max(down_since, w0); down_since = None
if down_since is not None:
    ev_down += t_end - max(down_since, w0)
# GCM reconnect measured from the moment Wi-Fi became the validated default again (sampler), not from "svc wifi enable":
# the hotspot association time (3-9 s) varies run to run and is not the VPN's doing
wd_per = []
for e in ev:
    if e.get("wifi_default"):
        tw = float(e["wifi_default"])
        nxt = min([a for a, k in acts if a > tw] or [t_end + 1])
        wd_per.append(next((round(t - tw, 1) for t, x in gt_ev if x == "conn" and tw - 3 < t < nxt), None))
# GCM: time from "VPN interface replaced" to the next successful gtalk_connection
repl_gcm = [next((round(t - tr, 2) for t, x in gt_ev if x == "conn" and t > tr - 0.5), None) for tr in repl_t]
nvpn_multi = sum(1 for t, f in samp if t >= w0 and int(f.get("nvpn", 0)) > 1)
nvpn_end = int(samp[-1][1].get("nvpn", 0)) if samp else None

# ---- MQTT ----
pubs = {}
for l in lines("pub.log"):
    p = l.split()
    if len(p) >= 3 and p[1] == "PUB":
        pubs[int(p[2])] = float(p[0]) + off
rx = {}
conns, closes = 0, []
for l in lines("mq.txt"):
    p = l.split()
    if len(p) < 2:
        continue
    t = float(p[0])
    if p[1] == "RXPUSH":
        s = int(p[2])
        if s not in rx:
            rx[s] = t
    elif p[1] == "CONN" and "TCPOK" in l and t >= w0:
        conns += 1
    elif p[1] == "CLOSE" and t >= w0:
        closes.append((round(t - w0, 1), re.search(r"why=(.*?) age=", l).group(1)))
mq_end = max(rx.values()) if rx else t_end
win = [s for s, t in pubs.items() if w0 <= t <= min(t_end, mq_end + 30) - 6]
lost = [s for s in win if s not in rx]
delays = sorted(rx[s] - pubs[s] for s in win if s in rx)
late = [d for d in delays if d > 10]
gaps = []
rs = sorted(rx[s] for s in rx if rx[s] >= w0 - 5)
for i in range(1, len(rs)):
    gaps.append(rs[i] - rs[i - 1])


# ---- probes ----
def probes(name, key):
    tot = fail = 0; ftimes = []
    for l in lines(name):
        p = l.split()
        if len(p) >= 4 and p[1] == key:
            t = float(p[0])
            if t < w0:
                continue
            tot += 1
            if p[3] == "FAIL":
                fail += 1; ftimes.append(round(t - w0, 1))
    return {"n": tot, "fail": fail, "fail_t": ftimes[:30]}


# ---- per switch (farm): one row per Wi-Fi off and per Wi-Fi on ----
ath_ev = []  # (t, kind) Athena lines from the owner pid
for l in lc:
    mm = re.match(r"^\s*(\d+\.\d+)\s+(\d+)\s+(\d+)\s+\w\s+(.*)$", l)
    if not mm or mm.group(2) not in owner_pids:
        continue
    t, msg = float(mm.group(1)), mm.group(4)
    if "Athena" in msg and "[Pkt]" not in msg and "Setup failed" not in msg:  # Setup failed = billing retries (no account)
        mc = re.search(r"\] \[(\w+)\]", msg)
        ath_ev.append((t, "comp:" + (mc.group(1) if mc else "plain")))
    mk = re.search(r"\[Conn\] (?:on(?:Available|Lost) .*kind=(\w+)|Underlying default network .*?\((\w+)[:), ])", msg)
    if mk:
        ath_ev.append((t, "kind:" + (mk.group(1) or mk.group(2))))
    if "TCP connections opened before the network change" in msg:
        ath_ev.append((t, "switchreset"))
    if re.search(r"Reset \d+ TCP connections of uids blocked on the new network", msg):
        ath_ev.append((t, "uidreset"))
    if "Clearing all active sessions due to network change" in msg or "Clearing sessions opened more than" in msg \
            or "Cleared all sessions on the event thread" in msg:
        ath_ev.append((t, "clear"))
    elif "Replacing VPN interface" in msg:
        ath_ev.append((t, "replacing"))
    elif re.search(r"VPN interface replaced in \d+ ms", msg):
        ath_ev.append((t, "replaced"))
    elif "was lost, clearing sessions" in msg:
        ath_ev.append((t, "lost-clear"))
    elif "keeping sessions" in msg:
        ath_ev.append((t, "keep"))
mqc = []
for l in lines("mq.txt"):
    p = l.split()
    if len(p) > 1 and p[1] == "CLOSE":
        mqc.append((float(p[0]), re.search(r"why=(.*?) age=", l).group(1)))
per_switch = []
for i, e in enumerate(ev):
    for k, dk, tr in (("off", "cell_default", "CELLULAR"), ("on", "wifi_default", "WIFI")):
        if not e.get(k):
            continue
        ta = float(e[k])
        nxt = min([a for a, kk in acts if a > ta] or [t_end + 1])
        row = {"flap": i + 1, "act": k, "t": round(ta - w0, 1), "to": tr,
               "default_after_s": round(float(e[dk]) - ta, 1) if e.get(dk) else None}
        for kind in ("clear", "replacing", "replaced", "lost-clear", "keep"):
            row["athena_" + kind] = sum(1 for t, x in ath_ev if x == kind and ta - 0.5 <= t < nxt)
        cc = {}
        for t, x in ath_ev:
            if x.startswith("comp:") and ta - 0.5 <= t < nxt:
                cc[x[5:]] = cc.get(x[5:], 0) + 1
        row["athena_lines"] = cc
        row["athena_uidreset"] = sum(1 for t, x in ath_ev if x == "uidreset" and ta - 0.5 <= t < nxt)
        row["athena_switchreset"] = sum(1 for t, x in ath_ev if x == "switchreset" and ta - 0.5 <= t < nxt)
        kk = {}
        for t, x in ath_ev:
            if x.startswith("kind:") and ta - 0.5 <= t < nxt:
                kk[x[5:]] = kk.get(x[5:], 0) + 1
        row["conn_kinds"] = kk
        row["gcm_conn_after_s"] = next((round(t - ta, 1) for t, x in gt_ev if x == "conn" and ta < t < nxt), None)
        row["gcm_close"] = sum(1 for t, x in gt_ev if x == "close" and ta - 0.5 <= t < nxt)
        row["mq_closes"] = [w for t, w in mqc if ta - 0.5 <= t < nxt]
        rx_in = sorted(v for v in rx.values() if ta <= v < nxt)
        row["mq_first_rx_after_s"] = round(rx_in[0] - ta, 1) if rx_in else None
        per_switch.append(row)

summ = {
    "tag": m["tag"], "build": build, "pattern": m["pattern"], "run": m["run"], "battery": [m.get("battery"), m.get("end_battery")],
    "window_s": round(t_end - w0, 1), "n_actions": len(acts),
    "default_switches": defsw, "n_default_switches": len(defsw),
    "athena_clears_owner": clears["owner"], "athena_clears_other_pkg": clears["other"], "clear_t": clear_ts,
    "rst_reset_tcp": reset_tcp, "fix_keep_sessions": keeps, "fix2_lost_clears": lost_clears, "fix3_replacing": replacing, "fix3_replaced_ms": replaced_ms, "vpn_netids": sorted({f.get("vpn", "none").split(":")[0] for t, f in samp if f.get("vpn", "none") != "none"}), "fix_underlying_changes": unders,
    "gcm_reconnects": gcm_reconnects, "gcm_per_action": per_act, "gcm_notconnected_s": round(gcm_notconn_s, 1), "gcm_end": gcm_end_state,
    "mq_pubs": len(win), "mq_lost": len(lost), "mq_late_gt10s": len(late), "mq_max_delay": round(max(delays), 1) if delays else None,
    "mq_reconnects": conns, "mq_closes": closes, "mq_max_gap": round(max(gaps), 1) if gaps else None,
    "probe_ip": probes("probe.txt", "PROBE"), "probe_dns": probes("dns.txt", "DNSPROBE"),
    "gcm_ev_per_action": ev_per_act, "gcm_ev_after_wifi_default": wd_per, "gcm_after_replacement": repl_gcm, "gcm_ev_down_s": round(ev_down, 1), "gcm_ev_conns": sum(1 for t, e in gt_ev if e == "conn" and t >= w0),
    "fix3_freeing": freeing, "fix3_bad": fix3_bad[:10], "nvpn_multi_samples": nvpn_multi, "nvpn_end": nvpn_end,
    "per_switch": per_switch, "uid_resets": uid_resets, "fix5_never": fix5_never[:20], "conn_kinds": conn_kinds, "switch_resets": switch_resets, "vpn_none_samples": vpn_none, "max_vpn_agents": max_nvpn, "vpn_owners": owners, "vpn_transports": vpn_transports, "pids": pids, "crash_lines": crash[:20],
}
json.dump(summ, open(os.path.join(R, "summary.json"), "w"), indent=1)
pa = ",".join(str(x["gcm_reconnect_s"]) for x in per_act)
print(f"{m['tag']}: sw={len(defsw)} clears={clears['owner']}(+{clears['other']} other) rst={sum(reset_tcp)} gcm_rc={gcm_reconnects} gcm_t=[{pa}] gcm_down={gcm_notconn_s:.0f}s "
      f"mq lost={len(lost)}/{len(win)} late={len(late)} maxgap={summ['mq_max_gap']} rc={conns} closes={[c[1] for c in closes]} "
      f"ip={summ['probe_ip']['fail']}/{summ['probe_ip']['n']} dns={summ['probe_dns']['fail']}/{summ['probe_dns']['n']} vpn_none={vpn_none} repl={replacing}/{freeing} ms={replaced_ms} lostclr={lost_clears} netids={summ['vpn_netids']} nvpn2={nvpn_multi} ev_gcm=[{",".join(str(x) for x in ev_per_act)}] ev_down={ev_down:.0f}s ev_after_wifidef={wd_per} gcm_after_repl={repl_gcm} bad={len(fix3_bad)} pids={pids} crash={len(crash)}")
