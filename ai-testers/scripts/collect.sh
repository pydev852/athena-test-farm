#!/usr/bin/env bash
# Adapted from mobile-agent-testers (MIT). Run on YOUR machine: downloads every encrypted
# results-* artifact of an ai-testers run and decrypts it with the private key (never committed).
# Usage: bash ai-testers/scripts/collect.sh <run_id> <private_key.pem> <out_dir> [owner/repo]
set -euo pipefail
RUN=$1; PRIV=$2; OUT=$3; REPO=${4:-pydev852/athena-test-farm}
rm -rf "$OUT/enc"; mkdir -p "$OUT/enc"
for art in $(gh api "repos/$REPO/actions/runs/$RUN/artifacts" --paginate -q '.artifacts[] | select(.expired | not) | .name | select(startswith("results-"))'); do
  gh run download "$RUN" -R "$REPO" -n "$art" -D "$OUT/enc/$art"
done
for key in "$OUT"/enc/*/*.key.bin; do
  [ -f "$key" ] || { echo "No results to decrypt in run $RUN"; exit 1; }
  name=$(basename "$key" .key.bin); dir=$(dirname "$key")
  pass=$(openssl pkeyutl -decrypt -inkey "$PRIV" -pkeyopt rsa_padding_mode:oaep -in "$key")
  mkdir -p "$OUT/$name"
  openssl enc -d -aes-256-cbc -pbkdf2 -in "$dir/$name.tgz.enc" -pass "pass:$pass" | tar xz -C "$OUT/$name"
done
rm -rf "$OUT/enc"
echo "Decrypted into $OUT"
