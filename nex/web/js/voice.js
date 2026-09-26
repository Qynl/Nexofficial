/* Voice — speech output (speechSynthesis) and input (SpeechRecognition).
 *
 * Nothing here is required for Nex to work: if the browser lacks the
 * APIs the buttons stay hidden and nothing breaks. Interruption is
 * first-class: any user action stops the speech.
 */

const state = {
  ttsSupported: 'speechSynthesis' in window,
  sttSupported: !!(window.SpeechRecognition
                  || window.webkitSpeechRecognition),
  ttsEnabled: loadPref('nex.tts', false),
  voiceURI: loadPref('nex.voice', ''),
  rate: parseFloat(loadPref('nex.rate', '1.0')),
  autoSend: loadPref('nex.autosend', '0') === '1',
  speaking: false,
  listening: false,
  recognition: null,
  analyser: null,
  onFinal: null,
  onInterim: null,
  onStateChange: null,
};

function loadPref(key, dflt) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? dflt : v;
  } catch {
    return dflt;
  }
}
function savePref(key, v) {
  try {
    localStorage.setItem(key, String(v));
  } catch { /* private mode */ }
}

function notify() {
  state.onStateChange && state.onStateChange({ ...state });
}

export const voice = {
  get state() { return state; },

  onStateChange(fn) { state.onStateChange = fn; },
  onDictation(fn) { state.onFinal = fn; },
  onInterim(fn) { state.onInterim = fn; },

  voices() {
    if (!state.ttsSupported) return [];
    return window.speechSynthesis.getVoices()
      .filter((v) => v.lang && v.lang.startsWith('en'));
  },

  setVoice(uri) {
    state.voiceURI = uri;
    savePref('nex.voice', uri);
    notify();
  },
  setRate(r) {
    state.rate = Math.max(0.5, Math.min(2, r));
    savePref('nex.rate', String(state.rate));
    notify();
  },
  setAutoSend(v) {
    state.autoSend = !!v;
    savePref('nex.autosend', v ? '1' : '0');
    notify();
  },
  setEnabled(v) {
    state.ttsEnabled = !!v;
    savePref('nex.tts', state.ttsEnabled ? '1' : '0');
    if (!state.ttsEnabled) voice.stopSpeaking();
    notify();
  },

  /* ---- output ---- */
  speak(text, { onEnd } = {}) {
    if (!state.ttsSupported || !state.ttsEnabled || !text
        || !text.trim()) {
      onEnd && onEnd();
      return;
    }
    voice.stopSpeaking();
    const u = new SpeechSynthesisUtterance(
      text.replace(/```[\s\S]*?```/g, ' (code block) ')
         .replace(/[*_#`>|]/g, '')
         .slice(0, 3000));
    const v = voice.voices().find((x) => x.voiceURI === state.voiceURI);
    if (v) u.voice = v;
    u.rate = state.rate;
    u.onstart = () => {
      state.speaking = true;
      notify();
    };
    const done = () => {
      state.speaking = false;
      notify();
      onEnd && onEnd();
    };
    u.onend = done;
    u.onerror = done;
    window.speechSynthesis.speak(u);
  },

  stopSpeaking() {
    if (state.ttsSupported) {
      try {
        window.speechSynthesis.cancel();
      } catch { /* ignore */ }
    }
    state.speaking = false;
    notify();
  },

  /* ---- input (dictation) ---- */
  toggleListening() {
    if (state.listening) {
      voice.stopListening();
      return;
    }
    if (!state.sttSupported) return;
    const Ctor = window.SpeechRecognition
      || window.webkitSpeechRecognition;
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = 'en-US';

    state.recognition = rec;
    state.listening = true;

    // Live mic level for the face.
    try {
      navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
        if (!state.listening) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        state._stream = stream;
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        state.analyser = analyser;
        state._ctx = ctx;
        notify();
      }).catch(() => {
        toastMicDenied();
      });
    } catch {
      toastMicDenied();
    }

    rec.onresult = (ev) => {
      let interim = '';
      let final = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) final += r[0].transcript;
        else interim += r[0].transcript;
      }
      if (final) state.onFinal && state.onFinal(final.trim());
      else if (interim) state.onInterim && state.onInterim(interim.trim());
    };
    rec.onerror = (ev) => {
      if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
        toastMicDenied();
        voice.stopListening();
      }
    };
    rec.onend = () => {
      if (state.recognition === rec) {
        // Chrome stops after silence; keep going until user toggles off.
        try {
          rec.start();
          return;
        } catch { /* fall through to stopped state */ }
      }
    };
    try {
      rec.start();
    } catch { /* already started */ }
    notify();
  },

  stopListening() {
    const rec = state.recognition;
    state.recognition = null;
    state.listening = false;
    if (rec) {
      rec.onend = null;
      try {
        rec.stop();
      } catch { /* ignore */ }
    }
    if (state._stream) {
      state._stream.getTracks().forEach((t) => t.stop());
      state._stream = null;
    }
    if (state._ctx) {
      try {
        state._ctx.close();
      } catch { /* ignore */ }
      state._ctx = null;
    }
    state.analyser = null;
    state.onInterim && state.onInterim('');
    notify();
  },
};

function toastMicDenied() {
  import('./toasts.js').then(({ toast }) => {
    toast('warn', 'Microphone unavailable',
          'Grant microphone permission (or use HTTPS) to dictate.');
  });
}

if (state.ttsSupported) {
  // Voice list loads async in Chrome.
  window.speechSynthesis.onvoiceschanged = () => notify();
  window.speechSynthesis.getVoices();
}
