/* Composer — input, dictation, send/stop, TTS toggle. */

import { store } from './state.js';
import { api } from './api.js';
import { toast, errorToast } from './toasts.js';
import { showTyping, hideTyping } from './chat.js';
import { face, faceStateFor } from './face.js';
import { voice } from './voice.js';

export function initComposer() {
  const input = document.getElementById('composer-input');
  const send = document.getElementById('btn-send');
  const stop = document.getElementById('btn-stop');
  const mic = document.getElementById('btn-mic');
  const speakBtn = document.getElementById('btn-speak');
  const dictateStatus = document.getElementById('dictate-status');

  function refreshSend() {
    send.disabled = !input.value.trim() && !store.get().generating;
  }

  input.addEventListener('input', () => {
    autoGrow();
    refreshSend();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    } else if (e.key === 'Escape' && voice.state.listening) {
      voice.stopListening();
    }
  });
  send.addEventListener('click', submit);
  stop.addEventListener('click', stopGeneration);

  // ── voice buttons ──
  if (voice.state.sttSupported) {
    mic.hidden = false;
    mic.addEventListener('click', () => {
      voice.toggleListening();
    });
  }
  if (voice.state.ttsSupported) {
    speakBtn.hidden = false;
    speakBtn.classList.toggle('active', voice.state.ttsEnabled);
    speakBtn.addEventListener('click', () => {
      voice.setEnabled(!voice.state.ttsEnabled);
      speakBtn.classList.toggle('active', voice.state.ttsEnabled);
      if (voice.state.ttsEnabled) {
        toast('success', 'Read-aloud on', 'Replies will be spoken.');
      }
    });
  }
  voice.onStateChange((vs) => {
    mic.classList.toggle('rec-pulse', vs.listening);
    mic.classList.toggle('active', vs.listening);
    document.querySelector('.composer').classList
      .toggle('listening', vs.listening);
    dictateStatus.hidden = !vs.listening;
    dictateStatus.className = vs.listening ? 'rec' : '';
    dictateStatus.textContent = vs.listening ? 'listening…' : '';
    if (vs.listening) {
      face.setState(faceStateFor('listening'));
    } else {
      face.setState(faceStateFor('idle'));
    }
    if (vs.analyser) {
      face.attachAnalyser(vs.analyser);
    } else {
      face.attachAnalyser(null);
    }
  });
  voice.onDictation((finalText) => {
    if (!finalText) return;
    const cur = input.value;
    input.value = cur ? cur.replace(/\s*$/, ' ') + finalText : finalText;
    autoGrow();
    refreshSend();
    if (voice.state.autoSend) {
      submit();
    }
  });
  voice.onInterim((interim) => {
    const hint = document.getElementById('composer-hint');
    hint.textContent = interim
      ? `“${interim.slice(-60)}”`
      : 'Enter to send · Shift+Enter for a new line';
  });

  async function submit() {
    const text = input.value.trim();
    const st = store.get();
    if (!text || st.generating) return;
    if (!st.activeId) return;

    // any user action interrupts speech
    voice.stopSpeaking();

    input.value = '';
    autoGrow();
    refreshSend();

    // optimistic user bubble
    const chat = document.getElementById('chat');
    const el = document.createElement('div');
    el.className = 'msg msg-user';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    el.appendChild(bubble);
    chat.appendChild(el);
    const scroller = document.getElementById('scroller');
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });

    store.set({ generating: true });
    showTyping();
    face.setState(faceStateFor('thinking'));
    try {
      await api.send(st.activeId, text);
    } catch (err) {
      store.set({ generating: false });
      hideTyping();
      errorToast(err);
      input.value = text;   // give the words back
      autoGrow();
      refreshSend();
    }
    refreshSend();
  }

  async function stopGeneration() {
    // stop local speech + typing; ask the server to stop the active run
    voice.stopSpeaking();
    const runs = store.get().activeRun;
    try {
      if (runs && runs.run_id) {
        await api.cancelRun(runs.run_id);
      }
    } catch { /* best effort */ }
    // The chat stream ends on its own; recover UI if it stalled.
    setTimeout(() => {
      if (store.get().generating) {
        store.set({ generating: false });
        hideTyping();
      }
    }, 600);
  }

  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight,
      parseInt(getComputedStyle(input).maxHeight || '300', 10)) + 'px';
  }

  // generating-state UI (send ↔ stop)
  store.subscribe((s, prev) => {
    if (s.generating !== prev.generating) {
      send.hidden = s.generating;
      stop.hidden = !s.generating;
      input.placeholder = s.generating
        ? 'Nex is replying…' : 'Message Nex…';
      if (!s.generating) {
        face.setState(faceStateFor('idle'));
        refreshSend();
      }
    }
  });

  refreshSend();
}
