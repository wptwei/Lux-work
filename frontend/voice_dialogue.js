() => {
  if (document.documentElement.dataset.luxVoiceDialogueBound === '1') return;
  document.documentElement.dataset.luxVoiceDialogueBound = '1';

  // 里程碑 C/D：唤醒 → 聆听 → 转写 → 思考 → 播报 → 追问 的闭环编排。
  // 只负责编排：PCM 转写走 /lux-voice-dialogue/transcribe，聊天走现有发送流程（复用
  // 上下文/检索/权限/输出检查），播报走 window.LUX_TTS（voice_output.js 暴露）。
  // 听写模式（voice_input.js）不受影响，继续只填草稿、不自动发送。
  const cfg = {
    pauseMs: Number((window.LUX_VOICE_DIALOGUE || {}).pauseMs || 800),
    silenceTimeoutMs: Number((window.LUX_VOICE_DIALOGUE || {}).silenceTimeoutMs || 8000),
    maxTurnMs: Number((window.LUX_VOICE_DIALOGUE || {}).maxTurnMs || 20000),
    minSpeechMs: Number((window.LUX_VOICE_DIALOGUE || {}).minSpeechMs || 400),
    energyThreshold: Number((window.LUX_VOICE_DIALOGUE || {}).energyThreshold || 0.02),
    followupMs: Number((window.LUX_VOICE_DIALOGUE || {}).followupMs || 10000),
  };
  const PRE_ROLL_MS = 1000;             // 语音起点前保留的预滚，避免丢掉开头一瞬
  const DUPLICATE_WINDOW_MS = 3000;     // 同一句在极短时间内重复到达才视为重复事件
  const TRANSCRIBE_TIMEOUT_MS = 30000;  // ASR 兜底超时：网络中断时界面能恢复
  const THINKING_TIMEOUT_MS = 120000;   // 模型回答兜底超时
  const SPEAKING_START_TIMEOUT_MS = 70000; // 合成失败/被拒时按纯文字处理（voice_output 60s 先超时）
  const WAKE_FALLBACK_MS = 1200;        // TTS 未就绪时，等待提示音播完再进入聆听
  const WAKE_READY_FALLBACK_MS = 6000;  // TTS 就绪但迟迟没有播放事件时的兜底
  const WAKE_SAFETY_MS = 15000;         // 已在播放但 ended 迟迟不来时的兜底

  const resolveUrl = (p) => (/^https?:\/\//i.test(p) ? p : new URL(p, window.location.origin).href);
  // 与 voice_output.js 共用浏览器会话标识（cookie），服务端据此按设备隔离。
  // 两脚本加载顺序不保证，延迟取一次即可。
  const sessionId = () => (window.LUX_TTS && window.LUX_TTS.sessionId) || '';
  const authHeaders = () => {
    const sid = sessionId();
    return sid ? { 'Content-Type': 'application/json', 'X-Lux-Voice-Session': sid } : { 'Content-Type': 'application/json' };
  };

  // phase: idle | waking | listening | transcribing | thinking | speaking | followup
  let phase = 'idle';
  let assistantOn = false;
  let gen = 0;                 // 代际号：打断/新一轮唤醒时 +1，迟到/过期结果全部丢弃
  let lastSubmit = { text: '', at: 0 };
  let statusText = '';
  let sampleRate = 48000;

  // VAD / 录音缓冲
  let vad = null;
  let preRoll = [];            // 最近约 PRE_ROLL_MS 的 Float32Array 片段
  let preRollSamples = 0;
  let turnChunks = [];         // 语音起点（含预滚）到结束的片段
  let turnSamples = 0;
  let listeningStart = 0;
  let transcribeCtl = null;    // 在途 ASR 请求：打断/关闭时立即取消

  const PHASE_TEXT = {
    idle: '',
    waking: '我在，请说',
    listening: '正在聆听…',
    transcribing: '正在识别…',
    thinking: '正在思考…',
    speaking: '正在说话…',
    followup: '可继续追问…',
  };

  const host = () => document.querySelector('#lux-voice-dialogue-host');

  const setStatus = (text) => { statusText = text || ''; render(); };
  const setPhase = (next) => {
    phase = next;
    statusText = PHASE_TEXT[next] || '';
    // 仅在本脚本正管理一轮对话时抑制 voice_output 的“自动朗读”，避免双播；
    // 待唤醒（idle）时的手动文字发送仍按原有自动朗读逻辑处理。
    document.documentElement.dataset.luxVoiceDialogueActive = (next !== 'idle') ? '1' : '0';
    render();
  };

  const render = () => {
    const h = host();
    if (!h) return;
    const s = h.querySelector('.vd-status');
    if (s) { s.textContent = statusText; s.classList.toggle('is-error', /失败|中断|超时|无法|请先登录/.test(statusText)); }
    const btn = h.querySelector('.vd-stop');
    if (btn) btn.style.display = assistantOn ? '' : 'none';
  };

  const injectControls = () => {
    const h = host();
    if (!h) return;
    h.classList.add('is-ready');
    if (!document.querySelector('#lux-voice-dialogue-styles')) {
      const st = document.createElement('style');
      st.id = 'lux-voice-dialogue-styles';
      st.textContent = [
        '#lux-voice-dialogue-host{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0;min-height:30px;}',
        '#lux-voice-dialogue-host .vd-stop{font-size:13px;padding:4px 12px;border-radius:6px;',
        '  border:1px solid var(--border-color,#3a3f4b);background:var(--bg-secondary,transparent);',
        '  color:var(--text-primary,inherit);cursor:pointer;}',
        '#lux-voice-dialogue-host .vd-status{font-size:12px;opacity:.85;}',
        '#lux-voice-dialogue-host .vd-status.is-error{color:#e5484d;}',
      ].join('');
      document.head.appendChild(st);
    }
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'vd-stop';
    stopBtn.textContent = '停止 / 打断';
    stopBtn.style.display = 'none';
    stopBtn.addEventListener('click', () => interrupt());
    const status = document.createElement('span');
    status.className = 'vd-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    h.append(stopBtn, status);
    render();
  };

  // ---- VAD / 录音 ----
  const resetVad = () => {
    vad = { speech: false, onset: 0, lastSpeech: 0, speechSamples: 0 };
    preRoll = []; preRollSamples = 0;
    turnChunks = []; turnSamples = 0;
  };

  const computeRms = (samples) => {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / Math.max(1, samples.length));
  };

  const updatePreRoll = (samples, rate) => {
    preRoll.push(samples);
    preRollSamples += samples.length;
    const cap = Math.floor(rate * PRE_ROLL_MS / 1000);
    while (preRollSamples > cap && preRoll.length) {
      const first = preRoll[0];
      if (preRollSamples - first.length >= cap) {
        preRollSamples -= first.length;
        preRoll.shift();
      } else {
        const excess = preRollSamples - cap;
        preRoll[0] = first.subarray(excess);
        preRollSamples -= excess;
        break;
      }
    }
  };

  const concatTurn = () => {
    if (!turnChunks.length) return new Float32Array(0);
    const out = new Float32Array(turnSamples);
    let offset = 0;
    for (const chunk of turnChunks) { out.set(chunk, offset); offset += chunk.length; }
    return out;
  };

  const encodePcm = (samples) => {
    const bytes = new Uint8Array(samples.length * 2);
    const view = new DataView(bytes.buffer);
    for (let i = 0; i < samples.length; i++) {
      const v = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(i * 2, Math.round(v * (v < 0 ? 32768 : 32767)), true);
    }
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    }
    return btoa(binary);
  };

  // ---- 时间管理 ----
  const timers = {};
  const clearTimers = () => {
    Object.keys(timers).forEach((k) => { if (timers[k]) { clearTimeout(timers[k]); timers[k] = null; } });
  };
  const abortTranscribe = () => {
    if (transcribeCtl) { try { transcribeCtl.abort(); } catch { /* ignore */ } transcribeCtl = null; }
  };

  const rearm = () => {
    abortTranscribe();
    clearTimers();
    resetVad();
    setPhase('idle');
    document.dispatchEvent(new CustomEvent('lux-voice:rearm'));
  };

  // 打断/关闭：先让在途结果失效（gen+1），再停播与取消请求，最后回到待唤醒。
  const interrupt = () => {
    gen += 1;
    phase = 'idle';                     // 先置空闲：stop() 同步派发的 playback 事件不会再进追问
    abortTranscribe();
    if (window.LUX_TTS && window.LUX_TTS.stop) window.LUX_TTS.stop();
    clearTimers();
    resetVad();
    setPhase('idle');
    setStatus('');
    document.dispatchEvent(new CustomEvent('lux-voice:rearm'));
  };

  const enterListening = () => {
    resetVad();
    listeningStart = performance.now();
    setPhase('listening');
  };

  // 追问窗口从“实际播放结束”开始计时（ended/error/blocked 或纯文字回退时调用）。
  const enterFollowup = (msg, token) => {
    if (token !== undefined && token !== gen) return;   // 过期轮次不重开窗口
    clearTimeout(timers.followup);
    resetVad();
    setPhase('followup');
    if (msg) setStatus(msg);
    timers.followup = setTimeout(() => {
      if (phase === 'followup') { setStatus('返回待唤醒，喊“小安小安”开始。'); rearm(); }
    }, cfg.followupMs);
  };

  // ---- 提交聊天 ----
  const submitText = (text) => {
    const textarea = document.querySelector('#task-composer textarea');
    if (!textarea) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(textarea, text); else textarea.value = text;
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    // 标记来源渠道：服务端据此写入统一的来源记录；身份/权限仍由登录上下文决定。
    window.LUX_INTAKE?.markVoiceAssistant?.();
    const sendBtn = document.querySelector('#task-send');
    if (!sendBtn || sendBtn.disabled) return false;
    setTimeout(() => { if (!sendBtn.disabled) sendBtn.click(); }, 50);
    return true;
  };

  // ---- 一轮结束：送转写 ----
  const endTurn = () => {
    if (phase !== 'listening' && phase !== 'followup') return;
    clearTimeout(timers.followup);
    const token = gen;
    const speechMs = vad ? vad.speechSamples / Math.max(1, sampleRate) * 1000 : 0;
    const samples = concatTurn();
    resetVad();
    setPhase('transcribing');

    if (!samples.length || speechMs < cfg.minSpeechMs) {
      enterFollowup('没有听清，请再说一遍。', token);
      return;
    }

    abortTranscribe();
    const ctl = new AbortController();
    transcribeCtl = ctl;
    const timeout = setTimeout(() => { try { ctl.abort(); } catch { /* ignore */ } }, TRANSCRIBE_TIMEOUT_MS);
    fetch(resolveUrl('/lux-voice-dialogue/transcribe'), {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ session: sessionId(), pcm: encodePcm(samples), rate: sampleRate, region: '' }),
      signal: ctl.signal,
    }).then((r) => r.json().catch(() => ({}))).then((data) => {
      if (token !== gen || phase !== 'transcribing') return;   // 迟到/已取消：不进入下一轮
      const text = ((data && data.text) || '').trim();
      if (!data || !data.ok || !text) {
        const msg = (data && data.message) || '没有听清，请再说一遍。';
        if (/请先登录/.test(msg)) { setStatus(msg); rearm(); return; }
        enterFollowup(msg, token);
        return;
      }
      // 重复事件：同一句在极短时间内再次到达才拦截；用户隔一会儿再说一遍仍会正常处理。
      const now = performance.now();
      if (text === lastSubmit.text && now - lastSubmit.at < DUPLICATE_WINDOW_MS) {
        enterFollowup('和刚才一样，无需重复。', token);
        return;
      }
      lastSubmit = { text, at: now };
      if (!submitText(text)) {
        setStatus('无法自动发送，请改用文字输入。');
        rearm();
        return;
      }
      setPhase('thinking');
      timers.thinking = setTimeout(() => {
        if (phase === 'thinking' && token === gen) { setStatus('处理超时，请重试。'); rearm(); }
      }, THINKING_TIMEOUT_MS);
    }).catch((err) => {
      if (token !== gen || phase !== 'transcribing') return;
      setStatus((err && err.name === 'AbortError') ? '识别超时，请重试。' : '网络中断，语音识别失败。');
      enterFollowup('网络中断或识别超时，请再说一遍。', token);
    }).finally(() => {
      clearTimeout(timeout);
      if (transcribeCtl === ctl) transcribeCtl = null;
    });
  };

  // ---- 回答播报 ----
  const startSpeaking = (token) => {
    setPhase('speaking');
    const tts = window.LUX_TTS;
    if (!tts || !tts.isReady || !tts.isReady() || !tts.playAnswer) {
      // TTS 未启用/未就绪：答案已在界面显示，直接进入追问窗口。
      enterFollowup('回答已显示，可继续追问。', token);
      return;
    }
    tts.playAnswer();
    // 合成失败/被拒/无答案时不会触发 playback，这里按纯文字处理进入追问。
    timers.speaking = setTimeout(() => {
      if (phase === 'speaking' && token === gen) enterFollowup('回答已显示，可继续追问。', token);
    }, SPEAKING_START_TIMEOUT_MS);
  };

  const onPcm = (samples, rate) => {
    if (!assistantOn) return;
    // 半双工：扬声器正在出声或刚结束的尾音，绝不当作新的用户问题（防止回声自问自答）。
    const tts = window.LUX_TTS;
    if (tts && ((tts.isPlaying && tts.isPlaying()) || (tts.isMuted && tts.isMuted()))) return;
    sampleRate = rate;
    if (phase !== 'listening' && phase !== 'followup') return;

    updatePreRoll(samples, rate);
    const rms = computeRms(samples);
    const now = performance.now();

    if (rms >= cfg.energyThreshold) {
      if (!vad.speech) {
        vad.speech = true;
        vad.onset = now;
        vad.lastSpeech = now;
        turnChunks = preRoll.slice();
        turnSamples = preRollSamples;
        if (phase === 'followup') {
          clearTimeout(timers.followup);
          setPhase('listening');
          listeningStart = now;
        }
      }
      vad.lastSpeech = now;
      vad.speechSamples += samples.length;
      turnChunks.push(samples);
      turnSamples += samples.length;
    } else if (vad.speech) {
      turnChunks.push(samples);
      turnSamples += samples.length;
      if (now - vad.lastSpeech >= cfg.pauseMs) { endTurn(); return; }
    } else if (phase === 'listening' && now - listeningStart >= cfg.silenceTimeoutMs) {
      setStatus('没有听到说话，已返回待唤醒。');
      rearm();
      return;
    }

    if (vad.speech && now - vad.onset >= cfg.maxTurnMs) endTurn();
  };

  // ---- 事件接线 ----
  let markSpeakingStarted = null;
  document.addEventListener('lux-voice-assistant-state', (e) => {
    const st = e.detail && e.detail.state;
    assistantOn = st && st !== 'disabled';
    if (!assistantOn) {
      // 关闭助手 / 切到后台：立即停播、取消在途识别、清空轮次，释放任务。
      interrupt();
      statusText = '';
      render();
    }
  });

  document.addEventListener('lux-voice:wake', () => {
    if (!assistantOn) return;
    gen += 1;
    abortTranscribe();
    clearTimers();
    resetVad();
    setPhase('waking');
    // 唤醒应答由 voice_output.js 播放；播完（或 TTS 未就绪的回退提示音）再进入聆听。
    const ttsReady = !!(window.LUX_TTS && window.LUX_TTS.isReady && window.LUX_TTS.isReady());
    timers.wake = setTimeout(() => {
      if (phase === 'waking') enterListening();
    }, ttsReady ? WAKE_READY_FALLBACK_MS : WAKE_FALLBACK_MS);
  });

  document.addEventListener('lux-tts:playback', (e) => {
    const st = e.detail && e.detail.state;
    const token = gen;
    if (phase === 'waking') {
      if (st === 'playing') {
        // 确认开始播报：等待它真正播完；若 ended 迟迟不来，用安全兜底进入聆听。
        clearTimeout(timers.wake);
        timers.wake = setTimeout(() => { if (phase === 'waking') enterListening(); }, WAKE_SAFETY_MS);
      } else if (st === 'ended' || st === 'error' || st === 'stopped' || st === 'blocked') {
        clearTimeout(timers.wake);
        if (token === gen) enterListening();
      }
    } else if (phase === 'speaking') {
      if (st === 'playing') {
        clearTimeout(timers.speaking);
        if (markSpeakingStarted) markSpeakingStarted();
      } else if (st === 'ended') {
        // 追问窗口严格从实际播放结束开始计时。
        clearTimeout(timers.speaking);
        enterFollowup('回答已说完，可继续追问或停止。', token);
      } else if (st === 'error' || st === 'stopped') {
        if (token !== gen) return;
        clearTimeout(timers.speaking);
        enterFollowup(st === 'error' ? '语音播放失败，回答已显示，可继续追问。' : '回答已显示，可继续追问。', token);
      } else if (st === 'blocked') {
        if (token !== gen) return;
        clearTimeout(timers.speaking);
        enterFollowup('浏览器阻止了自动播放，点上方「▶ 点击播放」或继续追问。', token);
      }
    }
  });

  document.addEventListener('lux-tts:answer-ready', () => {
    // 只有本脚本正等待的那一轮才播报；打断后迟到的回答不会进入下一轮。
    if (phase !== 'thinking') return;
    const token = gen;
    clearTimeout(timers.thinking);
    markSpeakingStarted = () => { clearTimeout(timers.speaking); };
    startSpeaking(token);
  });

  document.addEventListener('visibilitychange', () => {
    // 切到后台：按约定停播、取消在途识别并释放轮次（麦克风由 voice_assistant.js 关闭）。
    if (document.hidden && phase !== 'idle') interrupt();
  });
  window.addEventListener('pagehide', () => { gen += 1; abortTranscribe(); clearTimers(); });

  injectControls();
}
