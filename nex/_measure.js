// Visual measurement — generates hi-res render of resting face and reports
// exact dimensions of the eyes. Run after changing webgl.js proportions.

const fs = require('fs');
const path = require('path');

const HERE = __dirname;

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function smoothstep(e0, e1, x) { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); }
function sdRoundedBox(px, py, bx, by, r) {
  const qx = Math.abs(px) - bx + r, qy = Math.abs(py) - by + r;
  return Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - r;
}
function smin(a, b, k) { const h = clamp(0.5 + 0.5 * (b - a) / k, 0, 1); return a * h + b * (1 - h) - k * h * (1 - h); }

function render(W, H, opts) {
  opts = opts || {};
  const baseW = opts.baseW ?? 0.220;
  const baseH = opts.baseH ?? 0.155;
  const gap   = opts.gap   ?? 0.270;
  const corner= opts.corner?? 0.085;
  const aa = 1.5 / Math.min(W, H);
  const aspect = W / H;
  const px = new Uint8Array(W * H);
  for (let y = 0; y < H; y++) {
    const vy = (y / H - 0.5) * 2;
    for (let x = 0; x < W; x++) {
      const vx = (x / W - 0.5) * aspect * 2;
      const lC = [-gap, 0], rC = [gap, 0];
      function sde(cx, cy) {
        let dx = vx - cx, dy = vy - cy;
        const edgePhase = Math.atan2(dy, dx);
        const noise = Math.sin(edgePhase * 3) * 0.5 + Math.sin(edgePhase * 5) * 0.5;
        return sdRoundedBox(dx, dy, baseW, baseH, corner) - noise * 0;
      }
      const dL = sde(lC[0], lC[1]);
      const dR = sde(rC[0], rC[1]);
      const dEyes = smin(dL, dR, 0.010);
      const v = clamp(Math.round((1 - smoothstep(-aa, aa, dEyes)) * 255), 0, 255);
      px[y * W + x] = v;
    }
  }
  return { W, H, px };
}

function writePGM(name, img) {
  const buf = Buffer.concat([Buffer.from('P5\n' + img.W + ' ' + img.H + '\n255\n', 'ascii'), Buffer.from(img.px)]);
  fs.writeFileSync(name, buf);
}

// Generate reference-sized render at 1280x1280
const ref = render(1280, 1280, {});
writePGM('/tmp/nex_ref_rest.pgm', ref);
console.log('ref 1280x1280: white px =', ref.whiteCount, '(' + (100*ref.whiteCount/(ref.W*ref.H)).toFixed(2) + '%)');

// Also 16:9 (browser default)
const wide = render(1280, 720, {});
writePGM('/tmp/nex_wide_rest.pgm', wide);
console.log('wide 1280x720: white px =', wide.whiteCount, '(' + (100*wide.whiteCount/(wide.W*wide.H)).toFixed(2) + '%)');