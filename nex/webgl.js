/*
 * NEX — WebGL renderer.
 *
 * Renders two rounded-rectangle "eye" shapes (and optional props) as signed
 * distance fields, fully procedurally. No textures, no SVG, no GIFs.
 *
 * The animation system drives a single coherent parameter set per frame;
 * the shaders apply it to position, scale, deformation, audio distortion,
 * asymmetry, etc.
 *
 * Public API (window.NexGL):
 *   const gl = new NexGL(canvas);
 *   gl.resize();
 *   gl.render(state, audio);   // call every frame
 *
 * `state` is a flat object with fields like:
 *   lookX, lookY, breath, pulse, asymmetry, distortion, audioLow,
 *   audioMid, audioHigh, speechIntensity, motionIntensity,
 *   faceTilt, faceShiftX, faceShiftY,
 *   eyeLeft {offsetX, offsetY, scaleX, scaleY, squash, stretch, corner}
 *   eyeRight{...same...}
 *   visibility  (0..1, used by WAKE)
 *   prop        (null | { kind, opacity })
 */
(function () {
  'use strict';

  // -------- shader sources -------------------------------------------------

  const VERT = `
    attribute vec2 a_pos;
    varying vec2 v_uv;
    void main() {
      v_uv = a_pos * 0.5 + 0.5;
      gl_Position = vec4(a_pos, 0.0, 1.0);
    }
  `;

  // Fragment shader: builds an SDF for each shape and accumulates coverage.
  // Deformation, asymmetry, look offsets, audio-reactive distortion and
  // temporary props are all handled here.
  const FRAG = `
    precision highp float;

    varying vec2 v_uv;

    uniform vec2  u_res;          // canvas size in pixels
    uniform float u_dpr;          // device pixel ratio
    uniform float u_time;         // seconds

    // Base proportions (reference image).
    uniform float u_baseW;        // base shape width  (in normalized units of half-canvas)
    uniform float u_baseH;        // base shape height
    uniform float u_gap;          // gap between shape centers (horizontal)
    uniform float u_corner;       // base corner radius

    // Pose / state parameters (driven from JS).
    uniform float u_visibility;   // 0..1 (wake)
    uniform float u_faceTilt;     // radians
    uniform float u_faceShiftX;   // normalized shift of whole face
    uniform float u_faceShiftY;
    uniform float u_breath;        // -1..1 (subtle)
    uniform float u_pulse;        // 0..1
    uniform float u_lookX;       // -1..1
    uniform float u_lookY;       // -1..1
    uniform float u_asymmetry;   // -1..1 (positive => right shape reacts more)
    uniform float u_distortion;  // 0..1
    uniform float u_motion;       // 0..1 (general liveness)
    uniform float u_audioLow;     // 0..1
    uniform float u_audioMid;     // 0..1
    uniform float u_audioHigh;    // 0..1
    uniform float u_speech;       // 0..1
    uniform float u_listening;    // 0..1
    uniform float u_music;        // 0..1
    uniform float u_error;        // 0..1
    uniform float u_glitch;       // 0..1 (transient)

    // Per-eye overrides (set from JS; default 0).
    uniform vec4  u_left;   // x: offsetX, y: offsetY, z: scaleX, w: scaleY
    uniform vec4  u_right;  // same layout

    // Prop (headset / mic / etc.) — combined into a single uniform
    // {kind, opacity, tilt, level, time}.
    // kind: 0 = none, 1 = headset, 2 = microphone, 3 = controller,
    //       4 = magnifier, 5 = code, 6 = notification.
    uniform vec4  u_prop;   // x: kind, y: opacity, z: tilt, w: level

    // --------------------------------------------------------------
    // SDF primitives
    // --------------------------------------------------------------

    float sdRoundedBox(vec2 p, vec2 b, float r) {
      vec2 q = abs(p) - b + vec2(r);
      return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
    }

    // Smooth min (used to merge/separate fields and to compute coverage).
    float smin(float a, float b, float k) {
      float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
      return mix(b, a, h) - k * h * (1.0 - h);
    }

    // Pixel-grid AA factor based on canvas resolution.
    float aaScale() {
      return 1.5 / min(u_res.x, u_res.y);
    }

    // Slight breathing displacement of a point around a center (used so the
    // corners feel less rigid during long idle).
    vec2 organic(vec2 p, float t) {
      float n = sin(p.x * 3.1 + t * 1.3) * cos(p.y * 2.7 - t * 0.9);
      return p + vec2(0.0, n * 0.0008);
    }

    // Build the SDF for a single eye shape at its center.
    float sdEye(vec2 p, vec2 center, vec4 eyeLocal) {
      // Apply face-level tilt + shift.
      float c = cos(u_faceTilt);
      float s = sin(u_faceTilt);
      vec2 q = p - center - vec2(u_faceShiftX, u_faceShiftY);
      q = mat2(c, -s, s, c) * q;

      // Subtle organic noise (very low amplitude).
      q = organic(q, u_time);

      // Local offsets / scale for this specific eye.
      q -= vec2(eyeLocal.x, eyeLocal.y);

      // Base half-size with breath + pulse.
      float bw = u_baseW * eyeLocal.z;
      float bh = u_baseH * eyeLocal.w;

      // Audio-driven wobble: low frequencies stretch horizontally a touch,
      // high frequencies add micro jitter on the edges.
      bw *= 1.0 + u_audioLow * 0.04 + u_pulse * 0.03;
      bh *= 1.0 + u_audioLow * 0.02 + u_breath * 0.06 + u_pulse * 0.02;

      // Edge distortion (audio + listening) — push the box slightly along
      // its normal using noise so it looks alive instead of perfectly rigid.
      float edgePhase = atan(q.y, q.x);
      float noise =
        sin(edgePhase * 3.0 + u_time * 1.7) * 0.5 +
        sin(edgePhase * 5.0 - u_time * 2.3) * 0.5;
      float distort =
        u_distortion * 0.012 +
        u_audioMid  * 0.010 +
        u_listening * 0.006 +
        u_speech    * 0.008 +
        u_glitch    * 0.040;
      float dBox = sdRoundedBox(q, vec2(bw, bh), u_corner);
      return dBox - noise * distort;
    }

    // --------------------------------------------------------------
    // Props
    // --------------------------------------------------------------

    float sdHeadset(vec2 p, float level) {
      // Minimal headset: a thicker headband arc curving over the top of
      // the face, plus two ear cups at the sides. The arc DOES NOT cover
      // the eyes; it sits clearly above them.
      vec2 q = p;
      q = mat2(cos(u_faceTilt), -sin(u_faceTilt), sin(u_faceTilt), cos(u_faceTilt)) * q;

      // The headband is a thick half-ring above the face. We mask out the
      // bottom half so it only appears above the face.
      float bandR    = 0.28 + level * 0.015;   // headband radius
      float bandThk  = 0.018;                  // headband thickness
      vec2 bandCenter = vec2(0.0, 0.06);        // sits just above face center

      // Distance to the ring around bandCenter.
      float dRing = abs(length(q - bandCenter) - bandR) - bandThk;
      // Mask out everything below the face centerline.
      float mask  = q.y - 0.00;               // negative -> below center
      // Ring only where y > 0 (above the face).
      float arc   = max(dRing, mask);

      // Ear cups — small rounded squares to the sides of the face.
      float cupW = 0.045 + level * 0.005;
      float cupH = 0.075 + level * 0.005;
      float cupGap = u_gap * 0.5 + 0.180;
      float dCupL = sdRoundedBox(q - vec2(-cupGap, -0.02), vec2(cupW, cupH), 0.014);
      float dCupR = sdRoundedBox(q - vec2( cupGap, -0.02), vec2(cupW, cupH), 0.014);

      return min(arc, min(dCupL, dCupR));
    }

    float sdMic(vec2 p, float level) {
      // Minimal mic icon floating below-left of the face.
      vec2 q = p - vec2(-0.34, -0.28);
      q = mat2(cos(u_faceTilt), -sin(u_faceTilt), sin(u_faceTilt), cos(u_faceTilt)) * q;

      float body = sdRoundedBox(q - vec2(0.0, 0.02), vec2(0.04, 0.07), 0.035);
      vec2 stand = vec2(abs(q.x) - 0.005, q.y + 0.10);
      float standD = min(max(stand.x, stand.y), 0.0) + length(max(stand, 0.0)) - 0.004;
      vec2 base = vec2(abs(q.x) - 0.05, q.y + 0.13);
      float baseD = min(max(base.x, base.y), 0.0) + length(max(base, 0.0)) - 0.003;
      float lvl = -length(q - vec2(0.0, 0.05)) + 0.06 + level * 0.015;
      return min(min(body, standD), min(baseD, lvl));
    }

    float sdController(vec2 p, float level) {
      vec2 q = p - vec2(0.0, -0.36);
      q = mat2(cos(u_faceTilt), -sin(u_faceTilt), sin(u_faceTilt), cos(u_faceTilt)) * q;
      return sdRoundedBox(q, vec2(0.18 + level * 0.005, 0.045), 0.04);
    }

    float sdMagnifier(vec2 p, float level) {
      vec2 q = p - vec2(0.32, 0.22);
      float ring = abs(length(q) - 0.07) - 0.008;
      float handle = sdRoundedBox((q - vec2(0.06, -0.06)) * mat2(0.7071, -0.7071, 0.7071, 0.7071),
                                  vec2(0.04, 0.008), 0.005);
      return min(ring, handle);
    }

    float sdCode(vec2 p, float level) {
      vec2 q = p - vec2(-0.30, 0.24);
      // </>
      float lt = sdRoundedBox(q + vec2(0.02, 0.0), vec2(0.012, 0.035), 0.003);
      float slash = sdRoundedBox((q - vec2(0.0, 0.0)) * mat2(0.7071, -0.7071, 0.7071, 0.7071),
                                 vec2(0.045, 0.006), 0.003);
      float gt = sdRoundedBox(q - vec2(0.05, 0.0), vec2(0.012, 0.035), 0.003);
      return min(lt, min(slash, gt));
    }

    float sdBell(vec2 p, float level) {
      vec2 q = p - vec2(0.30, 0.26);
      float body = sdRoundedBox(q - vec2(0.0, -0.01), vec2(0.04, 0.045), 0.035);
      float clapper = length(q - vec2(0.0, -0.075)) - 0.006;
      return min(body, clapper);
    }

    float sdProp(vec2 p) {
      int kind = int(u_prop.x + 0.5);
      if (kind == 0) return 1e6;
      if (kind == 1) return sdHeadset(p, u_prop.w);
      if (kind == 2) return sdMic(p, u_prop.w);
      if (kind == 3) return sdController(p, u_prop.w);
      if (kind == 4) return sdMagnifier(p, u_prop.w);
      if (kind == 5) return sdCode(p, u_prop.w);
      if (kind == 6) return sdBell(p, u_prop.w);
      return 1e6;
    }

    // --------------------------------------------------------------
    // Main
    // --------------------------------------------------------------

    void main() {
      // Map v_uv (0..1) to centered coords in normalized units where
      // the canvas height is 2.0. (Aspect handled implicitly via u_res.)
      vec2 p = (v_uv - 0.5);
      p.x *= u_res.x / u_res.y;     // so things stay circular
      p *= 1.0;                     // half-height = 1.0

      // Look offsets shift shape centers (not the face itself) for natural
      // gaze behavior. Limits keep it subtle.
      float lookRange = 0.045;
      vec2 leftCenter  = vec2(-u_gap + u_lookX * lookRange, u_lookY * lookRange);
      vec2 rightCenter = vec2( u_gap + u_lookX * lookRange, u_lookY * lookRange);

      float dL = sdEye(p, leftCenter,  u_left);
      float dR = sdEye(p, rightCenter, u_right);

      // Merge the two fields. While idle they're separate; during music /
      // certain emotional states they can briefly merge to feel unified.
      float dEyes = smin(dL, dR, 0.010);

      // Prop field.
      float dProp = sdProp(p);
      // Union (not smin) so the headset stays geometrically separate from
      // the eyes. The smin around the eyes themselves stays for look states.
      float merged = min(dEyes, dProp);

      // Anti-aliased coverage.
      float aa = aaScale();
      float cover = 1.0 - smoothstep(-aa, aa, merged);

      // --- Soft inner shading (gives the flat pills dimensional life) ---
      // depth: 0 at the very edge, ~1 deep inside the shape.
      float depth = clamp(-merged / 0.05, 0.0, 1.0);
      // Vertical gradient: very slightly brighter toward the top edge.
      float vgrad = 0.90 + 0.10 * clamp(p.y * 0.5 + 0.5, 0.0, 1.0);
      // Soft gel falloff from the edge inward (so the center reads brightest).
      float gel = mix(0.88, 1.0, smoothstep(0.0, 1.0, depth));
      // Faint top-left key light highlight for a porcelain/glass feel.
      float hl = 0.05 * smoothstep(0.55, 0.0, length(p - vec2(-0.16, 0.16)));
      // Breathing subtly modulates brightness so idle "feels" alive.
      float shade = gel * vgrad + hl;
      shade *= 1.0 + u_breath * 0.05;

      vec3 col = vec3(cover * shade);

      // --- Outer aura / glow: a soft halo hugging the shapes. ---
      float halo = exp(-max(merged, 0.0) * 22.0);  // tight falloff outside
      col += vec3(0.09 * halo * u_visibility);

      // Tiny vignette so the face doesn't feel pasted onto the background.
      float vig = smoothstep(1.30, 0.42, length(p));
      col *= mix(0.93, 1.0, vig);

      // Glitch: rare transient alpha shimmer.
      float g = step(0.985, fract(sin(u_time * 11.3 + dot(v_uv, vec2(12.9898,78.233))) * 43758.5453));
      cover = mix(cover, cover * (1.0 - 0.4 * u_glitch), g * u_glitch);
      col *= mix(1.0, 1.0 - 0.4 * u_glitch, g * u_glitch);

      // WAKE: visibility scales everything in.
      col *= u_visibility;

      gl_FragColor = vec4(col, 1.0);
    }
  `;

  // -------- helpers --------------------------------------------------------

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error('Shader compile error: ' + log);
    }
    return sh;
  }

  function link(gl, vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(p);
      gl.deleteProgram(p);
      throw new Error('Program link error: ' + log);
    }
    return p;
  }

  // -------- NexGL ----------------------------------------------------------

  class NexGL {
    constructor(canvas) {
      this.canvas = canvas;
      const gl = canvas.getContext('webgl', { antialias: true, premultipliedAlpha: false, alpha: false });
      if (!gl) throw new Error('WebGL not available');
      this.gl = gl;

      const vs = compile(gl, gl.VERTEX_SHADER, VERT);
      const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
      this.program = link(gl, vs, fs);

      // Fullscreen triangle.
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        -1, -1,
         3, -1,
        -1,  3,
      ]), gl.STATIC_DRAW);

      const locPos = gl.getAttribLocation(this.program, 'a_pos');
      gl.enableVertexAttribArray(locPos);
      gl.vertexAttribPointer(locPos, 2, gl.FLOAT, false, 0, 0);

      // Cache uniform locations.
      const U = {};
      const names = [
        'u_res','u_dpr','u_time',
        'u_baseW','u_baseH','u_gap','u_corner',
        'u_visibility','u_faceTilt','u_faceShiftX','u_faceShiftY',
        'u_breath','u_pulse','u_lookX','u_lookY',
        'u_asymmetry','u_distortion','u_motion',
        'u_audioLow','u_audioMid','u_audioHigh',
        'u_speech','u_listening','u_music','u_error','u_glitch',
        'u_left','u_right','u_prop',
      ];
      for (const n of names) U[n] = gl.getUniformLocation(this.program, n);
      this.U = U;

      gl.useProgram(this.program);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.BLEND);

      this.resize();
    }

    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.floor(this.canvas.clientWidth  * dpr));
      const h = Math.max(1, Math.floor(this.canvas.clientHeight * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      this.gl.viewport(0, 0, w, h);
      this.dpr = dpr;
    }

    _setUniforms(state, audio) {
      const gl = this.gl;
      const U = this.U;
      gl.uniform2f(U.u_res, this.canvas.width, this.canvas.height);
      gl.uniform1f(U.u_dpr, this.dpr);
      gl.uniform1f(U.u_time, performance.now() * 0.001);

      // Base proportions (matches the reference image: two compact rounded
      // rectangles, slightly wider horizontally than vertically).
      gl.uniform1f(U.u_baseW, 0.220);
      gl.uniform1f(U.u_baseH, 0.155);
      gl.uniform1f(U.u_gap,   0.270);
      gl.uniform1f(U.u_corner, 0.085);

      gl.uniform1f(U.u_visibility, state.visibility ?? 1);
      gl.uniform1f(U.u_faceTilt,  state.faceTilt  ?? 0);
      gl.uniform1f(U.u_faceShiftX,state.faceShiftX?? 0);
      gl.uniform1f(U.u_faceShiftY,state.faceShiftY?? 0);
      gl.uniform1f(U.u_breath,    state.breath    ?? 0);
      gl.uniform1f(U.u_pulse,     state.pulse     ?? 0);
      gl.uniform1f(U.u_lookX,     state.lookX     ?? 0);
      gl.uniform1f(U.u_lookY,     state.lookY     ?? 0);
      gl.uniform1f(U.u_asymmetry, state.asymmetry ?? 0);
      gl.uniform1f(U.u_distortion,state.distortion?? 0);
      gl.uniform1f(U.u_motion,    state.motionIntensity ?? 0);

      gl.uniform1f(U.u_audioLow,  audio.low ?? 0);
      gl.uniform1f(U.u_audioMid,  audio.mid ?? 0);
      gl.uniform1f(U.u_audioHigh, audio.high?? 0);
      gl.uniform1f(U.u_speech,    state.speechIntensity ?? 0);
      gl.uniform1f(U.u_listening, state.listening ?? 0);
      gl.uniform1f(U.u_music,     state.music ?? 0);
      gl.uniform1f(U.u_error,     state.error ?? 0);
      gl.uniform1f(U.u_glitch,    state.glitch ?? 0);

      const l = state.eyeLeft  || {};
      const r = state.eyeRight || {};
      gl.uniform4f(U.u_left,  l.offsetX ?? 0, l.offsetY ?? 0, l.scaleX ?? 1, l.scaleY ?? 1);
      gl.uniform4f(U.u_right, r.offsetX ?? 0, r.offsetY ?? 0, r.scaleX ?? 1, r.scaleY ?? 1);

      const prop = state.prop || { kind: 0, opacity: 0 };
      gl.uniform4f(U.u_prop, prop.kind ?? 0, prop.opacity ?? 0, prop.tilt ?? 0, prop.level ?? 0);
    }

    render(state, audio) {
      this.resize();
      const gl = this.gl;
      gl.useProgram(this.program);
      this._setUniforms(state || {}, audio || {});
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
  }

  window.NexGL = NexGL;
})();