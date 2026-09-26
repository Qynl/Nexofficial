/* The living face — Nex's presence.
 *
 * One WebGL canvas, one behavior-engine instance, driven by app state:
 *
 *   idle       calm, occasional idle behaviors (blinks, drifts)
 *   listening  mic is open (amplitude from the mic analyser)
 *   thinking   a reply or plan is being computed
 *   speaking   TTS is playing (mouth synced to utterance boundaries)
 *   working    the agent is executing (scanline sweep)
 *   error      something failed
 *
 * The rig has two poses: HERO (empty chat — large, centered) and
 * BAR (topbar — compact). The pose is pure CSS.
 */

export class Face {
  constructor(canvas) {
    this.canvas = canvas;
    this.state = 'IDLE';
    this.level = 0;
    this._raf = null;
    this._analyser = null;
    this._freq = null;
    this._gaze = { x: 0, y: 0 };
    this.ok = false;
    try {
      this.gl = new window.NexGL(canvas);
      this.anim = new window.NexAnim();
      this.anim.start();
      this.ok = true;
    } catch (err) {
      console.warn('[face] unavailable:', err);
      return;
    }
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);

    // quiet gaze-follow (idle only; blended by the engine)
    window.addEventListener('pointermove', (e) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = (e.clientY / window.innerHeight) * 2 - 1;
      this._gaze = { x: nx * 0.85, y: ny * 0.6 };
    }, { passive: true });
  }

  setMode(mode) {
    // 'hero' | 'bar' | 'hidden'
    document.body.dataset.face = mode;
  }

  setState(state, params = {}) {
    if (!this.ok) return;
    this.state = state;
    this.anim.setState({ state, params });
  }

  speak(text) {
    if (!this.ok) return;
    this.anim.setSpeechText && this.anim.setSpeechText(text || '');
  }

  /* Attach a mic stream so LISTENING reflects real amplitude. */
  attachAnalyser(analyser) {
    this._analyser = analyser;
    if (analyser) {
      this._freq = new Uint8Array(analyser.frequencyBinCount);
    }
  }

  _loop(now) {
    this._raf = requestAnimationFrame(this._loop);
    if (!this.ok) return;
    const dt = Math.min(0.05, (now - (this._lastT || now)) / 1000);
    this._lastT = now;

    let audio = { low: 0, mid: 0, high: 0 };
    if (this._analyser) {
      this._analyser.getByteFrequencyData(this._freq);
      const f = this._freq;
      const loEnd = Math.floor(f.length * 0.12);
      const midEnd = Math.floor(f.length * 0.5);
      let lo = 0, mi = 0, hi = 0;
      for (let i = 0; i < loEnd; i++) lo += f[i];
      for (let i = loEnd; i < midEnd; i++) mi += f[i];
      for (let i = midEnd; i < f.length; i++) hi += f[i];
      audio = {
        low: lo / (loEnd * 255),
        mid: mi / ((midEnd - loEnd) * 255),
        high: hi / ((f.length - midEnd) * 255),
      };
    }
    this.anim.setAudio(audio);

    const params = this.anim.tick(dt);
    if (this._gaze && (this.anim.state === 'IDLE'
                       || this.anim.state === 'LISTENING')) {
      const cur = params.lookX || 0;
      params.lookX = cur * 0.88 + this._gaze.x * 0.12;
    }
    this.gl.render(params, audio);
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
  }
}

/* Map app-level intent to the face state vocabulary. */
export function faceStateFor(intent) {
  switch (intent) {
    case 'listening': return 'LISTENING';
    case 'thinking': return 'THINKING';
    case 'speaking': return 'SPEAKING';
    case 'working': return 'EXECUTING';
    case 'verifying': return 'VERIFYING';
    case 'planning': return 'SCAN';
    case 'error': return 'ERROR';
    default: return 'IDLE';
  }
}
