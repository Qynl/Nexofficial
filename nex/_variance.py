#!/usr/bin/env python3
"""Run multiple 5-min simulations to check variance."""
import subprocess
import json

SEEDS = [1, 2, 3, 4, 5, 6]
results = []

for seed in SEEDS:
    r = subprocess.run(['node', '_variance.js', str(seed)],
                       capture_output=True, text=True,
                       cwd='/home/user/Nexofficial/nex')
    out = r.stdout.strip()
    if not out:
        print(f"seed {seed}: NO OUTPUT. stderr={r.stderr[:200]}")
        continue
    d = json.loads(out)
    results.append(d)

def fmt(x):
    if isinstance(x, (int, float)): return f"{x:.2f}"
    return str(x)
print()
print(f"{'seed':>4} {'blinkN':>7} {'med':>6} {'lookN':>6} {'med':>6}  {'movN':>5} {'med':>6}  {'tiltN':>5} {'med':>6}  {'rareN':>6}")
for r in results:
    bs = r['blink']
    ls = r['look']
    ms = r.get('movement', {'n': 0})
    ts = r.get('tilt', {'n': 0})
    rs = r.get('rare', {'n': 0})
    print(f"{r['seed']:>4} {bs['n']:>7} {fmt(bs.get('med','-')):>6}  {ls['n']:>6} {fmt(ls.get('med','-')):>6}  {ms.get('n',0):>5} {fmt(ms.get('med','-')):>6}  {ts.get('n',0):>5} {fmt(ts.get('med','-')):>6}  {rs.get('n',0):>6}")
