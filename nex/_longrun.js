// Long-run simulation: run Nex for 5 minutes of simulated time, record
// every behavior choice, and analyze the distribution.

const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(path.join(__dirname, 'animations.js'), 'utf8');
global.window = {};
global.performance = { now: () => Date.now() };
eval('(function(){ var window=global.window,performance=global.performance;\n' + SRC + '\n;global.NexAnim = window.NexAnim;})()');

const a = new global.NexAnim();
a.start();

const dt = 1 / 60; // 60 FPS simulated
const totalSeconds = 300; // 5 minutes
const frames = Math.floor(totalSeconds / dt);

const events = []; // {time, behavior, prevBehavior, gap}
let lastBehaviorEnd = null;
let currentBehaviorName = null;
let currentBehaviorStart = null;

for (let i = 0; i < frames; i++) {
  const before = a.currentBehavior;
  a.tick(dt);
  const after = a.currentBehavior;
  if (before !== after) {
    if (before && currentBehaviorStart != null) {
      // Behavior ended.
      events.push({
        time: a.timeNow,
        behavior: before.name,
        category: before.category,
        duration: a.timeNow - currentBehaviorStart,
      });
      lastBehaviorEnd = a.timeNow;
    }
    if (after) {
      currentBehaviorName = after.name;
      currentBehaviorStart = a.timeNow;
    } else {
      currentBehaviorStart = null;
    }
  }
}

console.log('Total simulated time:', a.timeNow.toFixed(1), 's');
console.log('Total behaviors selected:', events.length);
console.log();

console.log('=== Behavior frequency ===');
const counts = {};
for (const e of events) counts[e.behavior] = (counts[e.behavior] || 0) + 1;
for (const name of Object.keys(counts).sort((a,b) => counts[b] - counts[a])) {
  console.log('  ' + name.padEnd(20), counts[name]);
}

console.log();
console.log('=== Category frequency ===');
const catCounts = {};
for (const e of events) catCounts[e.category] = (catCounts[e.category] || 0) + 1;
for (const name of Object.keys(catCounts).sort((a,b) => catCounts[b] - catCounts[a])) {
  console.log('  ' + name.padEnd(20), catCounts[name]);
}

console.log();
console.log('=== Blink timing analysis ===');
const blinks = events.filter(e => e.category === 'BLINK');
console.log('  total blinks:', blinks.length);
console.log('  avg gap:', (totalSeconds / blinks.length).toFixed(1), 's');
if (blinks.length > 1) {
  const gaps = [];
  for (let i = 1; i < blinks.length; i++) gaps.push(blinks[i].time - blinks[i-1].time);
  gaps.sort((a,b) => a-b);
  const mid = Math.floor(gaps.length / 2);
  console.log('  min gap:', gaps[0].toFixed(1), 's');
  console.log('  median gap:', gaps[mid].toFixed(1), 's');
  console.log('  max gap:', gaps[gaps.length-1].toFixed(1), 's');
}

console.log();
console.log('=== Look timing analysis ===');
const looks = events.filter(e => e.category === 'LOOK');
console.log('  total looks:', looks.length);
console.log('  avg gap:', (totalSeconds / Math.max(1, looks.length)).toFixed(1), 's');
if (looks.length > 1) {
  const gaps = [];
  for (let i = 1; i < looks.length; i++) gaps.push(looks[i].time - looks[i-1].time);
  gaps.sort((a,b) => a-b);
  const mid = Math.floor(gaps.length / 2);
  console.log('  min gap:', gaps[0].toFixed(1), 's');
  console.log('  median gap:', gaps[mid].toFixed(1), 's');
}

console.log();
console.log('=== Rare event timing ===');
const rare = events.filter(e => e.category === 'RARE');
console.log('  total rare events:', rare.length, '(over', totalSeconds, 's)');

console.log();
console.log('=== Behavior chains (gap < 2s between same-category) ===');
let chainCount = 0;
for (let i = 1; i < events.length; i++) {
  // events record END time. Start = time - duration.
  const gap = (events[i].time - events[i].duration) - events[i-1].time;
  if (events[i].category === events[i-1].category && gap < 2.0) {
    chainCount++;
    console.log('  chain:', events[i-1].behavior, '->', events[i].behavior, '(' + gap.toFixed(2) + 's)');
  }
}
console.log('  total chains:', chainCount);

console.log();
console.log('=== Long gaps (>= 5s of stillness) ===');
let longGapCount = 0;
let totalLongGap = 0;
for (let i = 1; i < events.length; i++) {
  const gap = (events[i].time - events[i].duration) - events[i-1].time;
  if (gap >= 5.0) {
    longGapCount++;
    totalLongGap += gap;
  }
}
console.log('  count of gaps >= 5s:', longGapCount);
if (longGapCount) console.log('  avg length:', (totalLongGap / longGapCount).toFixed(1), 's');
console.log('  total long-gap time:', totalLongGap.toFixed(1), 's (', ((totalLongGap/totalSeconds)*100).toFixed(1), '% of total)');

console.log();
console.log('=== First 60 seconds ===');
console.log('time | behavior | duration | gap before');
for (const e of events.filter(e => e.time <= 60)) {
  // events record END time + duration, so start = time - duration.
  const thisStart = e.time - e.duration;
  const prev = events.filter(x => x.time < e.time).slice(-1)[0];
  const gap = prev ? (thisStart - (prev.time - prev.duration)) : 0;
  console.log(e.time.toFixed(1).padStart(5), '|', e.behavior.padEnd(20), '|', e.duration.toFixed(2).padStart(5), '|', gap.toFixed(1));
}