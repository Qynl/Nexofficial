// Comprehensive QA: render every state, render each idle behavior at peak,
// produce a contact sheet for visual inspection.
const fs = require('fs');
const path = require('path');

const ANIM_SRC = fs.readFileSync(path.join(__dirname, 'animations.js'), 'utf8');
global.window = {};
global.performance = { now: () => Date.now() };
eval('(function(){ var window=global.window,performance=global.performance;\n' + ANIM_SRC + '\n;global.NexAnim = window.NexAnim;})()');

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function smoothstep(e0, e1, x) { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); }
function sdRoundedBox(px, py, bx, by, r) {
  const qx = Math.abs(px) - bx + r, qy = Math.abs(py) - by + r;
  return Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - r;
}
function smin(a, b, k) { const h = clamp(0.5 + 0.5 * (b - a) / k, 0, 1); return a * h + b * (1 - h) - k * h * (1 - h); }

function raster(W, H, params, audio) {
  const p = params, a = audio || { low:0, mid:0, high:0 };
  const aa = 1.5 / Math.min(W, H);
  const aspect = W / H;
  const lookRange = 0.045;
  const lC = [-p.gap + p.lookX * lookRange, p.lookY * lookRange];
  const rC = [ p.gap + p.lookX * lookRange, p.lookY * lookRange];
  const px = new Uint8Array(W * H);
  for (let y = 0; y < H; y++) {
    const vy = (y / H - 0.5) * 2;
    for (let x = 0; x < W; x++) {
      const vx = (x / W - 0.5) * aspect * 2;
      function sde(cx, cy, eye) {
        const c = Math.cos(p.faceTilt), s = Math.sin(p.faceTilt);
        let dx = vx - cx - p.faceShiftX, dy = vy - cy - p.faceShiftY;
        const rx = c*dx - s*dy, ry = s*dx + c*dy;
        dx = rx - eye[0]; dy = ry - eye[1];
        let bw = p.baseW * eye[2], bh = p.baseH * eye[3];
        bw *= 1 + a.low * 0.04 + p.pulse * 0.03;
        bh *= 1 + a.low * 0.02 + p.breath * 0.06 + p.pulse * 0.02;
        const edgePhase = Math.atan2(dy, dx);
        const noise = Math.sin(edgePhase * 3 + 1) * 0.5 + Math.sin(edgePhase * 5 - 1) * 0.5;
        const distort = (p.distortion || 0) * 0.012 + a.mid * 0.010 + (p.listening || 0) * 0.006 + (p.speech || 0) * 0.008 + (p.glitch || 0) * 0.040;
        return sdRoundedBox(dx, dy, bw, bh, p.corner) - noise * distort;
      }
      const dL = sde(lC[0], lC[1], p.eyeLeft);
      const dR = sde(rC[0], rC[1], p.eyeRight);
      const dEyes = smin(dL, dR, 0.010);
      let cover = 1 - smoothstep(-aa, aa, dEyes);
      // Headset prop (only kind 1)
      if (p.prop && p.prop.kind === 1 && p.prop.opacity > 0) {
        const bandR = 0.22 + (p.prop.level || 0) * 0.01, bandThk = 0.012;
        const c = Math.cos(p.faceTilt), s = Math.sin(p.faceTilt);
        let dx = vx, dy = vy;
        const rx = c*dx - s*dy, ry = s*dx + c*dy;
        const dRing = Math.abs(Math.hypot(rx, ry - 0.05) - bandR) - bandThk;
        const mask = ry - (-0.02);
        const arcD = Math.max(dRing, mask);
        const cupW = 0.040, cupH = 0.060;
        const cupGap = p.gap * 0.5 + 0.165;
        const cL = sdRoundedBox(rx - (-cupGap), ry - (-0.02), cupW, cupH, 0.010);
        const cR = sdRoundedBox(rx - ( cupGap), ry - (-0.02), cupW, cupH, 0.010);
        const propD = Math.min(arcD, Math.min(cL, cR));
        const propCover = (1 - smoothstep(-aa, aa, propD)) * p.prop.opacity;
        cover = Math.max(cover, propCover);
      }
      const v = clamp(Math.round(cover * 255 * (p.visibility ?? 1)), 0, 255);
      px[y * W + x] = v;
    }
  }
  return { W, H, px };
}

function toRasterParams(p) {
  return {
    baseW: 0.220, baseH: 0.155, gap: 0.270, corner: 0.085,
    faceTilt: p.faceTilt || 0, faceShiftX: p.faceShiftX || 0, faceShiftY: p.faceShiftY || 0,
    breath: p.breath || 0, pulse: p.pulse || 0,
    lookX: p.lookX || 0, lookY: p.lookY || 0,
    distortion: p.distortion || 0, glitch: p.glitch || 0,
    listening: p.listening || 0, speech: p.speechIntensity || 0,
    visibility: p.visibility ?? 1,
    eyeLeft:  [p.eyeLeft.offsetX, p.eyeLeft.offsetY, p.eyeLeft.scaleX, p.eyeLeft.scaleY],
    eyeRight: [p.eyeRight.offsetX, p.eyeRight.offsetY, p.eyeRight.scaleX, p.eyeRight.scaleY],
    prop: p.prop || { kind: 0, opacity: 0, level: 0 },
  };
}

function writePGM(name, img) {
  const buf = Buffer.concat([Buffer.from('P5\n' + img.W + ' ' + img.H + '\n255\n', 'ascii'), Buffer.from(img.px)]);
  fs.writeFileSync(name, buf);
}

const W = 640, H = 360;

const anim = new global.NexAnim();
anim.start();

// 1. Render rest after wake
for (let i = 0; i < 50; i++) anim.tick(0.02);
writePGM('/tmp/qa_rest.pgm', raster(W, H, toRasterParams(anim.params)));
console.log('rest: written');

// 2. Each named state at mid-cycle
const stateFrames = [
  { state: 'LISTENING',  t: 0.6 },
  { state: 'THINKING',   t: 1.5 },
  { state: 'SPEAKING',   t: 0.8 },
  { state: 'HAPPY',      t: 1.0 },
  { state: 'EXCITED',    t: 0.5 },
  { state: 'CALM',       t: 1.0 },
  { state: 'CONFUSED',   t: 0.6 },
  { state: 'FOCUSED',    t: 1.0 },
  { state: 'FRUSTRATED', t: 0.5 },
  { state: 'SURPRISED',  t: 0.10 },
  { state: 'CURIOUS',    t: 1.0 },
  { state: 'AMUSED',     t: 1.0 },
  { state: 'SLEEPY',     t: 2.0 },
  { state: 'PROUD',      t: 0.20 },
  { state: 'SUSPICIOUS', t: 1.0 },
  { state: 'MUSIC',      t: 1.0 },
  { state: 'ERROR',      t: 0.5 },
];
for (const s of stateFrames) {
  anim.setState({ state: s.state });
  // advance
  for (let i = 0; i < Math.ceil(s.t * 50); i++) anim.tick(0.02);
  writePGM('/tmp/qa_' + s.state.toLowerCase() + '.pgm', raster(W, H, toRasterParams(anim.params)));
  console.log(s.state + ': written');
}

// 3. Each idle behavior at its peak (forced)
// To capture each behavior's peak, we drive each at t=duration/2
const idleNames = ['breathing','blink','lookLeft','lookRight','lookUp','lookDown'];
for (const name of idleNames) {
  anim.setState({ state: 'IDLE' });
  // give wake/idle time to settle
  for (let i = 0; i < 20; i++) anim.tick(0.02);
  const bh = anim.behaviors[name];
  anim.currentBehavior = bh;
  anim.behaviorT = bh.duration / 2;
  anim.behaviorDur = bh.duration;
  anim.behaviorParams = anim._randomParams(bh);
  anim.tick(0.02); // one more to apply
  writePGM('/tmp/qa_idle_' + name.toLowerCase() + '.pgm', raster(W, H, toRasterParams(anim.params)));
  console.log('idle ' + name + ': written');
}

console.log('All renders complete. Convert with:');
console.log('  for f in /tmp/qa_*.pgm; do convert "$f" "${f%.pgm}.png"; done');