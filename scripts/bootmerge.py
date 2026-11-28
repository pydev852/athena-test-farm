#!/usr/bin/env python3
"""bootmerge.py <dir> -> markdown: boot start results (API x scenario per build) from <dir>/**/summary.json written by boot.py"""
import glob, json, os, sys

AV = {"26": "8.0", "27": "8.1", "28": "9", "29": "10", "30": "11", "31": "12", "32": "12L", "33": "13", "34": "14", "35": "15",
      "36": "16", "37": "17", "37.0": "17"}
SC = {"A": "start-on-boot", "B": "always-on", "C": "both", "D": "always-on+lockdown", "E": "both+lockdown", "F": "start-on-boot+battery exempt", "U": "update (install -r), no reboot"}
rows = []
for f in sorted(glob.glob(os.path.join(sys.argv[1], "**", "summary.json"), recursive=True)):
    try:
        r = json.load(open(f))
    except Exception as e:
        print(f"<!-- bad {f}: {e} -->"); continue
    r["_dir"] = os.path.relpath(os.path.dirname(f), sys.argv[1])
    rows.append(r)
apif = lambda a: float(a) if str(a).replace(".", "", 1).isdigit() else 0


def cell(r):
    if r is None:
        return "-"
    if r.get("status") != "ok":
        return f"ERR {r.get('status', '')[:40]}"
    v = f"VPN {r['vpn_up_after_boot_completed_s']}s" if r.get("vpn_up") else "no VPN"
    if r.get("vpn_up") and not r.get("vpn_at_end"):
        v += " (gone at end)"
    c = " CRASH " + ",".join(sorted({e.split(":")[0].split(".")[-1] for e in r.get("crash_exceptions", [])})) if r.get("crash") else ""
    p = f" via {r['path']}" if r.get("vpn_up") else ""
    t = f" net:{r.get('traffic_end')}"
    u = " unhandled-AO" if r.get("unhandled_vpnservice") else ""
    return v + p + c + t + u


for b in sorted({str(r.get("build")) for r in rows}):
    rr = [r for r in rows if str(r.get("build")) == b]
    scs = sorted({r.get("scenario") for r in rr})
    print(f"## {b}\n")
    print("| API (Android) | " + " | ".join(f"{s} {SC.get(s, '')}" for s in scs) + " |")
    print("|---|" + "---|" * len(scs))
    for a in sorted({str(r.get("api")) for r in rr}, key=apif):
        print(f"| {a} ({AV.get(a, '?')}) | " + " | ".join(cell(next((r for r in rr if str(r.get("api")) == a and r.get("scenario") == s), None)) for s in scs) + " |")
    print()

print("## Details\n")
print("| job | status | VPN up s after boot_completed | reboot->boot_completed s | path | starter | [Boot] Received s | Worker s | always-on start s | "
      "unhandled android.net.VpnService | bg-start denied | crash exceptions | traffic before/after/end | startOnBoot after boot | secure after boot | battery exempt |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in sorted(rows, key=lambda r: (str(r.get("build")), apif(r.get("api")), str(r.get("scenario")))):
    p = r.get("probes", {})
    pr = lambda x: (f"{x.get('ok')}/{x.get('ok', 0) + x.get('fail', 0)}" if x else "-")
    print(f"| {r['_dir']} | {r.get('status')} | {r.get('vpn_up_after_boot_completed_s')} | {r.get('times', {}).get('reboot_to_boot_completed_s')} | "
          f"{r.get('path')} | {r.get('starter')} | {r.get('boot_received_after_bc_s')} | {r.get('worker_after_bc_s')} | {r.get('always_on_start_after_bc_s')} | "
          f"{r.get('unhandled_vpnservice')} | {r.get('bg_start_denied')} | {'; '.join(r.get('crash_exceptions', []))[:200] or '-'} | "
          f"{pr(r.get('probe_vpn_before_reboot'))} / {pr(p.get('after_vpn_up') or p.get('mid_no_vpn'))} / {pr(p.get('end'))} | "
          f"{(r.get('datastore_after_boot') or {}).get('startOnBoot')} | {r.get('secure_after_boot')} | {r.get('battery_exempt_before_reboot')} |")
