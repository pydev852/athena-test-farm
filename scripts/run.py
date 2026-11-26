#!/usr/bin/env python3
"""One farm job on one booted emulator (adb device already up). Ported from the phone-S20 flap harness.

usage: run.py --apk <file> --build <name> --pattern a|b|c --run <n> --out <dir>

Steps: prep device -> install APK (-g) -> wait for GmsCore's GCM (mtalk) connection -> start Athena's VPN through
the UI (focus-gated taps, uiautomator) -> verify the VPN network is owned by the app uid -> workloads (MQTT
subscription to a local mosquitto on the runner with a host-side publisher, IP + DNS probes, sampler; run as an
app uid via su when the image allows it, else as the shell uid, which Android 14+ socket kills skip) -> flap pattern ->
collect logcat/dumpsys -> analyze.py -> summary.json.

Patterns (mobile data on, mobile_data_always_on=1, Wi-Fi = emulator "AndroidWifi"):
  a  svc wifi disable -> cellular default -> hold 5 s -> svc wifi enable -> Wi-Fi default; tail 90 s
  b  5 x (disable -> cellular default -> hold 10 s -> enable -> Wi-Fi default -> hold 10 s); tail 90 s
  c  svc wifi disable -> cellular default, stay 180 s
"""
import argparse, json, os, re, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
D = "/data/local/tmp/p2f"
ACT = "com.kin.athena.presentation.MainActivity"

ap = argparse.ArgumentParser()
ap.add_argument("--apk", required=True)
ap.add_argument("--build", required=True)
ap.add_argument("--pattern", required=True, choices=["a", "b", "c"])
ap.add_argument("--run", default="1")
ap.add_argument("--out", required=True)
ap.add_argument("--gcm-wait", type=int, default=300)
ap.add_argument("--broker", default="203.0.113.1", help="MQTT broker IP (default: local mosquitto on the runner, lo alias)")
ap.add_argument("--probe", default="203.0.113.1:8080", help="ip:port of an HTTP server for the fresh-TCP probe")
ap.add_argument("--client-pkg", default="com.android.chrome,com.google.android.youtube,com.google.android.apps.maps,com.android.vending",
                help="run the measurement clients as the uid of the first installed package (su); falls back to the shell uid")
A = ap.parse_args()

OUT = os.path.abspath(A.out)
for d in ("", "snap", "ui"):
    os.makedirs(os.path.join(OUT, d), exist_ok=True)
TL = open(os.path.join(OUT, "timeline.txt"), "a")
T0 = time.time()
TIMES = {}  # step -> seconds since the script started (emulator already booted)
INFO = {"build": A.build, "pattern": A.pattern, "run": A.run, "apk": os.path.basename(A.apk),
        "api": os.environ.get("FARM_API"), "gh_run": os.environ.get("GITHUB_RUN_ID"),
        "t_job0": os.environ.get("T_JOB0"), "t_emu0": os.environ.get("T_EMU0")}
if INFO["t_emu0"]:
    TIMES["emulator_start_to_booted_s"] = round(T0 - float(INFO["t_emu0"]), 1)
if INFO["t_job0"]:
    TIMES["job_start_to_booted_s"] = round(T0 - float(INFO["t_job0"]), 1)


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


def snap(name, cmd, to=60):
    o = adb(cmd, to)
    open(os.path.join(OUT, "snap", name + ".txt"), "w").write(o + "\n")
    return o


def finish(status, extra=None):
    """write summary.json: analyze.py output (if it ran) + run info; exit 0 on ok, 1 otherwise"""
    p = os.path.join(OUT, "summary.json")
    s = json.load(open(p)) if os.path.exists(p) else {}
    s.update(INFO); s["status"] = status; s["times"] = TIMES
    if extra:
        s.update(extra)
    json.dump(s, open(p, "w"), indent=1)
    log(f"FINISH {status} times={TIMES}")
    sys.exit(0 if status == "ok" else 1)


# ---------------- network state (dumpsys connectivity) ----------------
def netstate():
    o = adb("dumpsys connectivity", 30)
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
            ow = re.search(r"OwnerUid: (\d+)", l)
            un = re.search(r"UnderlyingNetworks: (\[[0-9, ]*\]|Null)", l)
            nets[i.group(1)] = {"tr": tr.group(1) if tr else "", "v": bool(re.search(r"Capabilities: [A-Z_&]*VALIDATED", l)),
                                "owner": ow.group(1) if ow else "", "und": un.group(1).replace(" ", "") if un else ""}
    return d, nets


def vpn_owned_by(uid):
    d, nets = netstate()
    return [k for k, n in nets.items() if "VPN" in n["tr"] and n["owner"] == uid]


# ---------------- GCM ----------------
def gcm_state():
    o = adb("dumpsys activity service com.google.android.gms/.gcm.GcmService 2>/dev/null | head -60", 30)
    st = ""
    for l in o.splitlines():
        if re.match(r"^ +(connected=|disconnected|connecting|Connection)", l):
            st = l.strip().split(",")[0]; break
    c = re.search(r"connects=(\d+)", o)
    return st, (int(c.group(1)) if c else None), o


def wait_gcm(tmax, label):
    end = time.time() + tmax; st = ""
    while time.time() < end:
        st, c, o = gcm_state()
        if st.startswith("connected="):
            open(os.path.join(OUT, "snap", f"gcm-{label}.txt"), "w").write(o + "\n")
            return True, st
        time.sleep(3)
    open(os.path.join(OUT, "snap", f"gcm-{label}-timeout.txt"), "w").write(gcm_state()[2] + "\n")
    return False, st


# ---------------- UI (focus-gated taps) ----------------
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
    adbx(["exec-out", "screencap", "-p"], 30, os.path.join(OUT, "ui", f"ui-{i:02d}.png"))
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
    """tap the centre of node n if the focused window (or the package of the UI dump just taken) matches pkg_pat.
    API 34 images keep mCurrentFocus on the launcher while its ANR dialog comes and goes, so the dump package counts too"""
    f = focus()
    if pkg_pat not in f and pkg_pat not in fg:
        log(f"REFUSE tap {why}: focus={f}"); return False
    x1, y1, x2, y2 = n["b"]
    adb(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
    log(f"TAP {why} at {(x1 + x2) // 2},{(y1 + y2) // 2} ({f})")
    return True


def start_vpn(pkg, uid):
    W, H = 1080, 2400
    m = re.search(r"(\d+)x(\d+)", adb("wm size"))
    if m:
        W, H = int(m.group(1)), int(m.group(2))
    adb(f"am start -W -n {pkg}/{ACT}", 60)
    time.sleep(3)
    shield_taps = 0
    for i in range(45):
        f = focus()
        up = vpn_owned_by(uid)
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
        elif pkg in f:
            n, t = find(nodes, ["Use VPN", "Allow", "Continue", "Next", "Get started", "Get Started", "Accept", "I agree", "Agree",
                                "Got it", "OK", "Done", "Skip", "Start", "Enable"])
            if n:
                tap(pkg, n, "athena " + t, fg)
            elif not up and shield_taps < 4:
                # shield: first clickable node in the top-left of the home search bar
                c = [n for n in nodes if n.get("clickable") == "true" and n.get("package") == pkg and n["b"][2] <= W * 0.35
                     and n["b"][1] >= H * 0.02 and n["b"][3] <= H * 0.2 and (n["b"][2] - n["b"][0]) < W * 0.3]
                c.sort(key=lambda n: (n["b"][1], n["b"][0]))
                if c:
                    shield_taps += 1; tap(pkg, c[0], f"shield #{shield_taps} {c[0].get('bounds')}", fg)
                    time.sleep(3)
                else:
                    log("no shield candidate")
        else:
            log(f"unexpected focus {f}; relaunching")
            adb(f"am start -W -n {pkg}/{ACT}", 60)
        time.sleep(2.5)
    return bool(vpn_owned_by(uid))


# ================= main =================
log(f"START {json.dumps(INFO)}")
adbx(["wait-for-device"], 120)
INFO["device"] = {k: adb(f"getprop {p}") for k, p in (("release", "ro.build.version.release"), ("sdk", "ro.build.version.sdk"),
                                                       ("fingerprint", "ro.build.fingerprint"))}
INFO["device"]["gms"] = adb("dumpsys package com.google.android.gms 2>/dev/null | grep -m1 versionName").strip().splitlines()[0:1]
INFO["device"]["nproc"] = adb("nproc")
for c in ("settings put system screen_off_timeout 2147483647", "svc power stayon true", "locksettings set-disabled true",
          "input keyevent KEYCODE_WAKEUP", "wm dismiss-keyguard", "settings put global mobile_data_always_on 1",
          "svc data enable", "svc wifi enable", "logcat -G 16M"):
    adb(c)
ROOT = adb("su 0 id").startswith("uid=0")
INFO["su"] = ROOT
time.sleep(5)
dismiss_anr()
snap("connectivity-boot", "dumpsys connectivity")
snap("wifi-boot", "cmd wifi status")
snap("ifaces-boot", "ip addr; ip rule; getprop | grep -iE 'gsm|radio' | head -30")
mark("prep")

# ---- install ----
o = adbx(["install", "-r", "-g", A.apk], 300)
log(f"install: {o[-200:]}")
pk = adb("pm list packages -U com.kin.athena")
mm = re.findall(r"package:(\S+) uid:(\d+)", pk)
if not mm:
    finish("fail: install", {"install_out": o[-500:]})
PKG, UID = mm[0]
INFO["pkg"], INFO["uid"] = PKG, UID
gu = re.search(r"uid:(\d+)", adb("pm list packages -U com.google.android.gms | grep -m1 'gms uid'"))
GMS_UID = gu.group(1) if gu else "0"
adb(f"mkdir -p {D} && chmod 777 {D}")
for f in ("mqflap", "samp.sh", "dnsprobe.sh"):
    log(f"push {f}: {adbx(['push', os.path.join(HERE, f), f'{D}/{f}'], 60)}")
adb(f"chmod 755 {D}/mqflap {D}/samp.sh {D}/dnsprobe.sh")
PIP, PPORT = A.probe.split(":")


def selftest(runas):
    """3 fresh TCP probes to the local HTTP server (no VPN yet); returns the number that succeeded"""
    o = adb(f"cd {D} && {runas}{D}/mqflap probe {PIP} {PPORT} {PIP} selftest 700 2; sleep 3", 30)
    return len(re.findall(r"PROBE \d+ OK", o)), o


CLIENT = ("shell", "2000")
INFO["client_selftest"] = {}
if ROOT:
    for cp in A.client_pkg.split(","):
        cu = re.search(r"uid:(\d+)", adb(f"pm list packages -U {cp} | grep -m1 'package:{cp} '"))
        if not (cu and adb(f"su {cu.group(1)} id").startswith(f"uid={cu.group(1)}")):
            continue
        # an app uid without a running process is blocked by the background firewall chain on Android 15+:
        # exempt the client package (power-save allowlist, active standby bucket)
        for c in (f"dumpsys deviceidle whitelist +{cp}", f"am set-standby-bucket {cp} active",
                  f"cmd appops set {cp} RUN_ANY_IN_BACKGROUND allow", f"am set-inactive {cp} false"):
            adb(c)
        n, o = selftest(f"su {cu.group(1)} ")
        INFO["client_selftest"][cp] = n
        log(f"client selftest {cp} uid {cu.group(1)}: {n} OK | {o[-300:]}")
        if n:
            CLIENT = (cp, cu.group(1)); break
if CLIENT[0] == "shell":
    INFO["client_selftest"]["shell"] = selftest("")[0]
snap("netpolicy", "dumpsys netpolicy | head -80")
INFO["client"] = {"pkg": CLIENT[0], "uid": CLIENT[1],
                  "note": "app uid via su" if CLIENT[0] != "shell" else "shell uid 2000: NOT representative on Android 14+ (socket kill skips it)"}
log(f"client {INFO['client']}")
adb(f"appops set {PKG} ACTIVATE_VPN allow")
adb(f"dumpsys deviceidle whitelist +{PKG}")
adb(f"pm grant {PKG} android.permission.POST_NOTIFICATIONS")
INFO["version"] = adb(f"dumpsys package {PKG} 2>/dev/null | grep -m1 versionName").strip().splitlines()[0:1]
mark("installed")

# ---- GCM before VPN ----
ok, st = wait_gcm(A.gcm_wait, "before-vpn")
INFO["gcm_before_vpn"] = st
mark("gcm_connected" if ok else "gcm_timeout")
log(f"GCM before VPN: {ok} {st}")

# ---- VPN ----
dismiss_anr()
vok = start_vpn(PKG, UID)
mark("vpn_up" if vok else "vpn_fail")
snap("connectivity-vpn", "dumpsys connectivity")
if not vok:
    adbx(["logcat", "-d", "-v", "epoch", "-b", "all"], 180, os.path.join(OUT, "logcat-full.txt"))
    finish("fail: vpn not started")
d, nets = netstate()
INFO["vpn_net"] = {k: n for k, n in nets.items() if "VPN" in n["tr"]}
time.sleep(5)
ok2, st2 = wait_gcm(180, "after-vpn")
INFO["gcm_after_vpn"] = st2
mark("gcm_after_vpn" if ok2 else "gcm_after_vpn_timeout")

# ---- client app in the foreground ----
# Android 16 destroys the TCP sockets of an idle uid about every 10 s (am_uid_idle -> InetDiagMessage "Destroyed live tcp
# sockets for uids={<uid>}"). A client running as an app uid with no process of its own goes idle, so bring that app to the
# foreground: its uid is then active, like an app in use.
if CLIENT[0] != "shell":
    adb(f"monkey -p {CLIENT[0]} -c android.intent.category.LAUNCHER 1", 60)
    time.sleep(5)
    INFO["client_foreground"] = focus()
    log(f"client app foreground: {INFO['client_foreground']}")

# ---- workloads ----
BROKER = socket.gethostbyname(A.broker)
INFO["broker_ip"], INFO["probe_target"] = BROKER, A.probe
RUNAS = f"su {CLIENT[1]} " if CLIENT[0] != "shell" else ""
CAT = "su 0 cat" if ROOT else "cat"
tag = f"{A.build}-{A.pattern}{A.run}-api{INFO['api'] or INFO['device']['sdk']}"
DUR = {"a": 260, "b": 520, "c": 330}[A.pattern]
adb(f"cd {D} && rm -f stop *.{tag}")
adb(f"cd {D} && (setsid sh samp.sh samp.{tag} {DUR + 120} {PKG} {GMS_UID} >/dev/null 2>&1 </dev/null &)")
time.sleep(3)


def last_samp():
    return adb(f"tail -n1 {D}/samp.{tag} 2>/dev/null")



def parse(l):
    return dict(w.split("=", 1) for w in l.split() if "=" in w)


def wait_default(tr, tmax, fallback=None):
    end = time.time() + tmax; fb = time.time() + 15
    while True:
        l = last_samp(); f = parse(l)
        p = f.get("def", "").split(":")
        if len(p) == 3 and p[1] == tr and p[2] == "V":
            v = f.get("vpn", "none")
            if v == "none" or f"und[{p[0]}]" in v or "undNull" in v or "und[" not in v:
                return True, l.split()[0] if l else ""
        if fallback and time.time() > fb:
            log(f"fallback: {fallback}"); adb(fallback); fallback = None
        if time.time() > end:
            log(f"WARN not {tr} default after {tmax}s: {l}")
            return False, ""
        time.sleep(0.5)


def act(cmd):
    r = adb(f"{cmd}; date +%s.%N | cut -c1-14")
    t = r.splitlines()[-1] if r else ""
    log(f"ACT {cmd} dev_t={t}")
    return t


ok, _ = wait_default("WIFI", 90)
if not ok:
    adb(f"touch {D}/stop"); finish("fail: Wi-Fi not default before the pattern")
f = parse(last_samp())
vpn = f.get("vpn", "none")
if f"owner{UID}" not in vpn:
    adb(f"touch {D}/stop"); finish(f"fail: VPN owner {vpn}, want {UID}")
t_h = time.time(); dt = adb("date +%s.%N | cut -c1-14"); t_h2 = time.time()
off = float(dt) - (t_h + t_h2) / 2 if dt and dt[0].isdigit() else 0.0
meta = dict(tag=tag, build=A.build, pattern=A.pattern, run=A.run, battery=100, clock_offset_dev_minus_host=round(off, 3),
            mdao=adb("settings get global mobile_data_always_on"), vpn=vpn, pids=f.get("pids"),
            wifi=adb("cmd wifi status | grep -m1 'Wifi is'"))
log(f"META {json.dumps(meta)}")
LT = int(float(dt)) - 2 if dt and dt[0].isdigit() else int(time.time()) - 2
lc = subprocess.Popen(["adb", "logcat", "-v", "epoch", "-b", "main,system,crash,events", "-T", f"{LT}.000"],
                      stdout=open(os.path.join(OUT, "logcat-all.txt"), "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
topic = f"athfarm/{INFO['gh_run'] or 'local'}/{tag}"
pub = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "pub.py"), BROKER, topic, "5", str(DUR + 30)],
                       stdout=open(os.path.join(OUT, "pub.log"), "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
adb(f"cd {D} && (setsid {RUNAS}{D}/mqflap mq {BROKER} 1883 {topic} {tag} 30 {DUR} > mq.{tag} 2>&1 </dev/null &)"
    f" && (setsid {RUNAS}{D}/mqflap probe {PIP} {PPORT} {PIP} {tag} 2000 {DUR} > probe.{tag} 2>&1 </dev/null &)"
    f" && (setsid {RUNAS}sh {D}/dnsprobe.sh dns.{tag} {DUR} >/dev/null 2>&1 </dev/null &)")
time.sleep(20)
log(f"WARM mq=[{adb(f'grep -cE READY {D}/mq.{tag}')} READY, {adb(f'grep -c RXPUSH {D}/mq.{tag}')} RX] {last_samp()}")
mark("workloads_up")

EV = []
OFF_FB = "cmd wifi set-wifi-enabled disabled"
ON_FB = "cmd wifi set-wifi-enabled enabled"


def flap(hold):
    e = {"off": act("svc wifi disable")}
    ok, t = wait_default("CELLULAR", 60, OFF_FB); e["cell_default"] = t
    time.sleep(hold)
    e["on"] = act("svc wifi enable")
    ok, t = wait_default("WIFI", 90, ON_FB); e["wifi_default"] = t
    EV.append(e); log(f"FLAP {e}")


if A.pattern == "a":
    flap(5); tail = 90
elif A.pattern == "b":
    for i in range(5):
        flap(10)
        if i < 4:
            time.sleep(10)
    tail = 90
else:
    e = {"off": act("svc wifi disable")}
    ok, t = wait_default("CELLULAR", 60, OFF_FB); e["cell_default"] = t
    EV.append(e); log(f"MOVE {e}"); tail = 180
time.sleep(tail)
log(f"END {last_samp()}")
mark("pattern_done")

# ---- collect ----
snap("connectivity-end", "dumpsys connectivity")
snap("athena-services", f"dumpsys activity services {PKG} | grep -E 'ServiceRecord|startRequested|app=ProcessRecord'")
snap("gcm-end", "dumpsys activity service com.google.android.gms/.gcm.GcmService | head -80")
adb(f"touch {D}/stop"); time.sleep(4)
for k in ("samp", "mq", "probe", "dns"):
    open(os.path.join(OUT, f"{k}.txt"), "w").write(adb(f"{CAT} {D}/{k}.{tag}", 60) + "\n")
adb(f"rm -f {D}/*.{tag} {D}/stop; pkill -f mqflap" + ("; su 0 pkill -f mqflap" if ROOT else ""))
lc.terminate(); pub.terminate()
adbx(["logcat", "-d", "-v", "epoch", "-b", "all"], 180, os.path.join(OUT, "logcat-full.txt"))
with open(os.path.join(OUT, "logcat-full.txt"), errors="replace") as fi, open(os.path.join(OUT, "athena.txt"), "w") as fa, \
        open(os.path.join(OUT, "gcm-events.txt"), "w") as fg:
    for l in fi:
        if re.search(r" [VDIWEF] Athena\s*:", l) or f" {PKG}" in l:
            fa.write(l)
        if "gtalk_conn" in l:
            fg.write(l)
if A.pattern == "c":
    adb("svc wifi enable")
meta["events"] = EV; meta["end_battery"] = 100
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "analyze.py"), OUT], capture_output=True, text=True)
log(f"ANALYZE {r.stdout.strip()} {r.stderr.strip()[-800:]}")
mark("done")
finish("ok" if os.path.exists(os.path.join(OUT, "summary.json")) else "fail: analyze", {"flaps": len(EV)})
