// Multi-window long-run simulator: measures behavior distribution at
// 30s / 1m / 3m / 5m / 1h boundaries and prints per-window stats.
const fs = require('fs');
const seed = process.argv[2] || '1';
const SRC = fs.readFileSync('./animations.js', 'utf8')
  .replace('mulberry32(0xC0FFEE)', 'mulberry32(' + seed + ')');
global.window = {};
global.performance = { now: () => Date.now() };
const wrapper = new Function('window', 'performance', SRC + '\n;return window.NexAnim;');
global.NexAnim = wrapper(global.window, global.performance);

const a = new global.NexAnim();
a.start();
const dt = 1/60;
let lastBeh = null;
const starts = []; // {t, name, cat}
const WINDOWS = [30, 60, 180, 300, 600, 1800, 3600];

const results = {};
for (const w of WINDOWS) results[w] = { blinks: [], looks: [], movs: [], tilts: [], rares: [] };

for (let i = 0; i < 3600 * 60 + 60; i++) {
  a.tick(dt);
  if (a.currentBehavior !== lastBeh) {
    if (lastBeh) {
      const startT = a.timeNow - (a.behaviorDur || 0);
      starts.push({ t: startT, name: lastBeh.name, cat: lastBeh.category });
    }
    lastBeh = a.currentBehavior;
  }
}

for (const w of WINDOWS) {
  const ws = starts.filter(s => s.t < w);
  const groups = { BLINK: [], LOOK: [], MOVEMENT: [], TILT: [], RARE: [] };
  for (const s of ws) {
    if (groups[s.cat]) groups[s.cat].push(s.t);
  }
  for (const k in groups) {
    const arr = groups[k];
    const gaps = arr.slice(1).map((t, i) => t - arr[i]).sort((a, b) => a - b);
    results[w][k.toLowerCase() + 's'] = {
      n: arr.length,
      min: gaps[0] !== undefined ? gaps[0] : null,
      med: gaps.length > 0 ? gaps[Math.floor(gaps.length / 2)] : null,
      max: gaps.length > 0 ? gaps[gaps.length - 1] : null,
    };
  }
}

console.log(JSON.stringify({ seed, windows: results }));
