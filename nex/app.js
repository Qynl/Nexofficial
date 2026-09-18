/*
 * NEX — application glue.
 *
 * Owns:
 *   - Audio capture (getUserMedia + AudioContext + AnalyserNode).
 *   - Music mode (an <audio> element plays a procedurally generated loop
 *     so music mode is testable without any external file).
 *   - Server-Sent Events connection to the backend.
 *   - Debug panel + keyboard shortcuts.
 *   - Speech bubble UI.
 *
 * Talks to:
 *   - window.NexGL    (renderer)
 *   - window.NexAnim  (behavior engine)
 */
(function () {
  // Auth bootstrap: when the server runs with NEX_AUTH_TOKEN, every
  // /api + /mcp request must carry it. The token is injected into the
  // served HTML (window.NEX_AUTH); this wrapper attaches it everywhere.
  (function () {
    const TOKEN = window.NEX_AUTH || '';
    if (!TOKEN) return;
    const orig = window.fetch.bind(window);
    window.fetch = (input, init) => {
      try {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.startsWith('/api') || url.startsWith('/mcp')) {
          init = init || {};
          init.headers = Object.assign({}, init.headers || {},
            { 'X-Nex-Auth': TOKEN });
          return orig(input, init);
        }
      } catch (e) { /* fall through */ }
      return orig(input, init);
    };
  })();

  'use strict';

  // ------- DOM -----------------------------------------------------------

  const canvas = document.getElementById('nex');
  const dev    = document.getElementById('dev');
  const devStates = document.getElementById('dev-states');
  const devIdle   = document.getElementById('dev-idle');
  const devInput  = document.getElementById('dev-input');
  const devState    = document.getElementById('dev-state');
  const devActivity = document.getElementById('dev-activity');
  const devBehavior = document.getElementById('dev-behavior');
  const devLast     = document.getElementById('dev-last');
  const devNext     = document.getElementById('dev-next');
  const devRecent   = document.getElementById('dev-recent');
  const bubble = document.getElementById('bubble');

  // ------- core ----------------------------------------------------------

  // Safety net: even if a tag leaks through, hide it from the bubble.
  // Matches the same set the backend uses.
  const CLIENT_TAG_RE = new RegExp(
    '\\[\\s*(?:' + [
      'IDLE','LISTENING','THINKING','SPEAKING',
      'HAPPY','EXCITED','CALM','CONFUSED','FOCUSED',
      'FRUSTRATED','SURPRISED',
      'CURIOUS','AMUSED','SLEEPY','PROUD','SUSPICIOUS',
      'MUSIC','ERROR','RECOVERY','WAKE',
    ].join('|') + ')\\s*\\]', 'ig'
  );
  function stripTagsClient(s) {
    if (!s) return '';
    return s.replace(CLIENT_TAG_RE, '').replace(/\s{2,}/g, ' ').trim();
  }

  let gl, anim, es;

  function boot() {
    try {
      gl = new NexGL(canvas);
    } catch (err) {
      console.error(err);
      return;
    }
    anim = new NexAnim();
    anim.start();

    buildDevPanel();
    bindKeyboard();
    bindMouse();
    bindNetwork();
    startConnPill();
    showBootHint();
    bindHelpChrome();
    // Stagger the UI in once the face has begun waking.
    requestAnimationFrame(() => document.body.classList.add('ready'));

    // Pointer gaze: the face quietly follows the cursor (blended only in
    // IDLE by the animation engine — calm idle is untouched).
    window.addEventListener('pointermove', (e) => {
      if (!anim) return;
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = -((e.clientY / window.innerHeight) * 2 - 1);
      anim.setGaze(nx * 0.9, ny * 0.7);
    }, { passive: true });

    // Mirror the live state onto <body data-state=...> so CSS can react
    // (ambient light, panel accents) without any polling.
    anim._origSetState = anim.setState.bind(anim);
    anim.setState = (req) => {
      anim._origSetState(req);
      const name = ((req && req.state) || 'IDLE').toUpperCase();
      document.body.dataset.state = name;
    };
    document.body.dataset.state = 'IDLE';

    // Speech bubble fade handling — typewriter-style reveal: only the NEW
    // suffix of each streamed chunk animates in.
    let bubbleText = '';
    anim._origSetSpeech = anim.setSpeechText.bind(anim);
    anim.setSpeechText = (t) => {
      anim._origSetSpeech(t);
      const text = String(t || '');
      if (text) {
        if (text.startsWith(bubbleText) && bubbleText && !bubble.hidden) {
          const suffix = text.slice(bubbleText.length);
          if (suffix) {
            const span = document.createElement('span');
            span.className = 'chat-new';
            span.textContent = suffix;
            bubble.appendChild(span);
          }
        } else {
          bubble.textContent = text;
        }
        bubbleText = text;
        bubble.hidden = false;
        requestAnimationFrame(() => bubble.classList.add('show'));
      } else {
        bubbleText = '';
        bubble.classList.remove('show');
        setTimeout(() => { if (!bubble.classList.contains('show')) bubble.hidden = true; }, 250);
      }
    };

    // ------- TTS (Web Speech API) ----------------------------------------
    //
    // The backend streams sentences via SSE. We feed each sentence into
    // speechSynthesis the moment it arrives so the audio starts within
    // ~150ms of the first chunk. This avoids waiting for the full reply
    // and keeps the spoken audio locked to the bubble text — no
    // stutter, no restart, no gap mid-sentence (because the flusher
    // only emits at safe boundaries: a complete sentence).
    //
    // The user can mute via window.NEX_TTS_MUTE = true or by clicking
    // the dev panel "TTS" button (we add that below).
    let ttsMute = false;
    let ttsVoice = null;
    function pickTtsVoice() {
      if (!('speechSynthesis' in window)) return null;
      const voices = window.speechSynthesis.getVoices();
      // Prefer a calm, low-pitch voice if available.
      const prefs = [
        /Google UK English Female/i,
        /Samantha/i, /Microsoft.*Zira/i, /Karen/i, /Moira/i,
        /en-?GB.*Female/i, /en-?US.*Female/i,
      ];
      for (const re of prefs) {
        const v = voices.find(x => re.test(x.name));
        if (v) return v;
      }
      return voices.find(v => /en/i.test(v.lang)) || voices[0] || null;
    }
    if ('speechSynthesis' in window) {
      // Voices load asynchronously on some browsers.
      window.speechSynthesis.onvoiceschanged = () => { ttsVoice = pickTtsVoice(); };
      ttsVoice = pickTtsVoice();
    }

    function speakChunk(text) {
      if (!text || ttsMute || !('speechSynthesis' in window)) return;
      const u = new SpeechSynthesisUtterance(text);
      if (ttsVoice) u.voice = ttsVoice;
      u.rate = 1.0;
      u.pitch = 1.0;
      u.volume = 1.0;
      // The browser queues utterances — they play in submission order
      // and we never interrupt in flight, so the spoken audio stays
      // smooth across delta boundaries.
      window.speechSynthesis.speak(u);
    }

    function cancelTts() {
      if ('speechSynthesis' in window) {
        try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
      }
    }

    // Expose for the dev panel.
    window.__nexTts = {
      speak: speakChunk,
      cancel: cancelTts,
      isMuted: () => ttsMute,
      toggleMute: () => { ttsMute = !ttsMute; if (ttsMute) cancelTts(); return ttsMute; },
      supported: () => 'speechSynthesis' in window,
    };

    startAudio();
    requestAnimationFrame(loop);
  }

  // ------- audio: mic + music -------------------------------------------

  let audioCtx = null;
  let micStream = null;
  let micNode = null;
  let analyser = null;
  let freqArr = null;
  let micEnabled = false;

  let musicEl = null;
  let musicAnalyser = null;
  let musicFreqArr = null;
  let musicEnabled = false;

  function ensureAudioCtx() {
    if (!audioCtx) {
      try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      } catch (e) { audioCtx = null; }
    }
    return audioCtx;
  }

  async function startMic() {
    if (micEnabled) return;
    const ctx = ensureAudioCtx();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (e) {
      console.warn('microphone denied / unavailable', e);
      return;
    }
    micNode = ctx.createMediaStreamSource(micStream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.65;
    micNode.connect(analyser);
    freqArr = new Uint8Array(analyser.frequencyBinCount);
    micEnabled = true;
  }

  function stopMic() {
    if (!micEnabled) return;
    try {
      if (micStream) micStream.getTracks().forEach(t => t.stop());
    } catch (e) { /* ignore */ }
    micStream = null;
    micNode = null;
    analyser = null;
    freqArr = null;
    micEnabled = false;
  }

  function toggleMic() {
    if (micEnabled) { stopMic(); markButton('mic', false); }
    else            { startMic().then(() => markButton('mic', micEnabled)); }
  }

  // Music: a procedural loop generated with WebAudio. Lets music mode be
  // exercised offline without any audio file.
  let musicNodes = null;
  function startMusic() {
    if (musicEnabled) return;
    const ctx = ensureAudioCtx();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();

    // Beat pattern: kick on 1, hat on 2/4, soft pad.
    const tempo = 96; // BPM
    const beatSec = 60 / tempo;
    const master = ctx.createGain();
    master.gain.value = 0.5;

    musicAnalyser = ctx.createAnalyser();
    musicAnalyser.fftSize = 512;
    musicAnalyser.smoothingTimeConstant = 0.7;
    master.connect(musicAnalyser);
    musicAnalyser.connect(ctx.destination);

    // Pad: low-pass filtered noise + a soft sine drone.
    const padBuf = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
    const data = padBuf.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * 0.6;
    const pad = ctx.createBufferSource();
    pad.buffer = padBuf;
    pad.loop = true;
    const padFilter = ctx.createBiquadFilter();
    padFilter.type = 'lowpass';
    padFilter.frequency.value = 700;
    pad.connect(padFilter); padFilter.connect(master);

    const drone = ctx.createOscillator();
    drone.type = 'sine';
    drone.frequency.value = 110;
    const droneGain = ctx.createGain();
    droneGain.gain.value = 0.15;
    drone.connect(droneGain); droneGain.connect(master);

    pad.start();
    drone.start();

    function scheduleLoop() {
      if (!musicEnabled) return;
      let t0 = ctx.currentTime + 0.05;
      for (let i = 0; i < 16; i++) {
        const when = t0 + i * (beatSec / 2);
        // Kick on 1, 3
        if (i % 4 === 0) {
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.frequency.setValueAtTime(120, when);
          o.frequency.exponentialRampToValueAtTime(40, when + 0.12);
          g.gain.setValueAtTime(0.7, when);
          g.gain.exponentialRampToValueAtTime(0.001, when + 0.18);
          o.connect(g); g.connect(master);
          o.start(when); o.stop(when + 0.2);
        }
        // Hat on 2, 4
        if (i % 4 === 2) {
          const nbuf = ctx.createBuffer(1, ctx.sampleRate * 0.05, ctx.sampleRate);
          const nd = nbuf.getChannelData(0);
          for (let k = 0; k < nd.length; k++) nd[k] = (Math.random() * 2 - 1) * (1 - k / nd.length);
          const ns = ctx.createBufferSource(); ns.buffer = nbuf;
          const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 6000;
          const g  = ctx.createGain(); g.gain.value = 0.18;
          ns.connect(hp); hp.connect(g); g.connect(master);
          ns.start(when);
        }
      }
      musicLoopTimer = setTimeout(scheduleLoop, beatSec * 8 * 1000);
    }
    scheduleLoop();

    musicNodes = { master, pad, drone, padFilter, droneGain };
    musicFreqArr = new Uint8Array(musicAnalyser.frequencyBinCount);
    musicEnabled = true;

    // Notify backend so a Music state event can flow back too (optional).
    try { fetch('/api/state', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ state: 'MUSIC' }) }); } catch (e) {}
  }

  let musicLoopTimer = null;
  function stopMusic() {
    if (!musicEnabled) return;
    musicEnabled = false;
    if (musicLoopTimer) { clearTimeout(musicLoopTimer); musicLoopTimer = null; }
    try {
      if (musicNodes) {
        musicNodes.drone.stop();
        musicNodes.pad.stop();
        musicNodes.master.disconnect();
      }
    } catch (e) { /* ignore */ }
    musicNodes = null;
    musicAnalyser = null;
    musicFreqArr = null;
    try { fetch('/api/state', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ state: 'IDLE' }) }); } catch (e) {}
  }

  function toggleMusic() {
    if (musicEnabled) { stopMusic(); markButton('music', false); }
    else              { startMusic();  markButton('music', musicEnabled); }
  }

  function readBands(arr) {
    // arr is Uint8Array. Split into low/mid/high by bin and normalize to 0..1.
    if (!arr) return { low: 0, mid: 0, high: 0 };
    const n = arr.length;
    const loEnd = Math.floor(n * 0.15);
    const midEnd = Math.floor(n * 0.5);
    let lo = 0, mi = 0, hi = 0;
    for (let i = 0; i < loEnd; i++) lo += arr[i];
    for (let i = loEnd; i < midEnd; i++) mi += arr[i];
    for (let i = midEnd; i < n; i++) hi += arr[i];
    return {
      low:  lo / (loEnd * 255),
      mid:  mi / ((midEnd - loEnd) * 255),
      high: hi / ((n - midEnd) * 255),
    };
  }

  // ------- main loop -----------------------------------------------------

  let lastT = performance.now();
  function loop(now) {
    const dt = Math.min(0.05, (now - lastT) / 1000); // clamp huge gaps
    lastT = now;

    let audio = { low: 0, mid: 0, high: 0 };
    if (musicEnabled && musicAnalyser) {
      musicAnalyser.getByteFrequencyData(musicFreqArr);
      audio = readBands(musicFreqArr);
    } else if (micEnabled && analyser) {
      analyser.getByteFrequencyData(freqArr);
      audio = readBands(freqArr);
    }
    anim.setAudio(audio);

    const params = anim.tick(dt);

    // Mouse attention: only during calm states. Apply on top of the
    // engine's output so we don't fight any active behavior.
    const mt = window.__nexMouseTarget && window.__nexMouseTarget();
    if (mt !== undefined && (anim.state === 'IDLE' || anim.state === 'LISTENING')) {
      const cur = params.lookX || 0;
      params.lookX = cur * 0.85 + mt * 0.15;
    }

    gl.render(params, audio);

    // Dev HUD
    if (dev && !dev.hidden) {
      const info = anim.debugInfo;
      if (devState.textContent !== info.state) devState.textContent = info.state;
      if (devActivity.textContent !== info.activityLevel) devActivity.textContent = info.activityLevel;
      devBehavior.textContent = info.behavior + '  ' + info.behaviorT + '/' + info.behaviorDur;
      devLast.textContent = info.lastBehavior + ' (' + info.lastBehaviorAge + 's)';
      devNext.textContent = info.nextEvalIn + 's';
      devRecent.textContent = info.recentBehaviors;
    }
    requestAnimationFrame(loop);
  }

  // ------- dev panel -----------------------------------------------------

  const STATES = [
    'IDLE','LISTENING','THINKING','SPEAKING','HAPPY','EXCITED','CALM',
    'CONFUSED','FOCUSED','FRUSTRATED','SURPRISED','CURIOUS','AMUSED',
    'SLEEPY','PROUD','SUSPICIOUS','MUSIC','ERROR','RECOVERY','WAKE'
  ];

  function buildDevPanel() {
    devStates.innerHTML = '';
    for (const s of STATES) {
      const b = document.createElement('button');
      b.textContent = s;
      b.dataset.state = s;
      b.addEventListener('click', () => sendState(s));
      devStates.appendChild(b);
    }

    devIdle.innerHTML = '';
    const idleBehaviors = [
      'breathing','blink','microMove','asyncBlink','lookLeft','lookRight',
      'stretch','squish','faceTilt','asymmetric','syncPulse','stillness',
      'rarePulse','slowLook','briefWiden','tinyTilt','oneEyeReact',
      'freeze','subtleShift','unusualStretch','syncEvent','doubleBlink','slowBlink',
    ];
    for (const name of idleBehaviors) {
      const b = document.createElement('button');
      b.textContent = name;
      b.title = 'force idle, then play this behavior';
      b.addEventListener('click', () => {
        sendState('IDLE');
        // Schedule it on the next idle tick.
        setTimeout(() => {
          const bh = anim.behaviors[name];
          if (!bh) return;
          anim.currentBehavior = bh;
          anim.behaviorT = 0;
          anim.behaviorDur = bh.duration;
          anim.behaviorParams = anim._randomParams(bh);
        }, 30);
      });
      devIdle.appendChild(b);
    }

    dev.querySelectorAll('button[data-action]').forEach(b => {
      b.addEventListener('click', () => {
        const a = b.dataset.action;
        if (a === 'mic')        toggleMic();
        else if (a === 'music') toggleMusic();
        else if (a === 'tts') {
          const muted = window.__nexTts && window.__nexTts.toggleMute();
          markButton('tts', !!muted);
        }
        else if (a === 'reset') sendState('IDLE');
        else if (a === 'wake')  sendState('WAKE');
        else if (a === 'error') sendState('ERROR');
        else if (a === 'send')  doSend();
        else if (a === 'settings') window.open('/settings.html', '_blank');
      });
    });

    devInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doSend();
    });

    // --- Autonomous agent: Build / Plan mode (STAGE 19/24) ---
    const agentRow = document.createElement('div');
    agentRow.className = 'agent-row';
    agentRow.appendChild(makeBtn('Build', 'agent', 'build',
      'Autonomous build — sends the input as a goal to the agent (model-driven plan + judges)'));
    agentRow.appendChild(makeBtn('Plan', 'agent', 'plan',
      'Plan only — generate + show a goal-specific plan without executing'));
    const campBtn = makeBtn('Campaign', 'campaign', 'campaign',
      'Long-running autonomous campaign — the model splits the goal into milestones and builds them one by one (checkpoints + resume)');
    agentRow.appendChild(campBtn);
    dev.appendChild(agentRow);

    dev.querySelectorAll('button[data-agent]').forEach(b => {
      b.addEventListener('click', () => runAgent(b.dataset.agent));
    });
    campBtn.addEventListener('click', runCampaign);
  }

  function makeBtn(label, kind, value, title) {
    const b = document.createElement('button');
    b.textContent = label;
    b.dataset[kind] = value;
    b.title = title || '';
    return b;
  }

  function runAgent(mode) {
    const goal = (devInput.value || '').trim();
    if (!goal) { devInput.focus(); return; }
    anim.setState({ state: 'THINKING', params: { source: 'agent' } });
    ensureAgentPanel();
    agentPanel().innerHTML = '<div class="agent-title">Agent (' + mode + ')</div>'
      + '<div class="agent-sub">goal: ' + escapeHtml(goal) + '</div>'
      + '<div class="agent-status">queued…</div>';
    fetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: goal, mode: mode }),
    }).then(r => r.json()).then(j => {
      if (!j || !j.ok) {
        agentPanel().innerHTML += '<div class="agent-status err">'
          + (j && j.error ? j.error : 'failed to start') + '</div>';
      }
    }).catch(e => {
      agentPanel().innerHTML += '<div class="agent-status err">network error</div>';
    });
  }

  function runCampaign() {
    const goal = (devInput.value || '').trim();
    if (!goal) { devInput.focus(); return; }
    anim.setState({ state: 'THINKING', params: { source: 'campaign' } });
    const p = ensureAgentPanel();
    p.innerHTML = '<div class="agent-title">Campaign</div>'
      + '<div class="agent-sub">goal: ' + escapeHtml(goal) + '</div>'
      + '<div class="agent-status">drafting milestone roadmap…</div>';
    fetch('/api/agent/campaign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: goal }),
    }).then(r => r.json()).then(j => {
      if (!j || !j.ok) {
        p.innerHTML += '<div class="agent-status err">'
          + (j && j.error ? j.error : 'failed to start campaign') + '</div>';
      }
    }).catch(() => {
      p.innerHTML += '<div class="agent-status err">network error</div>';
    });
  }

  // Campaign milestone list — one row per milestone with a live progress
  // bar; rows animate in as the campaign advances (agent.milestone_*).
  let campaignRows = null;

  function renderCampaignStart(milestones) {
    const p = ensureAgentPanel();
    campaignRows = {};
    let html = '<div class="agent-title">Campaign milestones</div>';
    html += '<div class="campaign-bar"><i></i></div>';
    (milestones || []).forEach((m, i) => {
      html += '<div class="campaign-ms" data-ms="' + i + '">'
        + '<span class="ms-state">·</span>'
        + '<span class="ms-title">' + escapeHtml(m.title || m.goal || '?')
        + '</span>'
        + '<span class="ms-status">queued</span></div>';
      campaignRows[i] = m.title || m.goal;
    });
    const block = document.createElement('div');
    block.innerHTML = html;
    p.innerHTML = '';
    p.appendChild(block);
  }

  function campaignUpdate(index, status, label) {
    const p = agentPanel();
    const row = p.querySelector('.campaign-ms[data-ms="' + index + '"]');
    if (!row) return;
    const bar = p.querySelector('.campaign-bar > i');
    if (status === 'running') {
      row.className = 'campaign-ms running';
      row.querySelector('.ms-state').textContent = '◐';
      row.querySelector('.ms-status').textContent = label || 'building';
    } else if (status === 'COMPLETED') {
      row.className = 'campaign-ms done';
      row.querySelector('.ms-state').textContent = '✓';
      row.querySelector('.ms-status').textContent = 'done';
      faceCelebrate();
    } else if (status === 'resumed') {
      row.className = 'campaign-ms done';
      row.querySelector('.ms-state').textContent = '✓';
      row.querySelector('.ms-status').textContent = 'resumed';
    } else {
      row.className = 'campaign-ms failed';
      row.querySelector('.ms-state').textContent = '✗';
      row.querySelector('.ms-status').textContent = label || status || 'failed';
    }
    if (bar) {
      const total = p.querySelectorAll('.campaign-ms').length || 1;
      const done = p.querySelectorAll('.campaign-ms.done, .campaign-ms.failed').length;
      bar.style.width = Math.round((done / total) * 100) + '%';
    }
  }

  // Live plan-step progress: agent events carry task ids like "step_N"
  // which map onto the rendered plan list items.
  function markStep(taskId, status) {
    if (!taskId) return;
    const m = /step_(\d+)/.exec(String(taskId));
    if (!m) return;
    const li = agentPanel().querySelector('.agent-steps li[data-step="' + m[1] + '"]');
    if (!li) return;
    li.classList.remove('running', 'done', 'failed');
    li.classList.add('rowIn', status);
  }

  function faceCelebrate() {
    // Shader burst + accent lift ...
    if (anim && anim.celebrate) anim.celebrate();
    // ... plus the DOM glow on the canvas.
    document.body.classList.remove('face-celebrate');
    void document.body.offsetWidth;  // restart the animation
    document.body.classList.add('face-celebrate');
    setTimeout(() => document.body.classList.remove('face-celebrate'), 950);
  }

  function faceTrouble() {
    if (anim && anim.trouble) anim.trouble();
    document.body.classList.remove('face-trouble');
    void document.body.offsetWidth;
    document.body.classList.add('face-trouble');
    setTimeout(() => document.body.classList.remove('face-trouble'), 750);
  }

  // ---- toasts -----------------------------------------------------------
  // ------------------------------------------------------------------
  // Amazon Music renderer. The connector (music_amazon.py, official
  // Web API backend) owns auth, search, queue sessions and the
  // mandatory event reporting; this is only the speaker: it renders
  // the playable the connector hands over, or says honestly when it
  // cannot (DRM streams need a Widevine-capable viewer).
  // ------------------------------------------------------------------
  var amAudio = null;
  function handleAmazonMusic(evt) {
    var action = evt.action || '';
    if (action === 'volume') {
      var vol = (typeof evt.volume === 'number' ? evt.volume : 70) / 100;
      if (amAudio) amAudio.volume = vol;
      showToast('Amazon Music volume: ' + evt.volume + '%', 'ok');
      return;
    }
    if (action === 'pause') {
      if (amAudio) amAudio.pause();
      showToast('Amazon Music: paused'
        + (evt.track && evt.track.title ? ' — ' + evt.track.title : ''), 'ok');
      return;
    }
    if (action === 'play') {
      var p = evt.playable || {};
      var drm = (p.drm_type || '').toUpperCase();
      if (!p.url || drm === 'WIDEVINE' || drm === 'FAIRPLAY'
          || drm === 'PLAYREADY') {
        showToast('Amazon Music: DRM-protected stream — this browser '
          + 'cannot render it; use the Amazon Music app or web player',
          'err');
        return;
      }
      try {
        if (!amAudio) { amAudio = new Audio(); }
        amAudio.src = p.url;
        amAudio.volume = (typeof evt.volume === 'number' ? evt.volume : 70) / 100;
        var pr = amAudio.play();
        if (pr && pr.catch) {
          pr.catch(function () {
            showToast('Amazon Music: browser blocked autoplay — interact '
              + 'with the page once to allow audio', 'err');
          });
        }
        showToast('Amazon Music: '
          + (evt.track && evt.track.title ? evt.track.title
                                          : (p.title || 'playing')), 'ok');
      } catch (e) {
        showToast('Amazon Music: could not start playback', 'err');
      }
    }
  }

  function showToast(msg, kind) {
    let wrap = document.getElementById('toasts');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.id = 'toasts';
      document.body.appendChild(wrap);
    }
    const t = document.createElement('div');
    t.className = 'toast ' + (kind || 'info');
    t.textContent = msg;
    wrap.appendChild(t);
    requestAnimationFrame(() => t.classList.add('in'));
    setTimeout(() => {
      t.classList.remove('in');
      t.classList.add('out');
      setTimeout(() => t.remove(), 400);
    }, 3600);
    // Cap the stack.
    while (wrap.children.length > 4) wrap.firstChild.remove();
  }

  // ---- connection pill ---------------------------------------------------
  let connTimer = null;
  function refreshConnPill() {
    fetch('/api/tunnels').then(r => r.json()).then(d => {
      let pill = document.getElementById('conn-pill');
      if (!pill) {
        pill = document.createElement('button');
        pill.id = 'conn-pill';
        pill.title = 'MCP servers — click to manage';
        pill.addEventListener('click', () =>
          window.open('/settings.html', '_blank'));
        document.body.appendChild(pill);
      }
      const online = d.online || 0, total = d.total || 0;
      pill.innerHTML = '<span class="pill-dot ' + (online > 0 ? 'on' : 'off')
        + '"></span>MCP ' + online + '/' + total;
    }).catch(() => {});
  }
  function startConnPill() {
    refreshConnPill();
    if (connTimer) clearInterval(connTimer);
    connTimer = setInterval(refreshConnPill, 15000);
  }

  // ---- boot hint ---------------------------------------------------------
  function showBootHint() {
    const h = document.createElement('div');
    h.id = 'boot-hint';
    h.textContent = 'press ` for controls';
    document.body.appendChild(h);
    setTimeout(() => h.classList.add('fade'), 3800);
    setTimeout(() => h.remove(), 5600);
  }

  // ---- conversation transcript ---------------------------------------------
  let chatEl = null;
  let currentNexMsg = null;

  function chatLog() {
    if (!chatEl) chatEl = document.getElementById('chat-log');
    if (!chatEl) return null;
    chatEl.hidden = false;
    chatEl.classList.add('active');
    clearTimeout(chatEl._dim);
    chatEl._dim = setTimeout(() => chatEl.classList.remove('active'), 12000);
    return chatEl;
  }

  function addChatUser(text) {
    const log = chatLog();
    if (!log) return;
    currentNexMsg = null;   // a user turn starts a fresh Nex message
    const m = document.createElement('div');
    m.className = 'chat-msg user rowIn';
    m.textContent = text;
    log.appendChild(m);
    trimChat(log);
  }

  function chatNexMsg() {
    const log = chatLog();
    if (!log) return null;
    if (!currentNexMsg) {
      currentNexMsg = document.createElement('div');
      currentNexMsg.className = 'chat-msg nex rowIn';
      currentNexMsg._text = '';
      log.appendChild(currentNexMsg);
      trimChat(log);
    }
    return currentNexMsg;
  }

  function updateChatNex(fullText) {
    const msg = chatNexMsg();
    if (!msg) return;
    const text = String(fullText || '');
    if (!text) return;
    if (text.startsWith(msg._text) && msg._text.length > 0) {
      const suffix = text.slice(msg._text.length);
      if (suffix) {
        const span = document.createElement('span');
        span.className = 'chat-new';
        span.textContent = suffix;
        msg.appendChild(span);
      }
    } else {
      // Non-cumulative chunk: render as its own fading segment.
      const span = document.createElement('span');
      span.className = 'chat-new';
      span.textContent = (msg._text ? ' ' : '') + text;
      msg.appendChild(span);
    }
    msg._text += text.startsWith(msg._text) ? text.slice(msg._text.length) : text;
    trimChat(msg.parentNode);
  }

  function trimChat(log) {
    if (!log) return;
    const msgs = log.querySelectorAll('.chat-msg');
    for (let i = 0; i < msgs.length - 8; i++) msgs[i].remove();
  }

  // ---- help overlay ---------------------------------------------------------
  function toggleHelp(force) {
    const h = document.getElementById('help-overlay');
    if (!h) return;
    const show = force != null ? force : h.hidden;
    h.hidden = !show;
    if (show) {
      requestAnimationFrame(() => h.classList.add('in'));
    } else {
      h.classList.remove('in');
    }
  }

  function bindHelpChrome() {
    const closeBtn = document.querySelector('.help-close');
    if (closeBtn) closeBtn.addEventListener('click', () => toggleHelp(false));
    const overlay = document.getElementById('help-overlay');
    if (overlay) overlay.addEventListener('click', (e) => {
      if (e.target === overlay) toggleHelp(false);
    });
  }

  // ---- typing indicator ---------------------------------------------------
  let typingEl = null;
  function showTyping() {
    if (typingEl) return;
    typingEl = document.createElement('div');
    typingEl.id = 'typing';
    typingEl.innerHTML = '<i></i><i></i><i></i>';
    document.body.appendChild(typingEl);
  }
  function hideTyping() {
    if (!typingEl) return;
    typingEl.remove();
    typingEl = null;
  }

  function agentPanel() {
    let p = document.getElementById('agent-panel');
    if (!p) {
      p = document.createElement('div');
      p.id = 'agent-panel';
      p.hidden = false;
      document.body.appendChild(p);
    }
    return p;
  }

  function ensureAgentPanel() {
    const p = agentPanel();
    p.hidden = false;
    // Persistent collapse toggle (re-added if innerHTML was wiped).
    if (!p.querySelector('.panel-toggle')) {
      const t = document.createElement('button');
      t.className = 'panel-toggle';
      t.textContent = p.classList.contains('collapsed') ? '+' : '–';
      t.title = 'collapse panel';
      t.addEventListener('click', () => {
        p.classList.toggle('collapsed');
        t.textContent = p.classList.contains('collapsed') ? '+' : '–';
      });
      p.appendChild(t);
    }
    return p;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function renderAgentPlan(plan) {
    const p = ensureAgentPanel();
    const steps = (plan && plan.steps) || [];
    let html = '<div class="agent-title">Plan' + (plan && plan.title ? ' — ' + escapeHtml(plan.title) : '') + '</div>';
    html += '<ol class="agent-steps">';
    let idx = 0;
    for (const s of steps) {
      html += '<li data-step="' + (s._idx != null ? s._idx : idx++) + '"><b>' + escapeHtml(s.name || '?') + '</b> '
        + '<code>' + escapeHtml(s.tool || '') + '</code>'
        + (s.depends_on && s.depends_on.length ? ' <i>← ' + escapeHtml(s.depends_on.join(', ')) + '</i>' : '')
        + (s.why ? '<br><span class="agent-why">' + escapeHtml(s.why) + '</span>' : '')
        + '</li>';
    }
    html += '</ol>';
    // Insert plan after the title block, keep the goal/status above.
    const head = p.querySelector('.agent-title');
    const sub = p.querySelector('.agent-sub');
    const status = p.querySelector('.agent-status');
    const block = document.createElement('div');
    block.innerHTML = html;
    if (status && status.nextSibling) p.insertBefore(block, status.nextSibling);
    else if (status) status.after(block);
    else p.appendChild(block);
  }

  function renderAgentVerdict(v) {
    const p = ensureAgentPanel();
    const llm = (v && v.judges && v.judges.llm) || null;
    const score = (v && v.score != null) ? Math.round(v.score * 100) : null;
    let html = '<div class="agent-title">Judge verdict'
      + (score != null ? ' — ' + score + '%' : '') + '</div>';
    html += '<div class="agent-verdict verdict-' + ((v && v.pass) ? 'pass' : 'fail') + '">'
      + ((v && v.pass) ? 'PASSED quality bar' : 'below quality bar — improving…') + '</div>';
    if (llm) {
      html += '<div class="agent-scores">'
        + 'fun ' + (llm.fun || '-') + ' · quality ' + (llm.quality || '-')
        + ' · playability ' + (llm.playability || '-') + '</div>';
    }
    const dims = (v && v.dimensions) || {};
    const labels = { core_loop: 'core loop', feedback: 'feedback', art: 'art',
      audio: 'audio', progression: 'progression', polish: 'polish' };
    html += '<div class="agent-dims">';
    for (const k in labels) {
      if (dims[k] != null) {
        html += '<span class="dim ' + (dims[k] ? 'on' : 'off') + '">' + labels[k] + '</span>';
      }
    }
    html += '</div>';
    const sugg = (v && v.suggestions) || [];
    if (sugg.length) {
      html += '<div class="agent-sugg"><b>Improve:</b><ul>';
      for (const s of sugg.slice(0, 5)) html += '<li>' + escapeHtml(s) + '</li>';
      html += '</ul></div>';
    }
    const block = document.createElement('div');
    block.innerHTML = html;
    p.appendChild(block);
  }

  function renderAgentReport(report) {
    const p = ensureAgentPanel();
    const r = report || {};
    let html = '<div class="agent-title">Build result — ' + escapeHtml(r.status || '?') + '</div>';
    html += '<div class="agent-status">';
    html += '✓ ' + (r.completed ? r.completed.length : 0)
      + ' completed · ✗ ' + (r.failed ? r.failed.length : 0)
      + ' failed · ⊘ ' + (r.skipped ? r.skipped.length : 0) + '</div>';
    if (r.failed && r.failed.length) {
      html += '<div class="agent-failed">Failed: ' + escapeHtml(r.failed.join(', ')) + '</div>';
    }
    if (r.missing && r.missing.length) {
      html += '<div class="agent-missing">Missing capability: '
        + escapeHtml(r.missing.map(m => (m.tool || m.task)).join(', ')) + '</div>';
    }
    const block = document.createElement('div');
    block.innerHTML = html;
    p.appendChild(block);
  }

  function sendState(name) {
    try {
      fetch('/api/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: name }),
      }).catch(() => {});
    } catch (e) {}
    anim.setState({ state: name });
  }

  function doSend() {
    const text = (devInput.value || '').trim();
    if (!text) return;
    devInput.value = '';
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    }).catch(() => {});
    anim.setState({ state: 'LISTENING' });
    showTyping();
    setTimeout(hideTyping, 30000);  // safety net
    addChatUser(text);
  }

  function markButton(name, on) {
    dev.querySelectorAll(`button[data-action="${name}"]`).forEach(b => {
      b.classList.toggle('active', !!on);
    });
  }

  function bindKeyboard() {
    window.addEventListener('keydown', (e) => {
      if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
      if (e.key === '`' || e.key === '~') {
        dev.hidden = !dev.hidden;
        return;
      }
      if (e.key === 'Escape') {
        dev.hidden = true;
        toggleHelp(false);
        return;
      }
      if (e.key === '?' && dev.hidden) {
        e.preventDefault();
        toggleHelp();
        return;
      }
      // Number keys 1..9 jump to states.
      const map = ['IDLE','LISTENING','THINKING','SPEAKING','HAPPY','EXCITED','CONFUSED','FOCUSED','FRUSTRATED'];
      const idx = parseInt(e.key, 10) - 1;
      if (idx >= 0 && idx < map.length) sendState(map[idx]);
      if (e.key.toLowerCase() === 'm') toggleMusic();
      if (e.key.toLowerCase() === 'r') sendState('IDLE');
      if (e.key.toLowerCase() === 'e') sendState('ERROR');
      if (e.key.toLowerCase() === 'w') sendState('WAKE');
      if (e.key.toLowerCase() === 's') sendState('SURPRISED');
    });
  }

  function bindMouse() {
    // Subtle reaction to mouse position: a tiny horizontal attention.
    let targetX = 0;
    window.addEventListener('mousemove', (e) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      targetX = nx * 0.25; // small
    });
    // We don't need a separate timer — apply the mouse nudge inside the
    // main rAF loop after the animation engine ticks. (Done in loop().)
    window.__nexMouseTarget = () => targetX;
  }

  function bindNetwork() {
    if (!('EventSource' in window)) return;
    es = new EventSource('/api/events');
    es.onmessage = (m) => {
      let evt;
      try { evt = JSON.parse(m.data); } catch (e) { return; }
      if (!evt || !evt.type) return;
      if (evt.type === 'state') {
        anim.setState({ state: evt.state, params: evt.params || {} });
        // Cancel any in-flight TTS when we leave SPEAKING — keeps a
        // previous reply's last word from bleeding into the next turn.
        if (evt.state !== 'SPEAKING' && evt.state !== 'THINKING' &&
            window.__nexTts) {
          window.__nexTts.cancel();
        }
      } else if (evt.type === 'speak') {
        // Legacy non-streaming speak event. The backend strips
        // [STATE] tags before sending. We accept either `tts_text`
        // (preferred, fully cleaned) or `text` (legacy / direct POST).
        anim.setState({ state: 'SPEAKING', params: {} });
        hideTyping();
        const shown = (evt.tts_text != null ? evt.tts_text : evt.text) || '';
        anim.setSpeechText(stripTagsClient(shown));
        updateChatNex(stripTagsClient(shown));
      } else if (evt.type === 'speak.delta') {
        hideTyping();
        updateChatNex(stripTagsClient(evt.text || ''));
        // Streaming: the bubble updates with each safe-boundary chunk.
        // The backend sends a complete, tag-stripped chunk. `text` is
        // the cumulative cleaned text; we use it directly so we never
        // have to track local accumulation state.
        anim.setSpeechText(stripTagsClient(evt.text || ''));
        // If this delta carries an emotion tag (parsed at the same
        // boundary as this text), apply it IN THE SAME FRAME so the
        // face animates synchronously with the bubble, not "before"
        // (visible as a head-start) or "after" (visible as a tail-flick).
        if (evt.state) {
          anim.setState({
            state: evt.state,
            params: { source: evt.stateSource || 'tag' },
          });
        }
        // Speak this delta the moment it arrives so TTS starts within
        // ~150ms of the first chunk instead of waiting for the full
        // reply. The flusher only emits at safe boundaries so each
        // delta is a coherent chunk of speech — no mid-word cut.
        // Skip empty deltas (state-only carryovers) so we don't queue
        // silent TTS utterances.
        if (evt.delta && window.__nexTts) {
          window.__nexTts.speak(stripTagsClient(evt.delta));
        }
      } else if (evt.type === 'speak.end') {
        // Final event for the stream. The last delta already populated
        // the bubble. We apply the emotion here ONLY if a delta didn't
        // already carry one — so a trailing emotion that arrived with
        // an empty text chunk still gets delivered.
        if (evt.state) {
          anim.setState({
            state: evt.state,
            params: { source: evt.stateSource || 'fallback' },
          });
        } else if (evt.tags && evt.tags.length > 0) {
          // A trailing tag arrived with no preceding text (model wrote
          // `[PROUD]` as the very last thing before stream end).
          // Apply it now so the face catches up.
          anim.setState({
            state: evt.tags[evt.tags.length - 1],
            params: { source: 'tag' },
          });
        } else {
          anim.setState({ state: 'SPEAKING', params: {} });
        }
      } else if (evt.type === 'emotion') {
        // Optional explicit emotion event from the backend.
        if (evt.state) anim.setState({ state: evt.state, params: {} });
      } else if (evt.type === 'agent' || evt.type === 'agent.report' || evt.type === 'agent.error') {
        // Semantic autonomous-agent events (STAGE 20/21). The frontend does
        // NOT need MCP internals — it only maps the coarse agent lifecycle
        // state onto an existing face state. Calm idle is preserved; the
        // agent's activity is shown as a RICH EVENT, not a new idle loop.
        if (evt.agentState) {
          var AGENT_FACE = {
            PLANNING: 'THINKING', OBSERVING: 'LISTENING',
            EXECUTING: 'FOCUSED', VERIFYING: 'SCAN',
            REPAIRING: 'CONFUSED', WAITING: 'CALM',
            COMPLETED: 'PROUD', BLOCKED: 'SUSPICIOUS', ERROR: 'ERROR'
          };
          var face = AGENT_FACE[evt.agentState];
          if (face) anim.setState({ state: face, params: { source: 'agent' } });
          if (evt.agentState === 'EXECUTING' && anim.sweepOnce) anim.sweepOnce(2.2);
          if (evt.agentState === 'PLANNING' && anim.sweepOnce) anim.sweepOnce(1.4);
          if (evt.agentState === 'COMPLETED' && anim.celebrate) anim.celebrate();
          if (evt.agentState === 'BLOCKED' && anim.trouble) anim.trouble();
        }
        // Surface a short status line in the debug panel when available.
        if (dev && !dev.hidden && evt.agentState) {
          devState.textContent = 'AGENT:' + evt.agentState;
        }
      } else if (evt.type === 'plan.submitted') {
        // A model reply contained a JSON plan. Show a confirm banner
        // so the user can approve / cancel destructive steps.
        showPlanBanner(evt.plan);
      } else if (evt.type === 'agent.mcp_connected') {
        showToast('MCP server connected: ' + (evt.server || 'server'), 'ok');
        refreshConnPill();
      } else if (evt.type === 'agent.mcp_disconnected') {
        showToast('MCP server lost: ' + (evt.server || 'server'), 'err');
        refreshConnPill();
      } else if (evt.type === 'agent.campaign_started') {
        renderCampaignStart(evt.milestones);
      } else if (evt.type === 'agent.milestone_started') {
        campaignUpdate(evt.index, 'running',
          'milestone ' + (evt.milestone || '?') + '/' + (evt.of || '?'));
        anim.setState({ state: 'FOCUSED', params: { source: 'campaign' } });
      } else if (evt.type === 'agent.milestone_completed') {
        campaignUpdate(evt.index, evt.status || evt.result && evt.result.status,
          evt.status === 'COMPLETED' ? 'done' : (evt.status || 'failed'));
        if (evt.status !== 'COMPLETED') faceTrouble();
      } else if (evt.type === 'agent.campaign_done') {
        const s = evt.summary || {};
        const p = ensureAgentPanel();
        p.innerHTML += '<div class="agent-title">Campaign result</div>'
          + '<div class="agent-status">' + s.milestones_completed + '/'
          + s.milestones_total + ' milestones completed — '
          + escapeHtml(s.status || '?') + '</div>';
        if (s.status === 'COMPLETED') {
          anim.setState({ state: 'PROUD', params: { source: 'campaign' } });
          faceCelebrate();
        } else {
          faceTrouble();
        }
      } else if (evt.type === 'agent.verification_passed') {
        // Light-touch flourish: sparkle line in the panel + face flash.
        const p = ensureAgentPanel();
        const tick = document.createElement('div');
        tick.className = 'agent-status rowIn';
        tick.innerHTML = '<span class="verify-tick">✓</span>verified '
          + escapeHtml(evt.task || '');
        p.appendChild(tick);
        if (anim && anim.flash) anim.flash('#7be0a0', 0.3, 0.7);
        markStep(evt.task, 'done');
      } else if (evt.type === 'agent.task_started') {
        markStep(evt.task, 'running');
      } else if (evt.type === 'agent.tool_failed') {
        const p = agentPanel();
        p.classList.remove('shake');
        void p.offsetWidth;
        p.classList.add('shake');
        markStep(evt.task, 'failed');
      } else if (evt.type === 'agent.plan_ready') {
        // The agent produced a goal-specific plan (model-driven or
        // capability fallback). Show it in the agent panel.
        const p = ensureAgentPanel();
        const st = p.querySelector('.agent-status');
        if (st) st.textContent = (evt.model_driven ? 'model-driven plan ready' : 'capability plan ready')
          + (evt.revise ? ' (revised)' : '');
        if (evt.steps) renderAgentPlan({ title: evt.title, steps: evt.steps });
      } else if (evt.type === 'agent.judged') {
        // Quality judges scored the build. Render the verdict.
        if (evt.verdict) renderAgentVerdict(evt.verdict);
      } else if (evt.type === 'agent.report') {
        // Final build result.
        const res = evt.result || {};
        if (res.report) renderAgentReport(res.report);
        else if (res.mode === 'plan') renderAgentPlan(res);
      } else if (evt.type === 'agent.error') {
        const p = ensureAgentPanel();
        p.innerHTML += '<div class="agent-status err">agent error: '
          + escapeHtml(evt.error || 'unknown') + '</div>';
      } else if (evt.type === 'agent.design_started') {
        // NEX 2.0: Nex is thinking about WHAT to build before any tool call.
        anim.setState({ state: 'FOCUSED', params: { source: 'design' } });
        showToast('Nex is designing the project — understanding before building', 'ok');
      } else if (evt.type === 'agent.design_ready') {
        // The Plan Page: inspect the structured design + draft plan.
        renderPlanPage(evt);
        anim.setState({ state: 'PROUD', params: { source: 'design' } });
      } else if (evt.type === 'agent.design_failed') {
        showToast('Design stage: ' + (evt.error || evt.note || 'failed')
          + ' — Nex can still build without a design doc', 'err');
        anim.setState({ state: 'CONFUSED', params: { source: 'design' } });
      } else if (evt.type === 'agent.critique') {
        // The critic verdict: PASS / WEAK + findings + next action.
        renderCritique(evt);
        anim.setState({ state: evt.verdict === 'PASS' ? 'PROUD' : 'CONFUSED',
                        params: { source: 'critique' } });
      } else if (evt.type === 'agent.improve_started') {
        anim.setState({ state: 'FOCUSED', params: { source: 'polish' } });
        showToast('Nex is ' + (evt.action === 'REPLAN' ? 'replanning'
          : 'polishing') + ' (critique cycle ' + (evt.cycle || 1) + ')', 'ok');
      } else if (evt.type === 'agent.design_guard') {
        showToast('Design guard: ' + (evt.blocked || 0)
          + ' step(s) blocked — they contradicted a LOCKED design decision', 'err');
      } else if (evt.type === 'amazon_music') {
        // The Amazon Music connector (official Web API backend) hands
        // us the playable; this is only the speaker.
        handleAmazonMusic(evt);
      } else if (evt.type === 'hello') {
        // no-op; backend announces its config here.
      }
    };

  // ------------------------------------------------------------------
  // NEX 2.0 — THE PLAN PAGE. A real page (not a text blob): the
  // structured Design Document + the draft plan, inspectable before
  // Nex touches anything. START BUILD approves via /api/project/<id>/build.
  // ------------------------------------------------------------------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function renderPlanPage(evt) {
    var design = evt.design || {};
    var plan = evt.plan || {};
    var old = document.getElementById('nexPlanPage');
    if (old) old.remove();

    var page = el('div', 'nex-plan-page');
    page.id = 'nexPlanPage';

    var card = el('div', 'nex-plan-card');
    card.appendChild(el('div', 'nex-plan-kicker',
      'NEX · DESIGN DOCUMENT' + (evt.project_id ? ' · ' + evt.project_id : '')));
    card.appendChild(el('h1', 'nex-plan-title',
      design.concept || evt.goal || 'Untitled project'));
    var sub = [design.genre, design.engine].filter(Boolean).join(' · ');
    if (sub) card.appendChild(el('div', 'nex-plan-sub', sub));

    function section(label) {
      var s = el('div', 'nex-plan-section');
      s.appendChild(el('div', 'nex-plan-label', label));
      return s;
    }
    function list(items, cls) {
      var ul = el('ul', 'nex-plan-list' + (cls ? ' ' + cls : ''));
      (items || []).forEach(function (it) {
        var name = (typeof it === 'string') ? it
          : (it && (it.name || it.decision)) || '';
        if (name) ul.appendChild(el('li', null, name));
      });
      return ul;
    }

    if (design.design_pillars && design.design_pillars.length) {
      var pil = section('DESIGN PILLARS');
      pil.appendChild(list(design.design_pillars));
      card.appendChild(pil);
    }
    if (design.gameplay_loop) {
      var loop = section('CORE LOOP');
      loop.appendChild(el('div', 'nex-plan-loop', design.gameplay_loop));
      card.appendChild(loop);
    }
    if (design.mechanics && design.mechanics.length) {
      var sys = section('SYSTEMS');
      sys.appendChild(list(design.mechanics, 'checks'));
      card.appendChild(sys);
    }
    if (design.world && (design.world.summary || (design.world.locations || []).length)) {
      var w = section('WORLD');
      if (design.world.summary) w.appendChild(el('div', null, design.world.summary));
      if (design.world.locations.length) w.appendChild(list(design.world.locations));
      card.appendChild(w);
    }
    if (design.milestones && design.milestones.length) {
      var ms = section('MILESTONES');
      design.milestones.forEach(function (m, i) {
        var row = el('div', 'nex-plan-milestone');
        row.appendChild(el('div', 'nex-plan-ms-name',
          'MILESTONE ' + String(i + 1).padStart(2, '0') + ' — ' + (m.name || '')));
        if (m.tasks && m.tasks.length) row.appendChild(list(m.tasks));
        ms.appendChild(row);
      });
      card.appendChild(ms);
    }
    if (design.quality_gates && design.quality_gates.length) {
      var qg = section('QUALITY GATES');
      qg.appendChild(list(design.quality_gates.map(function (g) {
        return (g && g.done ? '✓ ' : '○ ') + (g.name || '');
      })));
      card.appendChild(qg);
    }
    if (design.dependencies && design.dependencies.length) {
      var dep = section('DEPENDENCIES (honest gaps)');
      dep.appendChild(list(design.dependencies));
      card.appendChild(dep);
    }
    if (plan.steps && plan.steps.length) {
      var ps = section('DRAFT PLAN — ' + plan.steps.length + ' step(s)'
        + (plan.model_driven ? ' (model-driven, validated against live MCP)'
                             : ' (capability skeleton)'));
      ps.appendChild(list(plan.steps.map(function (s) {
        return (s.name || s.tool || '?');
      })));
      card.appendChild(ps);
    }

    var actions = el('div', 'nex-plan-actions');
    var start = el('button', 'nex-plan-start', 'START BUILD');
    start.addEventListener('click', function () {
      if (!evt.project_id) {
        showToast('No persisted project to build — run the design stage first', 'err');
        return;
      }
      start.disabled = true;
      start.textContent = 'BUILDING…';
      fetch('/api/project/' + encodeURIComponent(evt.project_id) + '/build',
        { method: 'POST' }).then(function () {
          page.remove();
          showToast('Nex is building: ' + (design.concept || evt.goal), 'ok');
        }).catch(function () {
          start.disabled = false;
          start.textContent = 'START BUILD';
          showToast('Could not start the build', 'err');
        });
    });
    actions.appendChild(start);
    var close = el('button', 'nex-plan-close', 'CLOSE');
    close.addEventListener('click', function () { page.remove(); });
    actions.appendChild(close);
    card.appendChild(actions);
    page.appendChild(card);
    document.body.appendChild(page);
    page.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') page.remove();
    });
  }

  // ------------------------------------------------------------------
  // The critic readout: PASS/WEAK + findings, in the work panel.
  // ------------------------------------------------------------------
  function renderCritique(evt) {
    var p = ensureAgentPanel();
    var box = el('div', 'agent-critique '
      + (evt.verdict === 'PASS' ? 'critique-pass' : 'critique-weak'));
    box.appendChild(el('div', 'agent-critique-head',
      'CRITIQUE CYCLE ' + (evt.cycle || 1) + ' — '
      + (evt.verdict === 'PASS' ? 'GOOD' : 'WEAK')
      + ' · NEXT: ' + (evt.action || '')));
    if (evt.note) box.appendChild(el('div', 'agent-critique-note', evt.note));
    (evt.findings || []).slice(0, 5).forEach(function (f) {
      box.appendChild(el('div', 'agent-critique-finding',
        (f.severity === 'major' ? '⚠ ' : '· ') + (f.message || '')));
    });
    p.appendChild(box);
    p.classList.remove('collapsed');
  }

  function showPlanBanner(plan) {
    // Minimal in-page banner. Built on top of the existing #bubble
    // element so we don't need a stylesheet change. Reusing the
    // bubble keeps the visual style consistent with the rest of
    // the chat surface.
    //
    // UNIFIED PIPELINE: "Approve + run" hands the plan to the ONE
    // canonical execution path (/autonomous → live-registry validation →
    // TaskGraph → AutonomousAgent with policy/verification/recovery).
    // The old sequential executor is kept as an explicit "step mode"
    // fallback for manual step-through.
    if (!plan || !plan.id) return;
    const confirmed = !plan.needs_confirmation;
    const stepCount = plan.step_count || 0;
    const destructive = (plan.classifications || []).filter(
        c => c === 'destructive').length;
    const summary = (plan.title || 'plan') + " · "
                  + stepCount + " step" + (stepCount === 1 ? '' : 's')
                  + (destructive
                     ? " · " + destructive + " destructive"
                     : "")
                  + (confirmed
                     ? " (safe — auto-running)"
                     : " (confirm to run)");
    const bubble = document.getElementById('bubble');
    if (!bubble) return;
    bubble.textContent = "";

    function showValidationErrors(res) {
      bubble.textContent = "";
      const errs = (res && res.validation_errors) || [res && res.error || 'run failed'];
      const head = document.createElement('div');
      head.textContent = 'Plan rejected before execution:';
      bubble.appendChild(head);
      for (const e of errs.slice(0, 4)) {
        const d = document.createElement('div');
        d.textContent = '· ' + e;
        bubble.appendChild(d);
      }
      setTimeout(() => { bubble.hidden = true; }, 8000);
    }

    async function runAutonomous(afterConfirm) {
      if (afterConfirm) {
        await fetch('/api/plan/' + plan.id + '/confirm', { method: 'POST' });
      }
      const r = await fetch('/api/plan/' + plan.id + '/autonomous',
                            { method: 'POST' });
      const res = await r.json().catch(() => ({}));
      if (r.status === 202) {
        bubble.textContent = 'Plan handed to the autonomous pipeline '
          + '(validated → task graph → verify). Watch the agent panel.';
        setTimeout(() => { bubble.hidden = true; }, 4000);
      } else {
        showValidationErrors(res);
      }
    }

    async function runStepMode(afterConfirm) {
      if (afterConfirm) {
        await fetch('/api/plan/' + plan.id + '/confirm', { method: 'POST' });
      }
      await fetch('/api/plan/' + plan.id + '/execute',
                  { method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stop_on_error: false }) });
      bubble.hidden = true;
    }

    const btnRow = document.createElement('div');
    btnRow.style.marginTop = '6px';
    const line = document.createElement('div');
    line.textContent = summary;
    bubble.appendChild(line);
    if (!confirmed) {
      const approve = document.createElement('button');
      approve.textContent = 'Approve + run';
      approve.onclick = () => runAutonomous(true);
      const step = document.createElement('button');
      step.textContent = 'Step mode';
      step.style.marginLeft = '6px';
      step.onclick = () => runStepMode(true);
      const cancel = document.createElement('button');
      cancel.textContent = 'Cancel';
      cancel.style.marginLeft = '6px';
      cancel.onclick = async () => {
        await fetch('/api/plan/' + plan.id + '/cancel',
                    { method: 'POST' });
        bubble.hidden = true;
      };
      btnRow.appendChild(approve);
      btnRow.appendChild(step);
      btnRow.appendChild(cancel);
      bubble.appendChild(btnRow);
    } else {
      // All-safe plan — kick off immediately so the user doesn't have
      // to click. The model has been told not to include destructive
      // steps in a no-confirm plan, so this is safe.
      runAutonomous(false);
      const step = document.createElement('button');
      step.textContent = 'Step mode instead';
      step.onclick = () => runStepMode(false);
      btnRow.appendChild(step);
      bubble.appendChild(btnRow);
    }
    bubble.hidden = false;
  }
    es.onerror = () => {
      // Will auto-retry; do nothing.
    };
  }

  // ------- audio auto-start hint -----------------------------------------

  function startAudio() {
    // We do NOT auto-request mic — privacy. Instead, watch for first
    // user gesture, and if music mode is requested, that starts audio.
    // The mic is started only on user action (toggleMic).
  }

  // ------- go ------------------------------------------------------------

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();