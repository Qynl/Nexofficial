// Run a 5-min simulation with patched seed and emit JSON stats.
const fs = require('fs');
const seed = process.argv[process.argv.length - 1];
const SRC = fs.readFileSync('./animations.js', 'utf8')
  .replace('mulberry32(0xC0FFEE)', 'mulberry32(' + seed + ')');
global.window = {};
global.performance = { now: () => Date.now() };
// Use Function constructor so we don't have to escape quotes inside SRC.
const wrapper = new Function('window', 'performance', SRC + '\n;return window.NexAnim;');
global.NexAnim = wrapper(global.window, global.performance);
const a = new global.NexAnim();
a.start();
const dt = 1/60;
let lastBeh = null;
const blinkStarts = [];
const slowBlinkStarts = [];
const lookStarts = [];
const movementStarts = [];
const tiltStarts = [];
const rareStarts = [];
for (let i = 0; i < 18000; i++) {
  a.tick(dt);
  if (a.currentBehavior !== lastBeh) {
    if (lastBeh) {
      const startT = a.timeNow - (a.behaviorDur || 0);
      if (lastBeh.name === 'blink') blinkStarts.push(startT);
      if (lastBeh.name === 'slowBlink') slowBlinkStarts.push(startT);
      if (lastBeh.category === 'LOOK') lookStarts.push(startT);
      if (lastBeh.category === 'MOVEMENT') movementStarts.push(startT);
      if (lastBeh.category === 'TILT') tiltStarts.push(startT);
      if (lastBeh.category === 'RARE') rareStarts.push(startT);
    }
    lastBeh = a.currentBehavior;
  }
}
function stats(arr) {
  if (arr.length < 2) return { n: arr.length };
  const g = arr.slice(1).map((t, i) => t - arr[i]).sort((a, b) => a - b);
  return {
    n: arr.length,
    min: g[0],
    med: g[Math.floor(g.length / 2)],
    max: g[g.length - 1],
    mean: g.reduce((s, x) => s + x, 0) / g.length,
  };
}
console.log(JSON.stringify({
  seed: process.argv[2],
  blink: stats(blinkStarts),
  slowBlink: stats(slowBlinkStarts),
  look: stats(lookStarts),
  movement: stats(movementStarts),
  tilt: stats(tiltStarts),
  rare: stats(rareStarts),
}));
