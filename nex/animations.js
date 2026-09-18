/*
 * NEX — animation / behavior engine.
 *
 * Owns:
 *   - The state machine and state priority.
 *   - The idle behavior library + scheduler.
 *   - All multi-phase animations.
 *   - Easing functions.
 *   - Producing a coherent parameter object every frame for WebGL.
 *
 * Does NOT own:
 *   - Audio capture (that lives in app.js).
 *   - Network / SSE (app.js).
 *
 * Public API (window.NexAnim):
 *   const anim = new NexAnim();
 *   anim.start();
 *   anim.setState({ state: 'LISTENING', params: {} });
 *   anim.tick(dtSec);   // returns the parameter object for the renderer
 */
(function () {
  'use strict';

  // -------- helpers --------------------------------------------------------

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const lerp  = (a, b, t)   => a + (b - a) * t;
  const TAU   = Math.PI * 2;

  function smoothstep(edge0, edge1, x) {
    const t = clamp((x - edge0) / (edge1 - edge0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  // Easing library — different shapes give different "personalities".
  const Easing = {
    linear:       t => t,
    inOutCubic:   t => t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t+2, 3)/2,
    outCubic:     t => 1 - Math.pow(1-t, 3),
    inQuad:       t => t*t,
    outQuad:      t => 1 - (1-t)*(1-t),
    inOutQuad:    t => t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2, 2)/2,
    outBack:      t => {
      const c1 = 1.70158, c3 = c1 + 1;
      return 1 + c3 * Math.pow(t-1, 3) + c1 * Math.pow(t-1, 2);
    },
    outElastic:   t => {
      const c = (2*Math.PI)/0.5;
      if (t === 0 || t === 1) return t;
      return Math.pow(2, -8*t) * Math.sin((t*1 - 0.1) * c) + 1;
    },
    outSine:      t => Math.sin((t*Math.PI)/2),
    inOutSine:    t => -(Math.cos(Math.PI*t) - 1) / 2,
  };

  // Small weighted random choice.
  function weightedPick(items, rng) {
    let total = 0;
    for (const it of items) total += it.weight;
    let r = rng() * total;
    for (const it of items) {
      r -= it.weight;
      if (r <= 0) return it.value;
    }
    return items[items.length - 1].value;
  }

  // Mulberry32 — deterministic, fast.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = a;
      t = Math.imul(t ^ t >>> 15, t | 1);
      t ^= t + Math.imul(t ^ t >>> 7, t | 61);
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  // Spring-like interpolation toward a target.
  function springStep(value, target, vel, dt, stiffness, damping) {
    const force = (target - value) * stiffness;
    const newVel = vel * Math.exp(-damping * dt) + force * dt;
    const newVal = value + newVel * dt;
    return [newVal, newVel];
  }

  // -------- Behavior library ----------------------------------------------
  //
  // Each behavior is a self-contained mini-animation: it gets a `ctx` with
  // helpers and an `emit(patch)` it can call to mutate the live state, plus
  // a `t` (0..1) and `dur` (total seconds). Behaviors NEVER call into the
  // WebGL renderer directly; they only update parameter fields.

  function makeBehaviorLibrary(rng) {
    const B = {};

    // Helper: ease a 0..1 phase through a curve.
    const eased = (t, ease) => Easing[ease || 'inOutCubic'](clamp(t, 0, 1));

    // Each behavior carries:
    //   category   — for category-level cooldowns
    //   cooldown   — per-behavior cooldown in seconds
    //   weight     — scheduler weight (used after filtering by activity level)
    //   tier       — VERY_COMMON / COMMON / UNCOMMON / RARE / VERY_RARE
    //                 used to bias selection against activity level

    // 1. BREATHING — both shapes slowly expand/contract.
    B.breathing = {
      name: 'breathing',
      category: 'BREATHE',
      tier: 'VERY_COMMON',
      cooldown: 1.0,
      weight: 0,
      // Breathing is continuous background; not selected through the
      // scheduler's "play one thing" loop. It's a separate modulation.
      continuous: true,
      duration: 4.2 + rng() * 1.6,
      run(ctx) {
        // Subtle, calm breath — too much expansion reads as nervousness.
        const env = 0.7 + 0.3 * Math.sin(ctx.t * TAU * 0.3 + 1.2);
        const v = Math.sin(ctx.t * Math.PI) * 0.30 * env;
        ctx.emit({ breath: v, motionIntensity: Math.abs(v) * 0.08 });
      },
    };

    // 2. BLINK — quick vertical compress + reopen.
    // Total ~180ms: close ~70ms, closed ~30ms, open ~80ms.
    B.blink = {
      name: 'blink',
      category: 'BLINK',
      tier: 'VERY_COMMON',
      cooldown: 1.5 + rng() * 1.0, // 1.5-2.5s min gap
      weight: 12,
      duration: 0.18,
      run(ctx) {
        // Two-phase: 0..0.4 close, 0.4..0.55 hold, 0.55..1 reopen.
        let k;
        if (ctx.t < 0.40) {
          k = ctx.t / 0.40;
        } else if (ctx.t < 0.55) {
          k = 1;
        } else {
          k = 1 - (ctx.t - 0.55) / 0.45;
        }
        const v = Easing.outCubic(k);
        const scaleY = 1 - v * 0.96;
        const scaleX = 1 + v * 0.04;
        ctx.emit({ eyeLeft: { scaleY, scaleX }, eyeRight: { scaleY, scaleX } });
        ctx.emit({ distortion: v * 0.10 });
      },
    };

    // 3. SLOW BLINK — a softer, slower blink (~320ms).
    B.slowBlink = {
      name: 'slowBlink',
      category: 'BLINK',
      tier: 'COMMON',
      cooldown: 6.0,
      weight: 4,
      duration: 0.32,
      run(ctx) {
        const t = ctx.t;
        let k;
        if (t < 0.40) k = t / 0.40;
        else if (t < 0.55) k = 1;
        else k = 1 - (t - 0.55) / 0.45;
        const v = Easing.outCubic(clamp(k, 0, 1));
        const scaleY = 1 - v * 0.92;
        const scaleX = 1 + v * 0.05;
        ctx.emit({ eyeLeft: { scaleY, scaleX }, eyeRight: { scaleY, scaleX }, distortion: v * 0.08 });
      },
    };

    // 4. DOUBLE BLINK — blink, pause, blink again (~720ms total).
    B.doubleBlink = {
      name: 'doubleBlink',
      category: 'BLINK',
      tier: 'UNCOMMON',
      cooldown: 12.0,
      weight: 1.5,
      duration: 0.72,
      run(ctx) {
        const t = ctx.t;
        const b1 = Math.max(0, 1 - Math.abs(t - 0.22) / 0.14);
        const b2 = Math.max(0, 1 - Math.abs(t - 0.60) / 0.14);
        const v = Easing.outCubic(clamp(Math.max(b1, b2), 0, 1));
        const scaleY = 1 - v * 0.94;
        const scaleX = 1 + v * 0.05;
        ctx.emit({ eyeLeft: { scaleY, scaleX }, eyeRight: { scaleY, scaleX } });
      },
    };

    // 5. ASYNCHRONOUS BLINK — wider timing gap so the asymmetry is visible.
    B.asyncBlink = {
      name: 'asyncBlink',
      category: 'BLINK',
      tier: 'UNCOMMON',
      cooldown: 14.0,
      weight: 1.2,
      duration: 0.34,
      run(ctx) {
        const off = (ctx.params.offset || 0.12);
        const t = ctx.t;
        const l = Math.max(0, 1 - Math.abs(t - (0.30 - off)) / 0.16);
        const r = Math.max(0, 1 - Math.abs(t - (0.30 + off)) / 0.16);
        const vl = Easing.outCubic(clamp(l, 0, 1));
        const vr = Easing.outCubic(clamp(r, 0, 1));
        ctx.emit({
          eyeLeft:  { scaleY: 1 - vl * 0.94, scaleX: 1 + vl * 0.05 },
          eyeRight: { scaleY: 1 - vr * 0.94, scaleX: 1 + vr * 0.05 },
        });
      },
    };

    // 6. LOOK LEFT — small, short horizontal gaze. Fires RARELY so the
    // face sits mostly still and a glance actually reads as a glance.
    B.lookLeft = {
      name: 'lookLeft',
      category: 'LOOK',
      direction: 'left',
      tier: 'UNCOMMON',
      cooldown: 35.0 + rng() * 25.0, // 35-60s between same-direction glances
      weight: 0.5,
      duration: 0.6,
      run(ctx) {
        // Tiny gaze offset. Reads as a glance, never leaves the face.
        const target = -0.040;
        const t = ctx.t;
        const v = t < 0.30
          ? Easing.outCubic(t / 0.30)
          : t < 0.55
          ? 1
          : 1 - Easing.inOutCubic((t - 0.55) / 0.45);
        ctx.emit({ lookX: target * v, motionIntensity: v * 0.10 });
      },
    };

    // 7. LOOK RIGHT — mirror of lookLeft.
    B.lookRight = {
      name: 'lookRight',
      category: 'LOOK',
      direction: 'right',
      tier: 'UNCOMMON',
      cooldown: 35.0 + rng() * 25.0,
      weight: 0.5,
      duration: 0.6,
      run(ctx) {
        const target = 0.040;
        const t = ctx.t;
        const v = t < 0.30
          ? Easing.outCubic(t / 0.30)
          : t < 0.55
          ? 1
          : 1 - Easing.inOutCubic((t - 0.55) / 0.45);
        ctx.emit({ lookX: target * v, motionIntensity: v * 0.10 });
      },
    };

    // 8. LOOK UP — small upward gaze. Sparse.
    B.lookUp = {
      name: 'lookUp',
      category: 'LOOK',
      direction: 'up',
      tier: 'UNCOMMON',
      cooldown: 40.0 + rng() * 25.0,
      weight: 0.3,
      duration: 0.6,
      run(ctx) {
        const target = 0.030;
        const t = ctx.t;
        const v = t < 0.30
          ? Easing.outCubic(t / 0.30)
          : t < 0.55
          ? 1
          : 1 - Easing.inOutCubic((t - 0.55) / 0.45);
        ctx.emit({ lookY: target * v });
      },
    };

    // 9. LOOK DOWN — sparse. Was way too frequent before.
    B.lookDown = {
      name: 'lookDown',
      category: 'LOOK',
      tier: 'UNCOMMON',
      cooldown: 40.0 + rng() * 25.0,
      weight: 0.3,
      duration: 0.6,
      run(ctx) {
        const target = -0.030;
        const t = ctx.t;
        const v = t < 0.30
          ? Easing.outCubic(t / 0.30)
          : t < 0.55
          ? 1
          : 1 - Easing.inOutCubic((t - 0.55) / 0.45);
        ctx.emit({ lookY: target * v });
      },
    };

    // 10. STRETCH — small horizontal widening, then back.
    B.stretch = {
      name: 'stretch',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 14.0,
      weight: 1.2,
      duration: 0.9,
      run(ctx) {
        const v = Easing.outQuad(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleX: 1 + v * 0.06, scaleY: 1 - v * 0.02 },
          eyeRight: { scaleX: 1 + v * 0.06, scaleY: 1 - v * 0.02 },
        });
      },
    };

    // 11. SQUISH — small vertical compression.
    B.squish = {
      name: 'squish',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 14.0,
      weight: 1.2,
      duration: 0.9,
      run(ctx) {
        const v = Easing.outQuad(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleY: 1 - v * 0.07, scaleX: 1 + v * 0.02 },
          eyeRight: { scaleY: 1 - v * 0.07, scaleX: 1 + v * 0.02 },
        });
      },
    };

    // 12. MICRO MOVEMENT — extremely small positional drift.
    B.microMove = {
      name: 'microMove',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 10.0,
      weight: 1.0,
      duration: 1.2,
      run(ctx) {
        const dx = ctx.params.dx || 0;
        const dy = ctx.params.dy || 0;
        const v = Math.sin(ctx.t * Math.PI);
        ctx.emit({ faceShiftX: dx * 0.006 * v, faceShiftY: dy * 0.006 * v,
                  motionIntensity: v * 0.06 });
      },
    };

    // 13. FACE TILT — very subtle tilt.
    B.faceTilt = {
      name: 'faceTilt',
      category: 'TILT',
      tier: 'UNCOMMON',
      cooldown: 12.0,
      weight: 0.7,
      duration: 1.0,
      run(ctx) {
        const s = ctx.params.sign || 1;
        const v = Math.sin(ctx.t * Math.PI);
        ctx.emit({ faceTilt: s * 0.022 * v, asymmetry: 0.2 * v * s });
      },
    };

    // 14. ASYMMETRIC MOVEMENT — opposite motion between eyes.
    B.asymmetric = {
      name: 'asymmetric',
      category: 'ASYMMETRY',
      tier: 'UNCOMMON',
      cooldown: 14.0,
      weight: 0.6,
      duration: 1.1,
      run(ctx) {
        const dx = ctx.params.dx || 0.5;
        const dy = ctx.params.dy || 0;
        const v = Math.sin(ctx.t * Math.PI);
        ctx.emit({
          eyeLeft:  { offsetX:  dx * 0.012 * v, offsetY:  dy * 0.006 * v },
          eyeRight: { offsetX: -dx * 0.012 * v, offsetY: -dy * 0.006 * v },
          asymmetry: v,
        });
      },
    };

    // 15. SYNCHRONIZED PULSE — both eyes briefly expand.
    B.syncPulse = {
      name: 'syncPulse',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 8.0,
      weight: 1.0,
      duration: 0.7,
      run(ctx) {
        const v = Easing.outQuad(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleX: 1 + v * 0.05, scaleY: 1 + v * 0.04 },
          eyeRight: { scaleX: 1 + v * 0.05, scaleY: 1 + v * 0.04 },
          pulse: v * 0.5,
        });
      },
    };

    // 16. STILLNESS — explicit quiet period. Counts as a behavior that
    // visibly does nothing.
    B.stillness = {
      name: 'stillness',
      category: 'FREEZE',
      tier: 'COMMON',
      cooldown: 6.0,
      weight: 0,
      duration: 1.6,
      run() { /* deliberately does nothing — a visible pause */ },
    };

    // ---- Secondary "personality" behaviors (calm variety) ----

    B.rarePulse = {
      name: 'rarePulse',
      category: 'RARE',
      tier: 'VERY_RARE',
      cooldown: 20.0,
      weight: 0.15,
      duration: 0.6,
      run(ctx) {
        const v = Easing.outCubic(Math.sin(ctx.t * Math.PI));
        ctx.emit({ pulse: v * 0.5, motionIntensity: v * 0.2 });
      },
    };

    B.slowLook = {
      name: 'slowLook',
      category: 'LOOK',
      direction: 'left',
      tier: 'UNCOMMON',
      cooldown: 30.0,
      weight: 0.4,
      duration: 1.0,
      run(ctx) {
        const target = -0.055;
        const t = ctx.t;
        const v = t < 0.35 ? Easing.outCubic(t / 0.35)
                            : t < 0.6 ? 1
                            : 1 - Easing.inOutCubic((t - 0.6) / 0.4);
        ctx.emit({ lookX: target * v, motionIntensity: v * 0.08 });
      },
    };

    B.briefWiden = {
      name: 'briefWiden',
      category: 'MOVEMENT',
      tier: 'UNCOMMON',
      cooldown: 12.0,
      weight: 0.7,
      duration: 0.5,
      run(ctx) {
        const v = Easing.outQuad(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleX: 1 + v * 0.05 },
          eyeRight: { scaleX: 1 + v * 0.05 },
        });
      },
    };

    B.tinyTilt = {
      name: 'tinyTilt',
      category: 'TILT',
      tier: 'UNCOMMON',
      cooldown: 12.0,
      weight: 0.6,
      duration: 0.8,
      run(ctx) {
        const v = Math.sin(ctx.t * Math.PI);
        ctx.emit({ faceTilt: 0.02 * v * Math.sin(ctx.t * 3.0),
                  asymmetry: 0.15 * v });
      },
    };

    B.oneEyeReact = {
      name: 'oneEyeReact',
      category: 'ASYMMETRY',
      tier: 'UNCOMMON',
      cooldown: 14.0,
      weight: 0.5,
      duration: 0.8,
      run(ctx) {
        const which = ctx.params.which || 'left';
        const v = Math.sin(ctx.t * Math.PI);
        const patch = which === 'left'
          ? { eyeLeft:  { offsetY: -0.010 * v, scaleY: 1 - 0.05 * v } }
          : { eyeRight: { offsetY: -0.010 * v, scaleY: 1 - 0.05 * v } };
        ctx.emit(patch);
        ctx.emit({ asymmetry: v });
      },
    };

    B.freeze = {
      name: 'freeze',
      category: 'FREEZE',
      tier: 'COMMON',
      cooldown: 8.0,
      weight: 0,
      duration: 2.2,
      run() { /* explicit hold */ },
    };

    B.subtleShift = {
      name: 'subtleShift',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 10.0,
      weight: 0.8,
      duration: 1.0,
      run(ctx) {
        const dir = ctx.params.dir || 1;
        const v = Math.sin(ctx.t * Math.PI);
        ctx.emit({ faceShiftX: dir * 0.01 * v, motionIntensity: v * 0.05 });
      },
    };

    B.unusualStretch = {
      name: 'unusualStretch',
      category: 'MOVEMENT',
      tier: 'RARE',
      cooldown: 18.0,
      weight: 0.3,
      duration: 1.2,
      run(ctx) {
        const v = Easing.inOutSine(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleX: 1 + v * 0.08, scaleY: 1 - v * 0.03 },
          eyeRight: { scaleX: 1 + v * 0.08, scaleY: 1 - v * 0.03 },
          pulse: v * 0.4,
        });
      },
    };

    B.syncEvent = {
      name: 'syncEvent',
      category: 'MOVEMENT',
      tier: 'UNCOMMON',
      cooldown: 12.0,
      weight: 0.6,
      duration: 0.6,
      run(ctx) {
        const v = Easing.outBack(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          pulse: v * 0.6,
          eyeLeft:  { scaleX: 1 + v * 0.03 },
          eyeRight: { scaleX: 1 + v * 0.03 },
        });
      },
    };

    // ---- Campaign-era additions: still calm, still rare ----------------

    // DOUBLE BLINK — a quick pair of blinks. Reads as mild interest.
    B.doubleBlink = {
      name: 'doubleBlink',
      category: 'BLINK',
      tier: 'UNCOMMON',
      cooldown: 14.0,
      weight: 0.5,
      duration: 0.42,
      run(ctx) {
        // Two closes: peaks at t=0.22 and t=0.72 of the timeline.
        const close1 = Math.exp(-Math.pow((ctx.t - 0.22) / 0.07, 2));
        const close2 = Math.exp(-Math.pow((ctx.t - 0.72) / 0.08, 2));
        const k = clamp(close1 + close2, 0, 1);
        ctx.emit({ eyeLeft: { scaleY: 1 - k * 0.92 }, eyeRight: { scaleY: 1 - k * 0.92 } });
      },
    };

    // CONTENT SQUINT — brief happy narrowing (like a small smile for eyes).
    B.contentSquint = {
      name: 'contentSquint',
      category: 'RARE',
      tier: 'VERY_RARE',
      cooldown: 26.0,
      weight: 0.35,
      duration: 1.1,
      run(ctx) {
        const env = Easing.outCubic(Math.sin(ctx.t * Math.PI));
        ctx.emit({
          eyeLeft:  { scaleY: 1 - env * 0.18, offsetY: env * 0.004 },
          eyeRight: { scaleY: 1 - env * 0.18, offsetY: env * 0.004 },
          asymmetry: env * 0.08,
        });
      },
    };

    // DRIFT GAZE — a slow, unhurried wander of attention (never a snap).
    B.driftGaze = {
      name: 'driftGaze',
      category: 'LOOK',
      tier: 'UNCOMMON',
      cooldown: 16.0,
      weight: 0.7,
      duration: 3.2,
      run(ctx) {
        const a = ctx.t;
        const ease = a < 0.25 ? Easing.inOutQuad(a / 0.25)
                   : a > 0.75 ? 1 - Easing.inOutQuad((a - 0.75) / 0.25)
                   : 1;
        const x = Math.sin(a * Math.PI * 2 * 0.5) * 0.3;
        const y = Math.sin(a * Math.PI + 0.7) * 0.10;
        ctx.emit({ lookX: x * ease, lookY: y * ease });
      },
    };

    // SETTLE — a tiny drop and recover, like getting comfortable.
    B.settle = {
      name: 'settle',
      category: 'MOVEMENT',
      tier: 'COMMON',
      cooldown: 18.0,
      weight: 0.5,
      duration: 0.9,
      run(ctx) {
        const drop = Math.sin(ctx.t * Math.PI);
        ctx.emit({
          faceShiftY: -drop * 0.006,
          eyeLeft:  { scaleY: 1 - drop * 0.05 },
          eyeRight: { scaleY: 1 - drop * 0.05 },
        });
      },
    };

    return B;

  }

  // -------- The engine ----------------------------------------------------

  // Priority order (lowest first; higher overrides lower).
  const STATE_PRIORITY = {
    IDLE: 0, MUSIC: 1, THINKING: 2, LISTENING: 3, SPEAKING: 3, HAPPY: 3, EXCITED: 3, CONFUSED: 3, FOCUSED: 3, FRUSTRATED: 3, SURPRISED: 3, CURIOUS: 3, AMUSED: 3, SLEEPY: 3, PROUD: 3, SUSPICIOUS: 3, SCAN: 3, ERROR: 5, RECOVERY: 4,
  };

  function hexToRgb(hex) {
    const h = String(hex || '').replace('#', '');
    const n = parseInt(h.length === 3
      ? h.split('').map(c => c + c).join('') : h, 16);
    return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255,
             b: (n & 255) / 255 };
  }

  class NexAnim {
    constructor() {
      this.rng = mulberry32(0xC0FFEE);
      this.behaviors = makeBehaviorLibrary(this.rng);
      this.state = 'IDLE';
      this.stateEnterT = 0;
      this.stateParams = {};
      this.statePhase = 'enter'; // multi-phase states progress: enter -> active -> exit

      // Idle scheduler state.
      this.cooldownUntil = 0;       // global minimum gap after a behavior
      this._nextEvaluationAt = 0;   // when the scheduler is allowed to think again
      this._activityLevel = 'NORMAL';
      this._perBehaviorCooldown = {};  // name -> time when cooldown expires
      this._categoryCooldown = {};     // category -> time when cooldown expires
      this._lastBehavior = null;
      this._lastCategory = null;
      this._lastBehaviorTime = 0;
      this._recentBehaviors = [];
      this.currentBehavior = null;
      this.behaviorT = 0;
      this.behaviorDur = 0;
      this.timeNow = 0;

      // Live parameter object (consumed by the renderer each frame).
      this.params = this._freshParams();

      // Transitions are short tweens; this holds their progress.
      this.transition = null;

      // Music mode.
      this.musicProp = null;
      // Speech bubble text (used by app.js).
      this.speechText = '';

      // Prop library (kind -> display info).
      this.propKinds = {
        HEADSET: 1, MIC: 2, CONTROLLER: 3, MAGNIFIER: 4, CODE: 5, BELL: 6,
      };

      // Temporary prop state (the prop can fade in/out independently).
      this.currentProp = { kind: 0, opacity: 0, level: 0 };
      this.propTarget = { kind: 0, opacity: 0 };

      // Audio smoothing.
      this.audio = { low: 0, mid: 0, high: 0 };

      // Wake sequence.
      this.wakeActive = false;
      this.wakeStart = 0;

      // One-shot event effects (celebration burst, light sweep, verify
      // scan, accent flash). Each: { kind, t, dur, color, amt }.
      this.effects = [];
      // Pointer gaze target (set by app.js; blended into IDLE only).
      this.gaze = { x: 0, y: 0, tx: 0, ty: 0 };
      this._nextSaccadeAt = 0;
    }

    // ---------- one-shot event effects (public API) ------------------------

    // Success flourish: radial burst + brief teal-green accent lift.
    celebrate(dur) {
      this.effects.push({ kind: 'burst', t: 0, dur: dur || 1.1 });
      this.flash('#7be0a0', 0.5, 1.2);
    }

    // Trouble flicker: structured glitch bands + ember accent.
    trouble(dur) {
      this.effects.push({ kind: 'trouble', t: 0, dur: dur || 0.8 });
      this.flash('#ff8a6b', 0.45, 0.8);
    }

    // Working sweep: light band across the eyes (plans / execution).
    sweepOnce(dur) {
      this.effects.push({ kind: 'sweep', t: 0, dur: dur || 1.6 });
    }

    // Continuous scanline while VERIFYING (driven by app.js state map).
    scanOnce(dur) {
      this.effects.push({ kind: 'scan', t: 0, dur: dur || 1.4 });
    }

    // Accent flash: tint + halo lift that attacks fast, decays calm.
    flash(colorHex, amt, dur) {
      const c = hexToRgb(colorHex || '#7be0a0');
      this.effects.push({ kind: 'flash', t: 0, dur: dur || 1.0,
                          color: c, amt: (amt == null ? 0.45 : amt) });
    }

    // Pointer gaze (normalized -1..1). Blended gently into IDLE only.
    setGaze(x, y) {
      this.gaze.tx = clamp(x, -1, 1);
      this.gaze.ty = clamp(y, -1, 1);
    }

    _applyEffects(dt) {
      const p = this.params;
      for (let i = this.effects.length - 1; i >= 0; i--) {
        const e = this.effects[i];
        e.t += dt;
        const a = clamp(e.t / e.dur, 0, 1);
        if (typeof e.run === 'function') e.run(a);
        if (e.kind === 'burst') {
          p.burst = a;
        } else if (e.kind === 'sweep') {
          p.sweep = a;
        } else if (e.kind === 'scan') {
          p.scan = a;
        } else if (e.kind === 'flash') {
          // Fast attack (~15%), exponential decay.
          const env = a < 0.15 ? (a / 0.15) : Math.pow(1 - (a - 0.15) / 0.85, 1.6);
          p.accent.r = e.color.r; p.accent.g = e.color.g; p.accent.b = e.color.b;
          p.accentAmt = Math.max(p.accentAmt, e.amt * env);
        } else if (e.kind === 'trouble') {
          const env = Math.sin(a * Math.PI);
          p.glitch = Math.max(p.glitch, env * 0.5);
        }
        if (a >= 1) {
          this.effects.splice(i, 1);
          if (e.kind === 'burst') p.burst = 0;
          if (e.kind === 'sweep') p.sweep = -1;
          if (e.kind === 'scan') p.scan = -1;
        }
      }
      // When no flash is active, drift the accent back to neutral.
      if (!this.effects.some(e => e.kind === 'flash')) {
        p.accentAmt = Math.max(0, p.accentAmt - dt * 0.8);
        if (p.accentAmt <= 0.001) {
          p.accentAmt = 0;
          p.accent.r = 1; p.accent.g = 1; p.accent.b = 1;
        }
      }
    }

    // Micro-saccade: a tiny gaze flick — the eyes dart a few degrees and
    // glide back. Runs OUTSIDE the behavior scheduler (it's reflexive, not
    // a behavior), so it composes with breathing/blink.
    _maybeSaccade() {
      if (this.state !== 'IDLE' || this.currentBehavior) return;
      if (this.timeNow < this._nextSaccadeAt) return;
      this._nextSaccadeAt = this.timeNow + 2.5 + this.rng() * 3.5;
      const sx = (this.rng() * 2 - 1) * 0.16;
      const sy = (this.rng() * 2 - 1) * 0.07;
      const self = this;
      this.effects.push({
        kind: 'saccade', t: 0, dur: 0.34,
        run(a) {
          // dart out (0..0.3), hold (0.3..0.55), glide home (0.55..1).
          let k;
          if (a < 0.3) k = Easing.outQuad(a / 0.3);
          else if (a < 0.55) k = 1;
          else k = 1 - Easing.inOutQuad((a - 0.55) / 0.45);
          self.params.lookX = sx * k + self.gaze.x * 0.06;
          self.params.lookY = sy * k + self.gaze.y * 0.04;
        },
      });
    }

    _freshParams() {
      return {
        visibility: 0, // becomes 1 after WAKE
        breath: 0,
        pulse: 0,
        lookX: 0,
        lookY: 0,
        asymmetry: 0,
        distortion: 0,
        motionIntensity: 0,
        speechIntensity: 0,
        listening: 0,
        music: 0,
        error: 0,
        glitch: 0,
        faceTilt: 0,
        faceShiftX: 0,
        faceShiftY: 0,
        eyeLeft:  { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 },
        eyeRight: { offsetX: 0, offsetY: 0, scaleX: 1, scaleY: 1 },
        prop: { kind: 0, opacity: 0, level: 0 },
        // Atmosphere / event FX (consumed by the WebGL shader).
        accent: { r: 1, g: 1, b: 1 },
        accentAmt: 0,
        burst: 0,
        sweep: -1,   // <0 = off, else 0..1 position
        scan: -1,    // <0 = off, else 0..1 position
        dust: 1,
        iris: 0,     // 0..1 inner-ring detail
      };
    }

    start() {
      this.stateEnterT = this.timeNow;
      this._beginWake();
    }

    _resetIdleScheduler() {
      // Reset the scheduler for a clean entry into idle. Give the face a
      // short quiet period before the next behavior is even considered.
      this.currentBehavior = null;
      this.behaviorT = 0;
      this._lastBehavior = null;
      this._lastCategory = null;
      this._recentBehaviors = [];
      this._activityLevel = 'NORMAL';
      this._lastLookDirection = null;
      this._lastLookAt = 0;
      // Pause for 1.5-3s before first scheduler evaluation after a state.
      this._nextEvaluationAt = this.timeNow + 1.5 + this.rng() * 1.5;
      this.cooldownUntil = this.timeNow + 1.0 + this.rng() * 1.0;
      // Independent blink scheduler — runs alongside the main scheduler so
      // blink cadence is not at the mercy of the main RNG pool.
      // Target ~5s median blink, calm and natural.
      this._nextBlinkAt = this.timeNow + 4.0 + this.rng() * 2.0;
    }

    // ---------- public state setter ---------------------------------------

    setState(req) {
      const name = (req.state || 'IDLE').toUpperCase();
      const params = req.params || {};
      const oldState = this.state;
      this.state = name;
      this.stateParams = params;
      this.stateEnterT = this.timeNow;
      this.statePhase = 'enter';

      // Reset the live parameter object so the new state's emissives
      // dominate. We never abruptly snap during render — the per-frame
      // logic re-targets values and tweens toward them.
      this.params = this._freshParams();
      this.params.visibility = this.wakeActive ? 0 : 1;

      // Prop handling per state.
      if (name === 'MUSIC') {
        this._setProp(this.propKinds.HEADSET, 1, 1);
      } else if (name === 'SPEAKING') {
        // No permanent prop; the bubble UI handles speech text.
      } else if (name === 'ERROR') {
        this._setProp(0, 0);
      } else if (name === 'IDLE' || name === 'RECOVERY') {
        this._setProp(0, 0);
      }

      // Pause the idle scheduler whenever we leave IDLE. Resume cleanly
      // when we come back. This prevents idle behaviors from firing
      // underneath state animations.
      if (name !== 'IDLE' && name !== 'WAKE') {
        this.currentBehavior = null;
        this.behaviorT = 0;
        this._nextEvaluationAt = Infinity;
      }
      if (name === 'IDLE' && oldState !== 'IDLE') {
        this._resetIdleScheduler();
      }

      // Build a short transition into the new state.
      this._beginTransition(oldState, name);
    }

    setProp(prop) {
      if (!prop) { this._setProp(0, 0); return; }
      const map = { HEADSET:1, MIC:2, CONTROLLER:3, MAGNIFIER:4, CODE:5, BELL:6 };
      const k = map[(prop.kind || '').toUpperCase()] || 0;
      this._setProp(k, prop.opacity ?? 1, prop.level ?? 0);
    }

    _setProp(kind, opacity, level) {
      this.propTarget = { kind, opacity, level: level ?? 0 };
    }

    setAudio(audio) {
      // audio: { low, mid, high } in 0..1.
      this.audio = audio;
    }

    setSpeechText(text) { this.speechText = text || ''; }

    // ---------- transitions ------------------------------------------------

    _beginTransition(from, to) {
      const dur = 0.45;
      this.transition = {
        from, to,
        t: 0, dur,
        // baseline target values per state; sub-animations override each frame.
      };
    }

    _beginWake() {
      this.wakeActive = true;
      this.wakeStart = this.timeNow;
      this.state = 'WAKE';
      this.stateEnterT = this.wakeStart;
      this.statePhase = 'enter';
      this.params.visibility = 0;
      this.params.dust = 2.4;   // dust flare on boot, settles to 1
      this.currentBehavior = null;
    }

    // ---------- scheduler --------------------------------------------------
    //
    // The idle scheduler is built around:
    //
    //   * Activity level (LOW / NORMAL / HIGH) — drifts gradually.
    //   * Per-behavior cooldown — each behavior has its own minimum gap.
    //   * Category cooldown — same category can't repeat too quickly.
    //   * Minimum visible-behavior gap — prevents chaining.
    //   * "NOTHING" is a valid outcome — the scheduler can decide not
    //     to play anything for several seconds.
    //   * Randomized evaluation interval (2-5s) so the user can't sense
    //     a metronome.
    //   * History-based de-prioritization: don't repeat the same name
    //     twice in a row; bias against same-category repeats.

    _scheduleNext() {
      const now = this.timeNow;
      if (now < this.cooldownUntil) return;       // global minimum gap
      if (now < this._nextEvaluationAt) return;   // not yet time to think

      // Time to evaluate. Roll the activity-level nudge.
      this._nudgeActivityLevel();

      // Decide between: NOTHING (most common), a visible behavior, or a
      // freeze (explicit quiet period).
      const nothingChance = this._nothingChance();
      if (this.rng() < nothingChance) {
        // Schedule the next evaluation 2-5s out.
        this._nextEvaluationAt = now + 2.0 + this.rng() * 3.0;
        return;
      }

      // Eligible pool: filter out behaviors still on cooldown and on
      // category cooldown, then weight by activity level.
      const eligible = this._buildEligiblePool(now);
      if (eligible.length === 0) {
        this._nextEvaluationAt = now + 1.0 + this.rng() * 2.0;
        return;
      }
      // Spec: a glance should feel like a single event, never chained.
      // Only commit a glance if we haven't had one in the last 18s.
      // (Per-behavior cooldown already prevents same-direction; this is
      // a category-wide gap so left -> right -> left can't alternate.)
      const isAnyLook = (b) => b.category === 'LOOK';
      const anyLookEligible = eligible.some(e => isAnyLook(e.value));
      const looksSinceLast = (this._lastBehavior && isAnyLook(this._lastBehavior))
        ? (now - (this._lastBehaviorTime || 0))
        : 1e9;
      // If the eligible pool contains a look, only let it through if 18s
      // have passed since the last look. (Cat cooldown handles LOOK too,
      // but we want a bigger gap than the per-category default.)
      if (anyLookEligible && looksSinceLast < 18.0) {
        // Strip looks from the eligible pool, then try again.
        const nonLooks = eligible.filter(e => !isAnyLook(e.value));
        if (nonLooks.length === 0) {
          this._nextEvaluationAt = now + 1.0 + this.rng() * 2.0;
          return;
        }
        const choice = weightedPick(nonLooks, this.rng);
        this._commitBehavior(choice, now);
        return;
      }
      const choice = weightedPick(eligible, this.rng);
      this._commitBehavior(choice, now);
    }

    _nothingChance() {
      // The face should spend MOST of its idle time literally still.
      // Spec: 5s of pure rest between blinks, sparse glances, almost
      // no other motion. Even at HIGH activity, NOTHING dominates.
      switch (this._activityLevel) {
        case 'LOW':    return 0.95;
        case 'HIGH':   return 0.75;
        case 'NORMAL':
        default:       return 0.88;
      }
    }

    _nudgeActivityLevel() {
      // The default mood is NORMAL/LOW. HIGH is rare. The level drifts
      // slowly so the user perceives a mood rather than a flickering
      // decision.
      const r = this.rng();
      if (r < 0.02) this._activityLevel = 'HIGH';
      else if (r < 0.45) this._activityLevel = 'LOW';
      else if (r < 0.55 && this._activityLevel !== 'NORMAL') this._activityLevel = 'NORMAL';
    }

    _buildEligiblePool(now) {
      const eligible = [];
      for (const name in this.behaviors) {
        const b = this.behaviors[name];
        if (b.continuous) continue;            // not selectable here
        if (name === 'blink') continue;        // blink runs on its own scheduler
        // Direction-repetition guard: skip the look direction we just used
        // so the eyes don't ping-pong between the same two directions.
        if (this._lastLookDirection && b.category === 'LOOK' &&
            b.direction === this._lastLookDirection &&
            now < (this._lastLookAt + 8.0)) continue;
        const perCD = this._perBehaviorCooldown[name] || 0;
        if (now < perCD) continue;
        const catCD = this._categoryCooldown[b.category] || 0;
        if (now < catCD) continue;
        // Don't repeat the exact same behavior twice in a row.
        if (b === this._lastBehavior) continue;
        // Bias weights by activity level.
        const w = b.weight * this._tierMultiplier(b.tier);
        if (w <= 0) continue;
        eligible.push({ value: b, weight: w });
      }
      return eligible;
    }

    _tierMultiplier(tier) {
      switch (this._activityLevel) {
        case 'LOW':
          // Only common / very common behaviors really fire.
          if (tier === 'VERY_COMMON') return 1.0;
          if (tier === 'COMMON') return 0.5;
          if (tier === 'UNCOMMON') return 0.12;
          if (tier === 'RARE') return 0.02;
          if (tier === 'VERY_RARE') return 0;
          return 0;
        case 'HIGH':
          if (tier === 'VERY_COMMON') return 1.0;
          if (tier === 'COMMON') return 1.2;
          if (tier === 'UNCOMMON') return 1.5;
          if (tier === 'RARE') return 1.4;
          if (tier === 'VERY_RARE') return 0.8;
          return 1.0;
        case 'NORMAL':
        default:
          if (tier === 'VERY_COMMON') return 1.0;
          if (tier === 'COMMON') return 0.8;
          if (tier === 'UNCOMMON') return 0.6;
          if (tier === 'RARE') return 0.6;
          if (tier === 'VERY_RARE') return 0.25;
          return 0.5;
      }
    }

    _commitBehavior(choice, now) {
      this.currentBehavior = choice;
      if (choice.category === 'LOOK' && choice.direction) {
        this._lastLookDirection = choice.direction;
        this._lastLookAt = now;
      }
      this.behaviorT = 0;
      this.behaviorDur = choice.duration;
      this.behaviorParams = this._randomParams(choice);
      this.behaviorStartT = now;
      // Per-behavior cooldown (relative to when the behavior ends, not starts).
      this._perBehaviorCooldown[choice.name] =
        now + choice.duration + (choice.cooldown || 4.0);
      // Category cooldown (so two LOOKs don't run back to back).
      this._categoryCooldown[choice.category] =
        now + choice.duration + this._categoryGapFor(choice.category);
      // Track history.
      this._lastBehavior = choice;
      this._lastCategory = choice.category;
      this._lastBehaviorTime = now;
      this._recentBehaviors.unshift(choice.name);
      if (this._recentBehaviors.length > 8) this._recentBehaviors.length = 8;
      // Next scheduler evaluation should happen AFTER the behavior ends
      // and after a small breathing-room gap. This prevents back-to-back
      // visible behaviors.
      const endAt = now + choice.duration;
      this._nextEvaluationAt = endAt + 1.5 + this.rng() * 2.5;
      // Global minimum gap after the behavior ends (used as a hard floor).
      this.cooldownUntil = endAt;
    }

    _categoryGapFor(category) {
      // Discourage same-category chaining. LOOK gets the biggest gap
      // because every glance is a noticeable event — the user wants
      // mostly-still idle with sparse glances.
      switch (category) {
        case 'BLINK':      return 2.0;   // blinks repeat naturally, but not chain
        case 'LOOK':       return 18.0;  // glances: discrete events, big gap
        case 'MOVEMENT':   return 10.0;
        case 'TILT':       return 10.0;
        case 'ASYMMETRY':  return 15.0;
        case 'RARE':       return 18.0;
        case 'FREEZE':     return 8.0;
        case 'BREATHE':    return 0;
        default:           return 4.0;
      }
    }

    _randomParams(b) {
      const p = {};
      if (b === this.behaviors.asyncBlink)      p.offset = 0.12 + this.rng() * 0.12;
      if (b === this.behaviors.microMove)       { p.dx = this.rng()*2-1; p.dy = this.rng()*2-1; }
      if (b === this.behaviors.faceTilt)        p.sign = this.rng() < 0.5 ? -1 : 1;
      if (b === this.behaviors.asymmetric)      { p.dx = this.rng()*2-1; p.dy = this.rng()*2-1; }
      if (b === this.behaviors.oneEyeReact)     p.which = this.rng() < 0.5 ? 'left' : 'right';
      if (b === this.behaviors.subtleShift)     p.dir   = this.rng() < 0.5 ? -1 : 1;
      return p;
    }

    // For tests / debug telemetry.
    _pickBehavior() {
      const now = this.timeNow;
      const eligible = this._buildEligiblePool(now);
      if (eligible.length === 0) return null;
      return weightedPick(eligible, this.rng);
    }
    _recent = null;

    // ---------- state behaviors (multi-phase) -----------------------------

    _applyStateBehavior(dt) {
      const t = this.timeNow - this.stateEnterT;
      const p = this.stateParams;
      switch (this.state) {
        case 'WAKE':       return this._stateWake(t);
        case 'LISTENING':  return this._stateListening(t, dt);
        case 'THINKING':   return this._stateThinking(t, dt);
        case 'SPEAKING':   return this._stateSpeaking(t, dt);
        case 'HAPPY':      return this._stateHappy(t, dt);
        case 'EXCITED':    return this._stateExcited(t, dt);
        case 'CALM':       return this._stateCalm(t, dt);
        case 'CONFUSED':   return this._stateConfused(t, dt);
        case 'FOCUSED':    return this._stateFocused(t, dt);
        case 'FRUSTRATED': return this._stateFrustrated(t, dt);
        case 'SURPRISED':  return this._stateSurprised(t, dt);
        case 'CURIOUS':    return this._stateCurious(t, dt);
        case 'AMUSED':     return this._stateAmused(t, dt);
        case 'SLEEPY':     return this._stateSleepy(t, dt);
        case 'PROUD':      return this._stateProud(t, dt);
        case 'SUSPICIOUS': return this._stateSuspicious(t, dt);
        case 'SCAN':       return this._stateScan(t, dt);
        case 'MUSIC':      return this._stateMusic(t, dt);
        case 'ERROR':      return this._stateError(t, dt);
        case 'RECOVERY':   return this._stateRecovery(t, dt);
        case 'IDLE':
        default:
          return this._stateIdle(dt);
      }
    }

    _stateWake(t) {
      // Sequence:
      //   0.00..0.45  visibility rises (points -> shapes expand)
      //   0.45..0.65  small synchronized pulse
      //   0.65..      transition to IDLE
      const p = this.params;
      const a = clamp(t / 0.45, 0, 1);
      p.visibility = Easing.outCubic(a);
      p.dust = 1 + 1.4 * (1 - Easing.outCubic(clamp(t / 1.6, 0, 1)));
      if (t > 0.45) {
        const tp = clamp((t - 0.45) / 0.20, 0, 1);
        const v = Math.sin(tp * Math.PI);
        const k = Easing.outCubic(v);
        p.pulse = k;
        p.eyeLeft.scaleX  = 1 + k * 0.05;
        p.eyeLeft.scaleY  = 1 + k * 0.05;
        p.eyeRight.scaleX = 1 + k * 0.05;
        p.eyeRight.scaleY = 1 + k * 0.05;
      }
      if (t > 0.65 && !this._wakeScheduled) {
        this._wakeScheduled = true;
        this.wakeActive = false;
        // Reset the scheduler so the face sits quietly for a moment
        // before any idle behavior is considered.
        this.setState({ state: 'IDLE' });
        this._resetIdleScheduler();
      }
    }

    _stateIdle(dt) {
      // Idle scheduler. We always tick the current behavior if any. When
      // it finishes, we run the scheduler evaluate-and-maybe-play logic.
      if (this.currentBehavior) {
        this.behaviorT += dt;
        if (this.behaviorT >= this.behaviorDur) {
          this.currentBehavior = null;
          this.behaviorT = 0;
          // The scheduler's _nextEvaluationAt (set at commit time) already
          // accounts for the post-behavior quiet gap; no need to add another.
        } else {
          const ctx = {
            t: clamp(this.behaviorT / this.behaviorDur, 0, 1),
            dur: this.behaviorDur,
            params: this.behaviorParams || {},
            emit: (patch) => this._mergePatch(patch),
          };
          try { this.currentBehavior.run(ctx); }
          catch (e) { console.error(e); }
        }
      } else {
        // Blink scheduler fires first if it's time and no blink is
        // currently in progress.
        if (this.timeNow >= this._nextBlinkAt) {
          const blink = this.behaviors.blink;
          if (blink) {
            this._commitBehavior(blink, this.timeNow);
            // Schedule next blink 4-6s out (target ~5s median).
            this._nextBlinkAt = this.timeNow + 4.0 + this.rng() * 2.0;
            return;
          }
        }
        this._scheduleNext();
      }
      // Background breathing modulation — adds life without occupying
      // a scheduler slot.
      if (!this.currentBehavior) {
        const b = this.behaviors.breathing;
        if (b) {
          const tBreath = (this.timeNow % b.duration) / b.duration;
          try { b.run({ t: tBreath, dur: b.duration, params: {}, emit: (p) => this._mergePatch(p) }); }
          catch (e) { /* ignore */ }
        }
      }
      this._decayTransient();
    }

    _stateListening(t, dt) {
      const p = this.params;
      // Subtle attention pose: small horizontal/vertical look toward the
      // perceived mic direction. The audio reactivity is applied below
      // via _applyAudioLayer.
      const breathe = Math.sin(t * 1.4) * 0.4;
      p.breath = breathe;
      p.listening = clamp(1.0 - t * 0.8, 0.6, 1.0);
      p.iris = 0.8;
      p.motionIntensity = 0.25 + 0.15 * Math.sin(t * 2.3);
      // Gentle horizontal shift
      p.faceShiftX = 0.004 * Math.sin(t * 1.1);
      // Small eye widening
      p.eyeLeft.scaleX  = 1 + 0.04 * (0.5 + 0.5 * Math.sin(t * 2.0));
      p.eyeRight.scaleX = 1 + 0.04 * (0.5 + 0.5 * Math.sin(t * 2.0));
      this._decayTransient();
    }

    _stateThinking(t, dt) {
      const p = this.params;
      // Multi-phase: enter -> active -> hesitation -> active -> exit
      const phaseDur = 2.5;
      const phase = Math.floor(t / phaseDur) % 3;
      const local = (t % phaseDur) / phaseDur;

      if (phase === 0) {
        // enter: brief sync pulse
        const v = Math.sin(local * Math.PI);
        const k = Easing.outCubic(v);
        p.pulse = k;
        p.eyeLeft.scaleX = p.eyeRight.scaleX = 1 + k * 0.06;
      } else if (phase === 1) {
        // active: small horizontal eye movements + slight independent motion
        const a = Math.sin(local * TAU + 1.2);
        const b = Math.cos(local * TAU * 0.7 + 0.3);
        p.lookX = 0.4 * a;
        p.eyeLeft.offsetX  = 0.008 * b;
        p.eyeRight.offsetX = 0.008 * Math.sin(local * TAU * 0.9 + 1.0);
        p.motionIntensity = 0.35 + 0.1 * Math.sin(local * TAU * 2);
      } else {
        // hesitation: slow desync, brief pause, then snap back
        const k = Math.sin(local * Math.PI);
        p.eyeLeft.scaleY  = 1 - 0.12 * k * 0.6;
        p.eyeRight.scaleY = 1 - 0.12 * (k * 0.6 + 0.2 * (1-k));
        p.asymmetry = 0.4 * k;
        p.distortion = 0.15 * k;
        if (local > 0.7) {
          p.pulse = 0.5 * (local - 0.7) / 0.3;
        }
      }
      this._decayTransient();
    }

    _stateSpeaking(t, dt) {
      const p = this.params;
      p.iris = 0.45 + 0.15 * Math.sin(t * 5.0);
      // Speech intensity rises quickly then settles.
      p.speechIntensity = clamp(0.4 + 0.6 * (1 - Math.exp(-t * 1.5)), 0.4, 1.0);

      // Subtle micro-movements that hint at articulation without bouncing.
      const micro = Math.sin(t * 7.1 + 0.4) * 0.5 + Math.cos(t * 5.3) * 0.5;
      const open  = (Math.sin(t * 3.7) * 0.5 + 0.5);
      const sy    = 0.96 + 0.10 * open + 0.04 * micro;
      const sx    = 1.02 - 0.04 * Math.sin(t * 4.3);

      p.eyeLeft.scaleY  = sy;
      p.eyeRight.scaleY = sy;
      p.eyeLeft.scaleX  = sx;
      p.eyeRight.scaleX = sx;
      p.distortion = 0.20 + 0.10 * Math.sin(t * 6.1);
      p.motionIntensity = 0.4 + 0.2 * Math.sin(t * 5.0);
      // Occasional stronger reaction
      if (Math.sin(t * 1.7) > 0.7) p.pulse = 0.4;
      this._decayTransient();
    }

    _stateHappy(t, dt) {
      const p = this.params;
      p.breath = Math.sin(t * 1.2) * 0.6;
      p.eyeLeft.scaleX = p.eyeRight.scaleX = 1 + 0.05 * (0.5 + 0.5 * Math.sin(t * 2.4));
      p.eyeLeft.scaleY = p.eyeRight.scaleY = 1 + 0.03 * (0.5 + 0.5 * Math.sin(t * 1.8));
      p.faceShiftY = 0.005 * Math.sin(t * 1.4);
      p.motionIntensity = 0.35 + 0.15 * Math.sin(t * 2.0);
      if (Math.sin(t * 0.9) > 0.85) p.pulse = 0.6;
      this._decayTransient();
    }

    _stateExcited(t, dt) {
      const p = this.params;
      p.breath = Math.sin(t * 3.2) * 0.8;
      p.eyeLeft.scaleX = p.eyeRight.scaleX = 1 + 0.10 * Math.abs(Math.sin(t * 4.2));
      p.eyeLeft.scaleY = p.eyeRight.scaleY = 1 + 0.06 * Math.abs(Math.sin(t * 5.0));
      p.pulse = 0.4 + 0.4 * Math.abs(Math.sin(t * 3.6));
      p.distortion = 0.25;
      p.motionIntensity = 0.7;
      p.faceTilt = 0.02 * Math.sin(t * 5);
      this._decayTransient();
    }

    _stateCalm(t, dt) {
      const p = this.params;
      p.breath = Math.sin(t * 0.6) * 0.3;
      p.motionIntensity = 0.05;
      p.eyeLeft.scaleY  = p.eyeRight.scaleY = 1 - 0.04 * Math.abs(Math.sin(t * 0.5));
      this._decayTransient();
    }

    _stateConfused(t, dt) {
      const p = this.params;
      p.asymmetry = 0.6 * Math.sin(t * 1.6);
      p.eyeLeft.offsetX  = 0.012 * Math.sin(t * 2.1);
      p.eyeRight.offsetX = 0.012 * Math.sin(t * 1.7 + 1.0);
      p.eyeLeft.scaleX   = 1 + 0.10 * Math.abs(Math.sin(t * 2.0));
      p.eyeRight.scaleX  = 1 - 0.06 * Math.abs(Math.sin(t * 2.3));
      p.lookX = 0.3 * Math.sin(t * 1.4);
      p.motionIntensity = 0.45;
      this._decayTransient();
    }

    _stateFocused(t, dt) {
      const p = this.params;
      p.lookX = 0.15 * Math.sin(t * 0.9);
      p.lookY = -0.05 * Math.sin(t * 0.7);
      p.motionIntensity = 0.15;
      // Brief scanning pulse
      if (Math.sin(t * 0.8) > 0.94) p.pulse = 0.5;
      this._decayTransient();
    }

    _stateFrustrated(t, dt) {
      const p = this.params;
      // Sharper, faster small movements
      const fast = Math.sin(t * 9.0);
      p.distortion = 0.30 + 0.15 * fast;
      p.eyeLeft.scaleX  = 1 - 0.05 * Math.abs(fast);
      p.eyeRight.scaleX = 1 - 0.05 * Math.abs(Math.sin(t * 9 + 1));
      p.pulse = 0.4 + 0.4 * Math.abs(fast);
      p.motionIntensity = 0.7;
      this._decayTransient();
    }

    _stateSurprised(t, dt) {
      const p = this.params;
      // 0..0.16 quick widening, 0.16..0.45 hold, 0.45..0.85 smooth return.
      // Widening is modest so the eyes never merge into one bar.
      if (t < 0.16) {
        const k = Easing.outCubic(t / 0.16);
        const sx = 1 + 0.12 * k;
        const sy = 1 + 0.05 * k;
        p.eyeLeft.scaleX = sx; p.eyeLeft.scaleY = sy;
        p.eyeRight.scaleX = sx; p.eyeRight.scaleY = sy;
        p.pulse = 0.6 * k;
      } else if (t < 0.45) {
        // hold
      } else if (t < 0.85) {
        const k = Easing.inOutCubic((t - 0.45) / 0.40);
        const sx = 1.12 - 0.12 * k;
        const sy = 1.05 - 0.05 * k;
        p.eyeLeft.scaleX = sx; p.eyeLeft.scaleY = sy;
        p.eyeRight.scaleX = sx; p.eyeRight.scaleY = sy;
      } else if (!this._surprisedDone) {
        this._surprisedDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    // CURIOUS — head tilts slightly, eyes scan left then right, slight
    // asymmetric squint. Reads as "what's that?". Auto-returns to IDLE
    // after ~2.4s. Trigger: something intriguing noticed.
    _stateCurious(t, dt) {
      const p = this.params;
      // Phases: 0..0.3 enter, 0.3..1.0 scan, 1.0..2.4 hold + breathe, 2.4+ exit
      const tiltAmp = 0.025;
      p.faceTilt = tiltAmp * Math.sin(t * 1.3 + 0.4);
      // Eyes scan left at 0.5s, right at 1.5s
      const scanLeft = Easing.outCubic(Math.max(0, 1 - Math.abs(t - 0.4) / 0.4));
      const scanRight = Easing.outCubic(Math.max(0, 1 - Math.abs(t - 1.4) / 0.4));
      const lx = -0.10 * scanLeft + 0.10 * scanRight;
      p.lookX = lx;
      p.lookY = -0.04 * Math.sin(t * 1.1);
      // Subtle asymmetric squint (one eye slightly narrower)
      const a = Math.sin(t * 1.8);
      p.asymmetry = 0.25 * a;
      p.eyeLeft.scaleY  = 1 - 0.04 * Math.abs(a);
      p.eyeRight.scaleY = 1 - 0.04 * Math.abs(Math.sin(t * 1.8 + 1.1));
      p.breath = Math.sin(t * 0.8) * 0.3;
      p.motionIntensity = 0.35 + 0.10 * Math.abs(a);
      if (t > 0.2 && t < 0.4) p.pulse = 0.4 * Easing.outCubic((t - 0.2) / 0.2);
      if (t > 2.4 && !this._curiousDone) {
        this._curiousDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    // AMUSED — eyes narrow slightly (like a soft smile), small vertical
    // bob, occasional pulse. Reads as a quiet chuckle. Auto-returns after
    // ~2.0s. Trigger: something funny.
    _stateAmused(t, dt) {
      const p = this.params;
      const env = Math.sin(t * 3.0);
      // Narrow eyes
      const narrow = 1 - 0.20 * (0.5 + 0.5 * Math.sin(t * 2.2));
      p.eyeLeft.scaleY  = narrow;
      p.eyeRight.scaleY = narrow;
      // Tiny squish in width to match a smile
      p.eyeLeft.scaleX  = 1 + 0.06 * Math.sin(t * 2.2);
      p.eyeRight.scaleX = 1 + 0.06 * Math.sin(t * 2.2);
      // Vertical bob
      p.faceShiftY = 0.008 * Math.sin(t * 3.0);
      // Soft pulse on each bob peak
      if (env > 0.92) p.pulse = 0.5;
      p.breath = Math.sin(t * 1.8) * 0.4;
      p.motionIntensity = 0.30 + 0.10 * Math.abs(env);
      if (t > 2.0 && !this._amusedDone) {
        this._amusedDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    // SLEEPY — heavy eyelids (gentle vertical compress), very slow breath,
    // occasional micro-pulse as if fighting to stay awake. Loops while
    // held. Trigger: long inactivity, or explicit.
    _stateSleepy(t, dt) {
      const p = this.params;
      const slow = Math.sin(t * 0.7);
      // Heavy eyelids: scaleY drops over time then comes back up
      const heavy = 1 - 0.20 * (0.5 + 0.5 * slow);
      p.eyeLeft.scaleY  = heavy;
      p.eyeRight.scaleY = heavy;
      p.eyeLeft.scaleX  = 1 + 0.02 * Math.sin(t * 0.9);
      p.eyeRight.scaleX = 1 + 0.02 * Math.sin(t * 0.9);
      // Slow deep breath
      p.breath = Math.sin(t * 0.5) * 0.5;
      // Occasional "almost blinked" pulse
      if (Math.sin(t * 0.4) > 0.95) p.pulse = 0.3;
      p.motionIntensity = 0.08 + 0.04 * Math.abs(slow);
      // Gentle face tilt
      p.faceTilt = 0.015 * Math.sin(t * 0.6);
      this._decayTransient();
    }

    // PROUD — eyes lift slightly, gentle scale up (taller + slightly wider),
    // soft pulse, vertical lift. Reads as "I did it". Auto-returns after
    // ~2.2s. Trigger: success / task completion.
    _stateProud(t, dt) {
      const p = this.params;
      const k = Easing.outCubic(clamp(t / 0.25, 0, 1));
      const settle = t < 0.25 ? k : 1;
      // Eyes lift slightly (look up)
      p.lookY = 0.06 * settle;
      // Modest scale up
      p.eyeLeft.scaleX  = 1 + 0.08 * settle;
      p.eyeRight.scaleX = 1 + 0.08 * settle;
      p.eyeLeft.scaleY  = 1 + 0.04 * settle;
      p.eyeRight.scaleY = 1 + 0.04 * settle;
      // Vertical lift (face shifts up a touch)
      p.faceShiftY = 0.012 * settle;
      // Slow confident breath
      p.breath = Math.sin(t * 1.0) * 0.4;
      // One soft pulse on entry
      if (t < 0.25) p.pulse = 0.5 * k;
      // Gentle afterglow pulse
      if (t > 0.6 && Math.sin(t * 1.4) > 0.9) p.pulse = 0.3;
      p.motionIntensity = 0.30;
      if (t > 2.2 && !this._proudDone) {
        this._proudDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    // SUSPICIOUS — eyes narrow asymmetrically, slight side-glance,
    // head tilts, occasional micro-pulse. Reads as "really?".
    // Auto-returns after ~2.6s. Trigger: something questionable.
    _stateSuspicious(t, dt) {
      const p = this.params;
      // Side-glance to the left
      const glance = Easing.outCubic(Math.max(0, 1 - Math.abs(t - 0.4) / 0.4));
      p.lookX = -0.08 * glance;
      // Head tilts slightly
      p.faceTilt = 0.020 * Math.sin(t * 1.1);
      // Narrow asymmetrically
      const narrow = 1 - 0.25 * (0.5 + 0.5 * Math.sin(t * 1.4));
      p.eyeLeft.scaleY  = narrow;
      p.eyeRight.scaleY = narrow;
      p.eyeLeft.scaleX  = 1 - 0.04 * Math.abs(Math.sin(t * 1.4));
      p.eyeRight.scaleX = 1 - 0.04 * Math.abs(Math.sin(t * 1.4));
      // Tiny asymmetry: one eyebrow-ish lift via scaleY difference
      p.asymmetry = 0.30 * Math.sin(t * 1.6);
      // Periodic micro-pulses (eyebrow raises)
      if (Math.sin(t * 1.4) > 0.85) p.pulse = 0.35;
      p.motionIntensity = 0.25;
      p.breath = Math.sin(t * 0.8) * 0.2;
      if (t > 2.6 && !this._suspiciousDone) {
        this._suspiciousDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    _stateMusic(t, dt) {
      const p = this.params;
      // Gentle left/right sway with the rhythm. The actual energy comes
      // from the audio analyser (low/mid).
      const beat = Math.sin(t * 2.0);
      const sway = 0.025 * Math.sin(t * 1.6);
      p.faceShiftX = sway;
      p.faceTilt   = 0.04 * Math.sin(t * 1.1);
      p.music      = clamp(0.4 + 0.6 * Math.sin(t * 1.4), 0, 1);
      p.eyeLeft.scaleX  = 1 + 0.04 * (0.5 + 0.5 * beat);
      p.eyeRight.scaleX = 1 + 0.04 * (0.5 + 0.5 * Math.sin(t * 2.0 + 1));
      p.pulse = 0.2 + 0.3 * Math.max(0, beat);
      p.motionIntensity = 0.4 + 0.2 * Math.abs(Math.sin(t * 1.3));
      this._decayTransient();
    }

    _stateError(t, dt) {
      const p = this.params;
      // One eye flickers while the other stays mostly stable. Strong
      // but not chaotic desync. Distortion is small enough that the
      // shape remains recognizable as the same character.
      const flick = Math.sin(t * 9) * 0.5 + Math.sin(t * 14) * 0.5;
      p.asymmetry = 0.5 * flick;
      p.glitch    = 0.35;
      p.distortion = 0.18 + 0.08 * Math.abs(flick);
      p.error     = 0.9 * (1 - Math.exp(-t * 1.5));
      p.eyeLeft.scaleX  = 1 - 0.08 * Math.abs(flick);
      p.eyeRight.scaleX = 1 + 0.04 * Math.sin(t * 2);
      // Phase: instability (0..1.4) -> desync (1.4..2.6) -> recovery (after)
      if (t > 2.6 && !this._errorDone) {
        this._errorDone = true;
        this.setState({ state: 'RECOVERY' });
      }
      this._decayTransient();
    }

    _stateRecovery(t, dt) {
      const p = this.params;
      const k = Easing.outCubic(clamp(t / 1.5, 0, 1));
      p.error = (1 - k) * 0.7;
      p.glitch = (1 - k) * 0.3;
      p.asymmetry = (1 - k) * 0.3 * Math.sin(t * 3);
      // gradually bring shapes back to sync.
      p.eyeLeft.scaleX  = lerp(0.92, 1, k);
      p.eyeRight.scaleX = lerp(1.08, 1, k);
      p.eyeLeft.scaleY  = lerp(0.95, 1, k);
      p.eyeRight.scaleY = lerp(1.04, 1, k);
      p.distortion = (1 - k) * 0.18;
      if (t > 1.5 && !this._recoveryDone) {
        this._recoveryDone = true;
        this.setState({ state: 'IDLE' });
      }
      this._decayTransient();
    }

    // ---------- audio layer ----------------------------------------------

    // SCAN — verification in progress: eyes narrow a touch, gaze sweeps
    // left/right like reading, shader scanline + teal accent, calm.
    _stateScan(t, dt) {
      const p = this.params;
      const breathe = Math.sin(t * 1.2) * 0.3;
      p.breath = breathe;
      // Reading sweep: two slow passes, then hold.
      const sweep = t < 2.4 ? Math.sin(t * 2.6) : Math.sin(2.4 * 2.6);
      p.lookX = sweep * 0.28;
      p.lookY = -0.06;
      p.eyeLeft.scaleY = 0.92; p.eyeRight.scaleY = 0.92;
      p.eyeLeft.scaleX = 1.02; p.eyeRight.scaleX = 1.02;
      p.iris = 0.9;
      p.motionIntensity = 0.25 + 0.1 * Math.sin(t * 3.1);
      // Shader scanline: ping-pong 0->1->0 per pass.
      const cycle = (t * 0.8) % 1.0;
      p.scan = cycle;
      p.accent.r = 0.48; p.accent.g = 0.88; p.accent.b = 0.78;
      p.accentAmt = Math.max(p.accentAmt, 0.35);
      this._decayTransient();
    }

    _applyAudioLayer() {
      const p = this.params;
      const a = this.audio;
      // Bass can create stronger pulses (already applied in shader via u_audioLow).
      // Speech adds a touch of horizontal jitter. Use = (not +=) so the
      // shift does not drift/accumulate across frames — every frame must
      // produce a deterministic, small, centered contribution.
      p.faceShiftX = 0.001 * (a.mid - 0.3);
      // High frequencies tickle distortion a bit more.
      p.distortion = clamp((p.distortion || 0) + a.high * 0.2, 0, 1);
    }

    // ---------- transient decay -------------------------------------------

    _decayTransient() {
      // After a behavior ends, decay the parameter values that should fade.
      // The per-frame spring snap in tick() handles most of this; this
      // hook exists for state-specific easing.
    }

    _mergePatch(patch) {
      const p = this.params;
      for (const k in patch) {
        const v = patch[k];
        if (k === 'eyeLeft' || k === 'eyeRight') {
          Object.assign(p[k], v);
        } else if (k === 'prop') {
          Object.assign(p.prop, v);
        } else {
          p[k] = v;
        }
      }
    }

    // ---------- tick ------------------------------------------------------

    tick(dt) {
      this.timeNow += dt;
      // Advance transition progress.
      if (this.transition) {
        this.transition.t += dt;
        if (this.transition.t >= this.transition.dur) this.transition = null;
      }
      // Reset per-frame temporary values that behaviors may overwrite.
      const p = this.params;
      // Always reset per-frame derivatives so state behaviors don't leak
      // (state behaviors set everything they care about).
      p.distortion = 0;
      p.motionIntensity = 0;
      p.glitch = 0;
      p.pulse = 0;

      // Apply state behavior (writes into p).
      this._applyStateBehavior(dt);

      // Pointer gaze blending (IDLE only, gentle) + reflexive saccades.
      this._applyGaze(dt);
      this._maybeSaccade();

      // One-shot event effects (burst / sweep / scan / flash / trouble).
      this._applyEffects(dt);

      // Apply audio layer on top.
      this._applyAudioLayer();

      // Prop interpolation.
      this._updateProp(dt);

      // Snap-to-zero for tiny values to keep shader stable.
      if (Math.abs(p.faceTilt)   < 1e-4) p.faceTilt = 0;
      if (Math.abs(p.faceShiftX) < 1e-4) p.faceShiftX = 0;
      if (Math.abs(p.faceShiftY) < 1e-4) p.faceShiftY = 0;

      return p;
    }

    _applyGaze(dt) {
      // Smooth-follow the pointer target; only IDLE blends it into the
      // actual look (states own the gaze while they're active).
      const g = this.gaze;
      const k = Math.min(1, dt * 3.0);
      g.x += (g.tx - g.x) * k;
      g.y += (g.ty - g.y) * k;
      if (this.state === 'IDLE' && !this.currentBehavior
          && !this.effects.some(e => e.kind === 'saccade')) {
        // Gentle bounded pull toward the pointer (never more than ±0.06
        // of extra look — the eyes acknowledge the cursor, they don't
        // chase it).
        this.params.lookX += (g.x * 0.06 - this.params.lookX) * 0.10;
        this.params.lookY += (g.y * 0.04 - this.params.lookY) * 0.10;
      }
    }

    _updateProp(dt) {
      // Fade prop opacity toward target. Kind changes snap (so swapping
      // headset -> mic isn't a crossfade of two completely different shapes).
      const cur = this.currentProp;
      const tgt = this.propTarget;
      if (cur.kind !== tgt.kind) {
        // Fade current out, then swap and fade in. ~1.0s total.
        cur.opacity += -dt * 1.1;
        if (cur.opacity <= 0.01) {
          cur.kind = tgt.kind;
          cur.opacity = 0;
          cur.level = tgt.level || 0;
        }
      } else {
        const diff = tgt.opacity - cur.opacity;
        cur.opacity += diff * Math.min(1, dt * 2.2);
      }
      cur.opacity = clamp(cur.opacity, 0, 1);
      this.params.prop.kind    = cur.kind;
      this.params.prop.opacity = cur.opacity;
      this.params.prop.level   = cur.level;
    }

    // Current state for the debug UI.
    get debugInfo() {
      const now = this.timeNow;
      const b = this.currentBehavior;
      return {
        state: this.state,
        activityLevel: this._activityLevel,
        behavior: b ? b.name : '—',
        behaviorT: (this.behaviorT || 0).toFixed(2),
        behaviorDur: (this.behaviorDur || 0).toFixed(2),
        lastBehavior: this._lastBehavior ? this._lastBehavior.name : '—',
        lastBehaviorAge: this._lastBehavior ? (now - this._lastBehaviorTime).toFixed(1) : '—',
        nextEvalIn: Math.max(0, (this._nextEvaluationAt || 0) - now).toFixed(1),
        cooldownIn: Math.max(0, (this.cooldownUntil || 0) - now).toFixed(1),
        recentBehaviors: (this._recentBehaviors || []).slice(0, 6).join(', ') || '—',
      };
    }
  }

  window.NexAnim = NexAnim;
  window.NexEasing = Easing;
})();