#!/system/bin/sh
# usage: dnsprobe.sh <outfile> <dur_s> -- every 2 s: DNS lookup + fresh TCP + HTTP HEAD to connectivitycheck.gstatic.com:80
# (toybox nc, 3 s timeout), in the background so a hang does not shift the schedule. "<epoch> DNSPROBE <n> OK|FAIL"
O=$1; end=$(( $(date +%s) + $2 )); cd /data/local/tmp/p2f; k=0
while [ $(date +%s) -lt $end ] && [ ! -e stop ]; do
  k=$((k + 1)); t=$(date +%s.%N | cut -c1-14); case $t in *N*) t=$(date +%s).000 ;; esac
  ( r=$(printf 'HEAD /generate_204 HTTP/1.0\r\nHost: connectivitycheck.gstatic.com\r\n\r\n' | timeout 3 nc -w 3 connectivitycheck.gstatic.com 80 2>&1 | head -1)
    case "$r" in HTTP/*) s=OK ;; *) s=FAIL ;; esac
    echo "$t DNSPROBE $k $s dt=$(echo "$(date +%s.%N | cut -c1-14) $t" | awk '{printf "%.2f", $1-$2}') $(echo "$r" | tr -d '\r' | cut -c1-40)" >> $O ) &
  sleep 2
done
