#!/usr/bin/env python3
"""Host MQTT publisher for the phone-S20 flap test: publishes "P<seq> <host_epoch>" (QoS0) to <topic> every <iv> s
on one persistent connection (reconnects on error). Logs "<epoch> PUB <seq>" / "<epoch> PUBERR ...".
usage: pub.py <broker_ip> <topic> <iv_s> <dur_s>"""
import socket, struct, sys, time, random

def s16(x):
    x = x.encode(); return struct.pack(">H", len(x)) + x
def rl(n):
    o = b""
    while True:
        b = n % 128; n //= 128; o += bytes([b | (128 if n else 0)])
        if not n: return o
def conn(ip):
    s = socket.create_connection((ip, 1883), timeout=10)
    p = s16("MQTT") + bytes([4, 2]) + struct.pack(">H", 120) + s16("s20pub%06d" % random.randrange(10**6))
    s.sendall(b"\x10" + rl(len(p)) + p)
    d = s.recv(4); assert d[:1] == b"\x20" and d[3] == 0, d
    return s

ip, topic, iv, dur = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
end = time.time() + dur; seq = 0; s = None; lastping = time.time(); nxt = time.time()
while time.time() < end:
    try:
        if s is None:
            s = conn(ip); s.settimeout(0.2); print(f"{time.time():.3f} PUBCONN", flush=True)
        while time.time() < nxt:
            try:
                d = s.recv(64)
                if d == b"": raise OSError("FIN")
            except socket.timeout:
                pass
        seq += 1; t = time.time()
        p = s16(topic) + f"P{seq} {t:.3f}".encode()
        s.sendall(b"\x30" + rl(len(p)) + p)
        print(f"{t:.3f} PUB {seq}", flush=True)
        nxt += iv
        if time.time() - lastping > 50:
            s.sendall(b"\xc0\x00"); lastping = time.time()
    except Exception as e:
        print(f"{time.time():.3f} PUBERR {e!r}", flush=True)
        try: s.close()
        except Exception: pass
        s = None; time.sleep(1)
        if nxt < time.time(): nxt = time.time()
try:
    s.sendall(b"\xe0\x00"); s.close()
except Exception: pass
