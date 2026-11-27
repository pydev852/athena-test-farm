#!/system/bin/sh
# usage: samp.sh <outfile> <dur_s> <pkg> <gms_uid> -- device-side sampler (~1-1.5 s per line) in /data/local/tmp/p2f:
# "<epoch> def=<id>:<TRANSPORT>:<V|nv> vpn=<id>:<V|nv>:und[..]:owner<uid> wifi=.. cell=.. gcm=<state>,connects=N,ct=<connectTime> pids=<pid of pkg> tcp5228=[..]"
O=$1; end=$(( $(date +%s) + $2 )); P=$3; G=${4:-0}; cd /data/local/tmp/p2f
while [ $(date +%s) -lt $end ] && [ ! -e stop ]; do
  t=$(date +%s.%N | cut -c1-14); case $t in *N*) t=$(date +%s).000 ;; esac
  n=$(dumpsys connectivity | awk '
    /^Active default network:/ { d = $4 }
    /^  NetworkAgentInfo\{/ {
      id = ""; if (match($0, /network\{[0-9]+\}/)) id = substr($0, RSTART + 8, RLENGTH - 9)
      tr = ""; if (match($0, /Transports: [A-Z|]+/)) tr = substr($0, RSTART + 12, RLENGTH - 12)
      v = ($0 ~ /Capabilities: [A-Z_&]*VALIDATED/) ? "V" : "nv"
      u = ""; if (match($0, /UnderlyingNetworks: (\[[0-9, ]*\]|Null)/)) u = substr($0, RSTART + 20, RLENGTH - 20)
      gsub(/ /, "", u)
      ow = ""; if (match($0, /OwnerUid: [0-9]+/)) ow = substr($0, RSTART + 10, RLENGTH - 10); else if (match($0, /EstablishingAppUid: [0-9]+/)) ow = substr($0, RSTART + 20, RLENGTH - 20)
      st = ""; if (match($0, /ni\{[A-Z_]+ [A-Z_]+/)) st = substr($0, RSTART + 3, RLENGTH - 3); sub(/^[A-Z_]+ /, "", st)
      T[id] = tr; F[id] = v; U[id] = u; S[id] = st; W[id] = ow; ids[++n] = id
    }
    END {
      o = "def=" (d == "" || d == "none" ? "none" : d ":" T[d] ":" F[d])
      vp = "none"; w = "-"; c = "-"
      for (i = 1; i <= n; i++) { k = ids[i]
        if (T[k] ~ /VPN/) { vp = k ":" F[k] ":und" U[k] ":owner" W[k] ":" T[k]; nv++ }
        else if (T[k] ~ /WIFI/) w = (w == "-" ? "" : w ",") k ":" F[k] ":" S[k]
        else if (T[k] ~ /CELLULAR/) c = (c == "-" ? "" : c ",") k ":" F[k] ":" S[k] }
      print o " vpn=" vp " nvpn=" nv + 0 " wifi=" w " cell=" c
    }')
  g=$(dumpsys activity service com.google.android.gms/.gcm.GcmService 2>/dev/null | awk '
    /^ +(connected=|disconnected|connecting|Connection)/ && !c1 { s = $0; sub(/^ +/, "", s); split(s, a, ","); c = a[1]; c1 = 1 }
    /connects=/ && !c2 { if (match($0, /connects=[0-9]+/)) k = substr($0, RSTART, RLENGTH); c2 = 1 }
    /connectTime=/ && !c3 { if (match($0, /connectTime=[^ ]+/)) ct = substr($0, RSTART + 12, RLENGTH - 12); c3 = 1 }
    /Heartbeat: alarm/ && !c4 { s4 = $0; sub(/^ +Heartbeat: /, "", s4); gsub(/ /, "_", s4); hb = s4; c4 = 1 }
    END { gsub(/ /, "_", c); print c "," k ",ct=" ct ",hb=" hb }')
  p=$(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v G=$G '$3 ~ /:146C$/ && $8 == G { printf "%s ", $4 }')
  echo "$t $n gcm=$g pids=$(pidof $P) tcp5228=[$p]" >> $O
  sleep 0.3
done
