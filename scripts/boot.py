#!/usr/bin/env python3
"""Boot start test: does Athena's VPN come back after a device reboot, and which path starts it?

usage: boot.py --apk <file> --build <name> --scenario A|B|C|D|E|F --out <dir> [--window 120]

Steps: prep device -> install APK (-g) -> start Athena's VPN through the UI (same focus-gated taps as run.py) -> take
Athena off the power-save allowlist (the consent flow may have put it there; that allowlist exempts it from background
start limits) unless the scenario keeps it -> write Athena's "Start on Boot" setting into its DataStore file (run-as on a
debug build, su otherwise) -> set or clear Always-on VPN in Settings.Secure -> HTTP probe through the VPN -> adb reboot ->
wait for a new boot_id, then sys.boot_completed -> sample dumpsys connectivity for a VPN owned by Athena for --window s
after boot_completed, probe traffic once the VPN is up and at the end -> logcat -b all, dumpsys -> summary.json.

Scenarios (start on boot, always-on, lockdown, keep battery exemption):
  A  on,  off, -, no     B  off, on, 0, no     C  on, on, 0, no
  D  off, on, 1, no      E  on,  on, 1, no     F  on, off, -, yes
  U  on,  off, -, no, and no reboot: adb install -r of the same APK while the VPN runs (times are from the end of the install)
"""
import argparse, json, os, re, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
D = "/data/local/tmp/p2f"
ACT = "com.kin.athena.presentation.MainActivity"
SCEN = {"A": (True, False, False, False), "B": (False, True, False, False), "C": (True, True, False, False),
        "D": (False, True, True, False), "E": (True, True, True, False), "F": (True, False, False, True),
        "U": (True, False, False, False)}  # U: no reboot, adb install -r of the same APK while the VPN runs

ap = argparse.ArgumentParser()
ap.add_argument("--apk", required=True)
ap.add_argument("--build", required=True)
ap.add_argument("--scenario", required=True, choices=sorted(SCEN))
ap.add_argument("--out", required=True)
ap.add_argument("--reboot", default="svc", choices=["svc", "adb"],
                help="svc: svc power reboot (clean shutdown through the framework, like the power menu; app ops and the power-save "
                     "allowlist are written); adb: adb reboot (init reboot without the framework shutdown)")
ap.add_argument("--window", type=int, default=120, help="seconds to sample after sys.boot_completed")
ap.add_argument("--probe", default="203.0.113.1:8080", help="ip:port of the HTTP probe target on the runner")
ap.add_argument("--client-pkg", default="com.android.chrome,com.google.android.youtube,com.google.android.apps.maps,com.android.vending",
                help="run the HTTP probe as the uid of the first installed package (su); falls back to the shell uid")
A = ap.parse_args()
SOB, AON, LOCK, KEEP_EXEMPT = SCEN[A.scenario]
UPD = A.scenario == "U"

OUT = os.path.abspath(A.out)
for d in ("", "snap", "ui"):
    os.makedirs(os.path.join(OUT, d), exist_ok=True)
TL = open(os.path.join(OUT, "timeline.txt"), "a")
T0 = time.time()
TIMES = {}
INFO = {"build": A.build, "scenario": A.scenario, "start_on_boot": SOB, "always_on": AON, "lockdown": LOCK,
        "keep_battery_exemption": KEEP_EXEMPT, "reboot": A.reboot, "apk": os.path.basename(A.apk), "api": os.environ.get("FARM_API"),
        "gh_run": os.environ.get("GITHUB_RUN_ID")}
if os.environ.get("T_EMU0"):
    TIMES["emulator_start_to_booted_s"] = round(T0 - float(os.environ["T_EMU0"]), 1)


def log(msg):
    l = f"{time.time():.3f} {time.strftime('%H:%M:%S')} {msg}"
    print(l, flush=True)
    TL.write(l + "\n"); TL.flush()


def mark(k):
    TIMES[k] = round(time.time() - T0, 1)
    log(f"MARK {k} {TIMES[k]}")


def adb(cmd, to=40):
    try:
        r = subprocess.run(["adb", "shell", cmd], capture_output=True, text=True, timeout=to, stdin=subprocess.DEVNULL)
        return (r.stdout + r.stderr).replace("\r", "").strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def adbx(args, to=120, out=None):
    try:
        if out:
            with open(out, "wb") as f:
                return subprocess.run(["adb"] + args, stdout=f, stderr=subprocess.DEVNULL, timeout=to, stdin=subprocess.DEVNULL).returncode
        r = subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=to, stdin=subprocess.DEVNULL)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def adbbin(args, to=30):
    try:
        return subprocess.run(["adb"] + args, capture_output=True, timeout=to, stdin=subprocess.DEVNULL).stdout
    except subprocess.TimeoutExpired:
        return b""


def snap(name, cmd, to=60):
    o = adb(cmd, to)
    open(os.path.join(OUT, "snap", name + ".txt"), "w").write(o + "\n")
    return o


def finish(status, extra=None):
    p = os.path.join(OUT, "summary.json")
    s = json.load(open(p)) if os.path.exists(p) else {}
    s.update(INFO); s["status"] = status; s["times"] = TIMES
    if extra:
        s.update(extra)
    json.dump(s, open(p, "w"), indent=1)
    log(f"FINISH {status} times={TIMES}")
    sys.exit(0 if status == "ok" else 1)


# ---------------- network state ----------------
def netstate(to=30):
    o = adb("dumpsys connectivity", to)
    d = None
    nets = {}
    for l in o.splitlines():
        m = re.match(r"^Active default network: (\S+)", l)
        if m:
            d = m.group(1)
        if l.startswith("  NetworkAgentInfo{"):
            i = re.search(r"network\{(\d+)\}", l)
            if not i:
                continue
            tr = re.search(r"Transports: ([A-Z|]+)", l)
            ow = re.search(r"(?:OwnerUid|EstablishingAppUid): (\d+)", l)
            nets[i.group(1)] = {"tr": tr.group(1) if tr else "", "v": bool(re.search(r"Capabilities: [A-Z_&]*VALIDATED", l)),
                                "owner": ow.group(1) if ow else ""}
    return d, nets, o


def athena_vpn(nets, uid):
    """VPN networks owned by uid; API < 29 dumpsys shows no owner: any VPN counts (Athena is the only VPN app)"""
    return [k for k, n in nets.items() if "VPN" in n["tr"] and n["owner"] in (uid, "")]


def pkg_uid(pkg):
    m = re.search(rf"package:{re.escape(pkg)} uid:(\d+)", adb(f"pm list packages -U {pkg} 2>/dev/null"))
    if m:
        return m.group(1)
    m = re.search(r"userId=(\d+)", adb(f"dumpsys package {pkg} 2>/dev/null | grep -m1 userId="))
    return m.group(1) if m else None


# ---------------- UI (focus-gated taps, as run.py) ----------------
def focus():
    return adb("dumpsys window 2>/dev/null | grep -m1 mCurrentFocus")


def dismiss_anr():
    x = adb("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; cat /sdcard/ui.xml", 30)
    if "isn't responding" in x:
        m = re.search(r'text="Wait"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', x)
        if m:
            a = [int(v) for v in m.groups()]; adb(f"input tap {(a[0] + a[2]) // 2} {(a[1] + a[3]) // 2}"); log("TAP Wait on an ANR dialog")


def uidump(i):
    adb("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1", 30)
    x = adb("cat /sdcard/ui.xml", 30)
    open(os.path.join(OUT, "ui", f"ui-{i:02d}.xml"), "w").write(x + "\n")
    nodes = []
    for m in re.finditer(r"<node ([^>]*?)/?>", x):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
        b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", a.get("bounds", ""))
        if b:
            a["b"] = tuple(int(v) for v in b.groups())
            nodes.append(a)
    return nodes


def find(nodes, texts):
    for t in texts:
        for n in nodes:
            for k in ("text", "content-desc"):
                if n.get(k, "").strip().lower() == t.lower():
                    return n, t
    return None, None


def tap(pkg_pat, n, why, fg=""):
    f = focus()
    if pkg_pat not in f and pkg_pat not in fg:
        log(f"REFUSE tap {why}: focus={f}"); return False
    x1, y1, x2, y2 = n["b"]
    adb(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
    log(f"TAP {why} at {(x1 + x2) // 2},{(y1 + y2) // 2} ({f})")
    return True


def vpn_up_now(pkg, uid):
    _, nets, _ = netstate()
    v = athena_vpn(nets, uid)
    if v and all(nets[k]["owner"] == "" for k in v):
        if "ServiceRecord" not in adb(f"dumpsys activity services {pkg} | grep -m1 ServiceRecord"):
            return []
    return v


def start_vpn(pkg, uid):
    W, H = 1080, 2400
    m = re.search(r"(\d+)x(\d+)", adb("wm size"))
    if m:
        W, H = int(m.group(1)), int(m.group(2))
    adb(f"am start -W -n {pkg}/{ACT}", 60)
    time.sleep(3)
    shield_taps = 0; relaunches = 0
    for i in range(60):
        f = focus()
        up = vpn_up_now(pkg, uid)
        if up and not re.search(r"vpndialogs|GrantPermissions|permissioncontroller|RequestIgnoreBattery", f):
            log(f"VPN up net={up} focus={f}")
            adb("input keyevent KEYCODE_HOME")
            return True
        nodes = uidump(i)
        pk = {}
        for n in nodes:
            pk[n.get("package", "")] = pk.get(n.get("package", ""), 0) + 1
        fg = max(pk, key=pk.get) if pk else ""
        if fg and fg not in f:
            f = f + " dump=" + fg
        log(f"UI {i} focus={f} vpn={up} texts={[n.get('text') or n.get('content-desc') for n in nodes if n.get('text') or n.get('content-desc')][:25]}")
        anr, _ = find(nodes, ["Wait"])
        if anr and any("isn't responding" in n.get("text", "") for n in nodes):
            x1, y1, x2, y2 = anr["b"]; adb(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}"); log("TAP Wait on an ANR dialog")
        elif "com.android.vpndialogs" in f:
            n, t = find(nodes, ["OK", "Allow"])
            if n: tap("com.android.vpndialogs", n, "vpn-consent " + t, fg)
        elif "GrantPermissions" in f or "permissioncontroller" in f:
            n, t = find(nodes, ["Allow", "ALLOW", "While using the app", "Allow all the time"])
            if n: tap("permissioncontroller", n, "permission " + t, fg)
        elif "RequestIgnoreBatteryOptimizations" in f or "com.android.settings" in f:
            n, t = find(nodes, ["Allow", "ALLOW", "OK"])
            if n: tap("com.android.settings", n, "battery " + t, fg)
        elif pkg in f and any("Disable Firewall" in (n.get("text") or "") for n in nodes):
            n, t = find(nodes, ["Cancel"])
            if n: tap(pkg, n, "keep firewall on: Cancel", fg)
        elif pkg in f and not up and shield_taps >= 2 and relaunches < 3:
            relaunches += 1; shield_taps = 0
            log(f"shield taps had no effect: relaunch #{relaunches}")
            adb(f"am force-stop {pkg}"); time.sleep(1); adb(f"am start -W -n {pkg}/{ACT}", 60); time.sleep(5)
        elif pkg in f:
            n, t = find(nodes, ["Use VPN", "Allow", "Continue", "Next", "Get started", "Get Started", "Accept", "I agree", "Agree",
                                "Got it", "OK", "Done", "Skip", "Start", "Enable"])
            if n:
                tap(pkg, n, "athena " + t, fg)
            elif not up:
                c = [n for n in nodes if n.get("clickable") == "true" and n.get("package") == pkg and n["b"][2] <= W * 0.35
                     and n["b"][1] >= H * 0.02 and n["b"][3] <= H * 0.2 and (n["b"][2] - n["b"][0]) < W * 0.3]
                c.sort(key=lambda n: (n["b"][1], n["b"][0]))
                if c:
                    shield_taps += 1; tap(pkg, c[0], f"shield #{shield_taps} {c[0].get('bounds')}", fg)
                    time.sleep(3)
                else:
                    log("no shield candidate")
                    if fg != pkg and relaunches < 3:
                        relaunches += 1; shield_taps = 0
                        log(f"Athena focused but not drawn: relaunch #{relaunches}")
                        adb(f"am force-stop {pkg}"); time.sleep(1); adb(f"am start -W -n {pkg}/{ACT}", 60); time.sleep(5)
        else:
            log(f"unexpected focus {f}; relaunching")
            adb(f"am start -W -n {pkg}/{ACT}", 60)
        time.sleep(2.5)
    return bool(vpn_up_now(pkg, uid))


# ---------------- DataStore (Preferences protobuf) ----------------
def varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7F) << s; s += 7
        if not c & 0x80:
            return r, i


def enc_varint(n):
    o = bytearray()
    while True:
        c = n & 0x7F; n >>= 7
        o.append(c | (0x80 if n else 0))
        if not n:
            return bytes(o)


def pb_entries(b):
    """PreferenceMap: repeated field 1 = map entry {1: key, 2: Value}; returns [(key, raw value bytes)]"""
    out = []; i = 0
    while i < len(b):
        tag, i = varint(b, i)
        if tag & 7 != 2:
            raise ValueError(f"unexpected wire type {tag}")
        n, i = varint(b, i); e = b[i:i + n]; i += n
        if tag >> 3 != 1:
            continue
        j = 0; k = v = None
        while j < len(e):
            t, j = varint(e, j); m, j = varint(e, j); x = e[j:j + m]; j += m
            if t >> 3 == 1:
                k = x.decode(errors="replace")
            elif t >> 3 == 2:
                v = x
        out.append((k, v))
    return out


def pb_value(v):
    """Value oneof: 1 bool, 2 float, 3 int, 4 long, 5 string ..."""
    if not v:
        return None
    t, j = varint(v, 0)
    if t >> 3 in (1, 3, 4) and t & 7 == 0:
        n, _ = varint(v, j)
        return bool(n) if t >> 3 == 1 else n
    if t & 7 == 2:
        n, j = varint(v, j)
        return v[j:j + n].decode(errors="replace")
    return v.hex()


def pb_build(entries):
    o = bytearray()
    for k, v in entries:
        kb = k.encode()
        e = b"\x0a" + enc_varint(len(kb)) + kb + b"\x12" + enc_varint(len(v)) + v
        o += b"\x0a" + enc_varint(len(e)) + e
    return bytes(o)


def ds_path(pkg):
    return f"/data/data/{pkg}/files/datastore/settings.preferences_pb"


def ds_read(pkg):
    """returns (bytes or None, method)"""
    p = ds_path(pkg)
    if RUNAS_OK:
        if "yes" in adb(f"run-as {pkg} sh -c 'test -f {p} && echo yes'"):
            return adbbin(["exec-out", f"run-as {pkg} cat {p}"]), "run-as"
        return None, "run-as"
    if ROOT:
        if "yes" in adb(f"su 0 sh -c 'test -f {p} && echo yes'"):
            return adbbin(["exec-out", f"su 0 cat {p}"]), "su"
        return None, "su"
    return None, "none"


def ds_decode(b):
    try:
        return {k: pb_value(v) for k, v in pb_entries(b or b"")}
    except Exception as e:
        return {"_error": str(e), "_hex": (b or b"").hex()[:400]}


def ds_set_bool(pkg, uid, key, val):
    b, how = ds_read(pkg)
    ent = [(k, v) for k, v in pb_entries(b or b"") if k != key]
    ent.append((key, b"\x08" + (b"\x01" if val else b"\x00")))
    tmp = os.path.join(OUT, "settings.preferences_pb.new")
    open(tmp, "wb").write(pb_build(ent))
    adbx(["push", tmp, f"{D}/sp.pb"], 30); adb(f"chmod 644 {D}/sp.pb")
    p = ds_path(pkg); dd = os.path.dirname(p)
    if RUNAS_OK:
        r = adb(f"run-as {pkg} sh -c 'mkdir -p {dd} && cat {D}/sp.pb > {p}'")
        log(f"datastore write run-as: {r}")
    if (not RUNAS_OK or ds_decode(ds_read(pkg)[0]).get(key) != val) and ROOT:
        r = adb(f"su 0 sh -c 'mkdir -p {dd} && cat {D}/sp.pb > {p} && chown {uid}:{uid} {dd} {p} && chmod 600 {p} && restorecon -R /data/data/{pkg}/files'")
        log(f"datastore write su: {r}")
    got = ds_decode(ds_read(pkg)[0])
    return got, got.get(key) == val


# ---------------- probe ----------------
PIP, PPORT = A.probe.split(":")


def probe(label):
    o = adb(f"cd {D} && {RUNAS}{D}/mqflap probe {PIP} {PPORT} {PIP} {label} 700 3; sleep 4", 30)
    ok = len(re.findall(r"PROBE \d+ OK", o)); fail = len(re.findall(r"PROBE \d+ FAIL", o))
    r = {"t": round(time.time(), 3), "ok": ok, "fail": fail, "lines": [l for l in o.splitlines() if "PROBE" in l][:6]}
    log(f"PROBE {label}: {ok} ok {fail} fail")
    return r


# ================= main =================
log(f"START {json.dumps(INFO)}")
adbx(["wait-for-device"], 120)
INFO["device"] = {k: adb(f"getprop {p}") for k, p in (("release", "ro.build.version.release"), ("sdk", "ro.build.version.sdk"),
                                                       ("fingerprint", "ro.build.fingerprint"))}
SDK = int(re.sub(r"\D", "", INFO["device"]["sdk"]) or 0)
ROOT = adb("su 0 id").startswith("uid=0")
INFO["su"] = ROOT


def prep():
    for c in ("settings put system screen_off_timeout 2147483647", "svc power stayon true", "locksettings set-disabled true",
              "input keyevent KEYCODE_WAKEUP", "wm dismiss-keyguard", "svc data enable", "svc wifi enable", "logcat -G 16M"):
        adb(c)


prep()
if ROOT:
    adb("su 0 setprop persist.logd.size 16777216")
time.sleep(5)
dismiss_anr()
mark("prep")

o = adbx(["install", "-r", "-g", A.apk], 300)
log(f"install: {o[-200:]}")
pk = re.findall(r"package:(\S+)", adb("pm list packages com.kin.athena"))
mm = [(p, pkg_uid(p)) for p in pk if pkg_uid(p)]
if not mm:
    finish("fail: install", {"install_out": o[-500:]})
PKG, UID = mm[0]
INFO["pkg"], INFO["uid"] = PKG, UID
INFO["version"] = adb(f"dumpsys package {PKG} 2>/dev/null | grep -m1 versionName").strip()
RUNAS_OK = adb(f"run-as {PKG} id").startswith("uid=")
INFO["run_as"] = RUNAS_OK
adb(f"mkdir -p {D} && chmod 777 {D}")
log(f"push mqflap: {adbx(['push', os.path.join(HERE, 'mqflap'), f'{D}/mqflap'], 60)}")
adb(f"chmod 755 {D}/mqflap")
adb(f"appops set {PKG} ACTIVATE_VPN allow")
adb(f"pm grant {PKG} android.permission.POST_NOTIFICATIONS")

CLIENT = ("shell", "2000")
CLIENT_PKGS = []
if ROOT:
    for cp in A.client_pkg.split(","):
        cuid = pkg_uid(cp)
        cu = re.match(r"(\d+)", cuid or "")
        if not (cu and adb(f"su {cu.group(1)} id").startswith(f"uid={cu.group(1)}")):
            continue
        CLIENT = (cp, cu.group(1)); break


def exempt_client():
    # an app uid without a running process is blocked by the background firewall chain on Android 15+
    if CLIENT[0] != "shell":
        for c in (f"dumpsys deviceidle whitelist +{CLIENT[0]}", f"am set-standby-bucket {CLIENT[0]} active",
                  f"cmd appops set {CLIENT[0]} RUN_ANY_IN_BACKGROUND allow", f"am set-inactive {CLIENT[0]} false"):
            adb(c)


exempt_client()
RUNAS = f"su {CLIENT[1]} " if CLIENT[0] != "shell" else ""
INFO["client"] = {"pkg": CLIENT[0], "uid": CLIENT[1]}
INFO["probe_no_vpn"] = probe("novpn")
mark("installed")

# ---- VPN once ----
dismiss_anr()
vok = start_vpn(PKG, UID)
mark("vpn_up" if vok else "vpn_fail")
snap("connectivity-vpn", "dumpsys connectivity")
if not vok:
    adbx(["logcat", "-d", "-v", "epoch", "-b", "all"], 180, os.path.join(OUT, "logcat-prereboot.txt"))
    finish("fail: vpn not started before the reboot")
time.sleep(5)
INFO["probe_vpn_before_reboot"] = probe("vpnpre")

# ---- scenario ----
wl = adb("dumpsys deviceidle whitelist")
INFO["battery_exempt_after_ui"] = PKG in wl
if not KEEP_EXEMPT and PKG in wl:
    log(f"deviceidle whitelist -{PKG}: {adb(f'dumpsys deviceidle whitelist -{PKG}')}")
elif KEEP_EXEMPT and PKG not in wl:
    log(f"deviceidle whitelist +{PKG}: {adb(f'dumpsys deviceidle whitelist +{PKG}')}")
INFO["battery_exempt_before_reboot"] = PKG in adb("dumpsys deviceidle whitelist")
b, how = ds_read(PKG)
INFO["datastore_method"] = how
INFO["datastore_initial"] = ds_decode(b)
got, ok = ds_set_bool(PKG, UID, "startOnBoot", SOB)
INFO["datastore_before_reboot"] = got
log(f"startOnBoot={SOB} written={ok} datastore={got}")
if not ok:
    finish("fail: could not write startOnBoot", {})
if AON:
    adb(f"settings put secure always_on_vpn_app {PKG}")
    adb(f"settings put secure always_on_vpn_lockdown {1 if LOCK else 0}")
else:
    adb("settings delete secure always_on_vpn_app")
    adb("settings put secure always_on_vpn_lockdown 0")
INFO["secure_before_reboot"] = {k: adb(f"settings get secure {k}") for k in ("always_on_vpn_app", "always_on_vpn_lockdown")}
log(f"secure settings {INFO['secure_before_reboot']}")
snap("services-prereboot", f"dumpsys activity services {PKG} | grep -E 'ServiceRecord|startRequested|app=ProcessRecord'")
adbx(["logcat", "-d", "-v", "epoch", "-b", "all"], 180, os.path.join(OUT, "logcat-prereboot.txt"))
mark("scenario_set")

# logcat from the host as soon as adb is back (the boot logs may wrap a small buffer before the final dump)
LC = {"stop": False}


def logcat_stream():
    with open(os.path.join(OUT, "logcat-stream.txt"), "w") as f:
        while not LC["stop"]:
            p = subprocess.Popen(["adb", "logcat", "-v", "epoch", "-b", "all"], stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            while p.poll() is None and not LC["stop"]:
                time.sleep(0.5)
            if p.poll() is None:
                p.terminate()
            f.write(f"--- logcat stream ended {time.time():.3f}\n"); f.flush()
            time.sleep(1)


_, _nets, _ = netstate()
PRE_VPN = athena_vpn(_nets, UID)  # U: the VPN network from before the update does not count as "back"
INFO["vpn_net_before"] = PRE_VPN
# ---- reboot, or (scenario U) an in-place update with adb install -r ----
log(f"appops write-settings: {adb('appops write-settings')}")
time.sleep(6)  # DeviceIdleController writes its allowlist file a few seconds after a change
th = threading.Thread(target=logcat_stream, daemon=True)
if UPD:
    th.start(); time.sleep(2)
    t_reboot = time.time()
    TIMES["reboot_at"] = round(t_reboot - T0, 1)
    o = adbx(["install", "-r", A.apk], 300)
    t_adb = t_bc = time.time()
    INFO["update_out"] = o[-200:]; INFO["reboot_method"] = "none (adb install -r)"
    log(f"UPDATE adb install -r took {t_bc - t_reboot:.1f} s: {o[-200:]}")
else:
    BOOT_ID = adb("cat /proc/sys/kernel/random/boot_id")
    t_reboot = time.time()
    if A.reboot == "svc":
        # API 26 ignores svc power reboot from the shell uid: run it as root where the image allows
        log(f"REBOOT svc power reboot boot_id={BOOT_ID} {adb(('su 0 ' if ROOT else '') + 'svc power reboot', 30)}")
    else:
        log(f"REBOOT adb reboot boot_id={BOOT_ID} {adbx(['reboot'], 60)}")
    INFO["reboot_method"] = A.reboot
    TIMES["reboot_at"] = round(t_reboot - T0, 1)
    t_adb = t_bc = None
    reconnects = 0
    while time.time() - t_reboot < 600:
        st = adbx(["get-state"], 10)
        if st.strip() == "device":
            bid = adb("cat /proc/sys/kernel/random/boot_id", 10)
            if re.match(r"^[0-9a-f-]{36}$", bid) and bid != BOOT_ID:
                t_adb = time.time(); break
            if bid == BOOT_ID and INFO["reboot_method"] == "svc" and time.time() - t_reboot > 45:
                INFO["reboot_method"] = "adb (svc power reboot did not reboot)"
                log(f"still the same boot after 45 s: adb reboot {adbx(['reboot'], 60)}")
        elif time.time() - t_reboot > 90 * (reconnects + 1) and reconnects < 4:
            reconnects += 1
            log(f"device state {st!r}: adb reconnect #{reconnects} {adbx(['reconnect', 'offline'], 20)}")
            if reconnects >= 2:
                adbx(["kill-server"], 20); adbx(["start-server"], 30)
        time.sleep(1)
    if not t_adb:
        finish("fail: device did not come back after adb reboot", {"adb_state": adbx(["devices", "-l"], 10)})
    log(f"adb back after {t_adb - t_reboot:.1f} s")
    th.start()

samples = []
t_vpn = None
PROBES = {}


def sample():
    t = time.time()
    bc = adb("getprop sys.boot_completed", 10)
    d, nets, _ = netstate(15)
    v = [k for k in athena_vpn(nets, UID) if not (UPD and k in PRE_VPN)]
    pid = adb(f"pidof {PKG}", 10)
    s = {"t": round(t, 3), "bc": bc, "def": d, "vpn": [f"{k}:{nets[k]['tr']}:{'V' if nets[k]['v'] else 'nv'}:owner{nets[k]['owner']}" for k in v],
         "nvpn_any": sum(1 for n in nets.values() if "VPN" in n["tr"]), "pid": pid if re.match(r"^[\d ]+$", pid or "") else ""}
    samples.append(s)
    return s


while not UPD and time.time() - t_adb < 420:
    s = sample()
    if s["bc"] == "1":
        t_bc = time.time(); break
    if s["vpn"] and t_vpn is None:
        t_vpn = s["t"]
    time.sleep(1)
if not t_bc:
    LC["stop"] = True
    finish("fail: no sys.boot_completed after the reboot", {"samples": samples[-20:]})
log(f"boot_completed {t_bc - t_reboot:.1f} s after adb reboot")
TIMES["reboot_to_adb_s"] = round(t_adb - t_reboot, 1)
TIMES["reboot_to_boot_completed_s"] = round(t_bc - t_reboot, 1)
adb("input keyevent KEYCODE_WAKEUP"); adb("wm dismiss-keyguard")
exempt_client()
probe_mid = False
while time.time() - t_bc < A.window:
    s = sample()
    if s["vpn"] and t_vpn is None:
        t_vpn = s["t"]; log(f"VPN up {t_vpn - t_bc:.1f} s after boot_completed: {s}")
        time.sleep(3)
        PROBES["after_vpn_up"] = probe("vpnup")
    if not probe_mid and t_vpn is None and time.time() - t_bc > A.window / 2:
        probe_mid = True; PROBES["mid_no_vpn"] = probe("midnovpn")
    time.sleep(1)
last = sample()
PROBES["end"] = probe("end")
mark("window_done")

# ---- collect ----
snap("connectivity-end", "dumpsys connectivity")
snap("services-end", f"dumpsys activity services {PKG}")
snap("jobscheduler", f"dumpsys jobscheduler | grep -iE -A3 '{PKG}' | head -150")
snap("dropbox-crash", "dumpsys dropbox --print data_app_crash 2>/dev/null | head -300")
snap("deviceidle", "dumpsys deviceidle whitelist")
snap("vpn-dumpsys", "dumpsys connectivity | grep -iE -A2 'vpn|always' | head -120")
INFO["secure_after_boot"] = {k: adb(f"settings get secure {k}") for k in ("always_on_vpn_app", "always_on_vpn_lockdown")}
INFO["datastore_after_boot"] = ds_decode(ds_read(PKG)[0])
INFO["battery_exempt_after_boot"] = PKG in adb("dumpsys deviceidle whitelist")
LC["stop"] = True; time.sleep(1.5)
adbx(["logcat", "-d", "-v", "epoch", "-b", "all"], 180, os.path.join(OUT, "logcat-full.txt"))

# ---- analysis ----
lines = []
seen = set()
for fn in ("logcat-stream.txt", "logcat-full.txt"):
    try:
        for l in open(os.path.join(OUT, fn), errors="replace"):
            l = l.rstrip("\n")
            if l not in seen:
                seen.add(l); lines.append(l)
    except FileNotFoundError:
        pass


def ts(l):
    m = re.match(r"^\s*(\d{9,}\.\d+)", l)
    return float(m.group(1)) if m else None


lines.sort(key=lambda l: ts(l) or 0)
boot_lines = [l for l in lines if (ts(l) or 0) >= t_reboot - 1]
with open(os.path.join(OUT, "athena.txt"), "w") as fa:
    for l in boot_lines:
        if re.search(r" [VDIWEF] (?:Athena|NetGuard\.JNI|WM-\S+)\s*:", l) or PKG in l:
            fa.write(l + "\n")

PAT = {
    "fix7": r"start source=\S+ .*result=|Firewall is not running|Not-running notification|Background start refused|Failed to start|Restart after process death",
    "am_crash": r"am_crash.*com\.kin\.athena",
    "unhandled_vpnservice": r"Unhandled action: android\.net\.VpnService",
    "boot_debug": r"\[Boot\]",
    "boot_info": r"Scheduled boot services via WorkManager|VPN permission not granted|Failed to load settings (on boot|for boot start)|Error in boot start worker|Network speed monitor started on boot",
    "workmanager": r"BootStartWorker|boot_start_work",
    "bg_start": r"ForegroundServiceStartNotAllowed|BackgroundServiceStartNotAllowed|Background start not allowed|Not allowed to start service|app is in background",
    "vpn_start": r"\[Vpn\] (Client onStartCommand|VpnManager\.start|Client starting|Starting the server)|\[Fw\] startFirewall",
    "always_on_sys": r" [VDIWEF] (Vpn|VpnManagerService|LockdownVpnTracker)\s*:|[Aa]lways.?[Oo]n.*[Aa]thena|[Aa]thena.*[Aa]lways.?[Oo]n",
}
EVD = {k: [] for k in PAT}
for l in boot_lines:
    for k, p in PAT.items():
        if re.search(p, l) and (k != "bg_start" or PKG in l):
            EVD[k].append(l)
# the AndroidRuntime crash block(s) for Athena
crash_blocks = {}  # pid -> AndroidRuntime lines from FATAL EXCEPTION on (logcat -b all interleaves other tags)
for l in boot_lines:
    m = re.match(r"^\s*\S+\s+(\d+)\s+\d+\s+E AndroidRuntime", l)
    if not m:
        continue
    if "FATAL EXCEPTION" in l:
        crash_blocks.setdefault(m.group(1), []).append([l])
    elif crash_blocks.get(m.group(1)) and len(crash_blocks[m.group(1)][-1]) < 40:
        crash_blocks[m.group(1)][-1].append(l)
athena_crashes = [b for bl in crash_blocks.values() for b in bl if any(PKG in x for x in b[:4])]
crashed = bool(athena_crashes)
exc = []
for b in athena_crashes:
    m = next((re.sub(r"^.*AndroidRuntime\s*:\s*", "", x) for x in b if re.search(r"Exception|Error", x) and "FATAL" not in x), "")
    th_ = re.sub(r"^.*FATAL EXCEPTION:\s*", "", b[0]).strip()
    fr = next((re.sub(r"^.*AndroidRuntime\s*:\s*", "", x).strip() for x in b if re.search(r"\bat com\.kin\.", x)), "")
    exc.append(f"{m[:240]} [thread {th_}; {fr[:140]}]")


def first_t(k, p=None):
    for l in EVD[k]:
        if p is None or re.search(p, l):
            return ts(l)
    return None


recv_t = first_t("boot_debug", r"Received android\.intent\.action\.BOOT_COMPLETED")
worker_t = first_t("boot_debug", r"BootStartWorker id=") or first_t("workmanager")
aon_t = first_t("unhandled_vpnservice") or first_t("vpn_start", r"action=android\.net\.VpnService")
worker_ev = bool(worker_t)
boot_start = [l for l in EVD["boot_debug"] if "Starting the VPN" in l]
own_start = [l for l in EVD["vpn_start"] if "onStartCommand action=com.kin.athena.START_VPN" in l]
starter = boot_start[0].split("[Boot]", 1)[1].strip() if boot_start else None
src = next((re.search(r"\[Vpn\] start source=(\S+) .*result=started", l).group(1) for l in EVD["fix7"]
            if re.search(r"\[Vpn\] start source=(\S+) .*result=started", l)), None)
if src and not starter:
    starter = f"source={src}"
if t_vpn:
    if src:  # FIX7 and later: the client logs who started it
        path = {"boot": "BootReceiver (FGS)", "update": "update receiver", "always-on": "Always-on", "sticky": "sticky restart"}.get(src, src)
    elif boot_start:
        path = "Worker" if worker_ev else "BootReceiver"
        if aon_t and ts(boot_start[0]) and aon_t < ts(boot_start[0]):
            path += " (process started by Always-on)"
    elif SOB and not EVD["boot_debug"]:  # release build: no debug lines
        path = ("Worker" if worker_ev else "BootReceiver") + " (inferred)"
        if aon_t:
            path += " (Always-on start seen)"
    elif aon_t and not own_start:
        path = "Always-on"
    else:
        path = "unknown"
else:
    path = "none"
rel = lambda t: round(t - t_bc, 1) if t else None
pv = PROBES.get("after_vpn_up") or PROBES.get("mid_no_vpn") or {}
pe = PROBES.get("end", {})
traffic_end = "works" if pe.get("ok") else ("blocked" if pe.get("fail") else "?")
summary = {
    "vpn_up": bool(t_vpn), "vpn_up_after_boot_completed_s": rel(t_vpn), "vpn_up_after_reboot_s": round(t_vpn - t_reboot, 1) if t_vpn else None,
    "vpn_at_end": bool(last["vpn"]), "vpn_samples_up": sum(1 for s in samples if s["vpn"]), "n_samples": len(samples),
    "athena_pid_end": last["pid"], "crash": crashed, "crash_exceptions": exc, "crash_blocks": athena_crashes[:3],
    "path": path, "starter": starter, "boot_received_after_bc_s": rel(recv_t), "worker_after_bc_s": rel(worker_t),
    "always_on_start_after_bc_s": rel(aon_t), "unhandled_vpnservice": len(EVD["unhandled_vpnservice"]),
    "bg_start_denied": len(EVD["bg_start"]), "probes": PROBES, "traffic_end": traffic_end,
    "traffic_after_vpn_or_mid": "works" if pv.get("ok") else ("blocked" if pv.get("fail") else "?"),
    "evidence": {k: v[:25] for k, v in EVD.items()}, "samples": samples,
}
json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)
log(f"RESULT vpn_up={summary['vpn_up']} t={summary['vpn_up_after_boot_completed_s']} crash={crashed} {exc} path={path} "
    f"unhandled={summary['unhandled_vpnservice']} bg_denied={summary['bg_start_denied']} traffic_end={traffic_end}")
mark("done")
finish("ok")
