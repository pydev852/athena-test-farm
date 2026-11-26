"""Turns Claude Code's stream-json output into agent-output.json and prints one public line:
status, turns, cost and token usage. Nothing the agent wrote is printed.

When the run was cut off (no final "result" event), the usage is summed from the assistant
messages so the tokens are still known; the cost is then marked as unknown.
Usage: python3 scripts/usage.py <agent-stream.jsonl> <agent-output.json> <persona>"""
import json, os, re, sys

KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def main(stream, out, persona):
    persona = re.sub(r"[^\w.-]", "", persona) or "agent"
    result, usage, seen, turns, model = None, dict.fromkeys(KEYS, 0), set(), 0, ""
    try:
        lines = open(stream, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            model = msg.get("model") or model
            mid = msg.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            turns += 1
            for k in KEYS:
                v = (msg.get("usage") or {}).get(k)
                if isinstance(v, int):
                    usage[k] += v
    if result is None:
        result = {"type": "result", "subtype": "cut_off", "is_error": True, "num_turns": turns,
                  "usage": usage, "total_cost_usd": None, "result": ""}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f)
    u = result.get("usage") if isinstance(result.get("usage"), dict) else usage
    cost = result.get("total_cost_usd")
    cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "cost unknown (cut off)"
    tok = ", ".join(f"{k.replace('_input_tokens', '').replace('_tokens', '')} {u.get(k, 0)}" for k in KEYS)
    line = (f"{persona}: {result.get('subtype')}, {result.get('num_turns', turns)} turns, {cost_s}, "
            f"tokens: {tok}, model {model or '?'}")
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"Usage {line}\n\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
