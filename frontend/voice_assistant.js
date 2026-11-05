() => {
  if (document.documentElement.dataset.luxVoiceAssistantBound === '1') return;
  document.documentElement.dataset.luxVoiceAssistantBound = '1';

  // 里程碑 A：网页前台“小安小安”语音唤醒（本地 KWS，未唤醒时不上传环境音频）。
  // 唤醒词在浏览器本地由 Sherpa-ONNX WASM 检测，仅加载模型时访问服务端静态资源。
  const cfg = {
    wakeWord: (window.LUX_VOICE || {}).wakeWord || '小安小安',
    workerUrl: (window.LUX_VOICE || {}).workerUrl || '/lux-kws/kws-worker.js',
    modelDataUrl: (window.LUX_VOICE || {}).modelDataUrl || '/lux-kws/model.data',
    modelMetadataUrl: (window.LUX_VOICE || {}).modelMetadataUrl || '/lux-kws/model.metadata',
    listeningTimeoutMs: Number((window.LUX_VOICE || {}).listeningTimeoutMs || 8000),
    wakeCooldownMs: Number((window.LUX_VOICE || {}).wakeCooldownMs || 1500),
  };

  // 绝对化：Gradio 分享页的前端脚本可能从 CDN（S3）加载，若直接写相对/绝对路径，
  // import() / Worker / fetch 会错误地解析到 CDN 域。这里统一解析到页面自身 origin：
  // 分享页 = share 域名，局域网 = 本机 IP，都能命中 /lux-kws/* 路由。
  const resolveUrl = (p) => (/^https?:\/\//i.test(p) ? p : new URL(p, window.location.origin).href);
  const urls = {
    worker: resolveUrl(cfg.workerUrl),
    main: resolveUrl(cfg.workerUrl.replace(/kws-worker\.js$/, 'main.js')),
    data: resolveUrl(cfg.modelDataUrl),
    metadata: resolveUrl(cfg.modelMetadataUrl),
  };

  // 状态机：disabled / loading / armed / acknowledged / listening / error
  let state = 'disabled';
  let spotter = null;
  let capture = null;
  let wakeCooldownUntil = 0;
  let ring = null;        // 环形缓冲：保留唤醒前约 4 秒，避免“小安小安，帮我…”丢掉问题开头
  let ringRate = 16000;

  const RING_SECONDS = 4;

  // 里程碑 A 验收指标（全部在浏览器本地记录，不上传）：
  //   hits       —— 唤醒命中次数（armed 态下模型判定命中，即“唤醒成功”候选）
  //   latencyMs  —— “喂入音频 → 命中回调”的检测延迟
  //   armedMs    —— 累计处于待唤醒时长（用于评估误触发频率的分母）
  // 误触发 = 在受控静音/无关语音时段出现的命中，由测试者对照命中时间戳判定。
  let lastFeedAt = 0;
  let armedSince = 0;
  const metrics = {
    hits: 0,
    lastHitAt: 0,
    latencyMs: { last: 0, min: Infinity, max: 0, sum: 0, n: 0 },
    armedMs: 0,
    enabledAt: 0,
  };

  const el = (sel) => document.querySelector(sel);
  const statusEl = () => el('#voice-assistant-status');
  const buttonEl = () => el('#task-voice-assistant');

  const STATE_TEXT = {
    disabled: '',
    loading: '正在加载本地唤醒模型…',
    armed: `已就绪，喊“${cfg.wakeWord}”唤醒`,
    acknowledged: '我在，请说',
    listening: '正在聆听…',
    error: '语音助手启动失败',
  };

  const metricsText = () => {
    if (state === 'disabled' || metrics.hits === 0) return '';
    const lat = metrics.latencyMs;
    const avg = lat.n ? Math.round(lat.sum / lat.n) : 0;
    const parts = [`命中 ${metrics.hits} 次`];
    if (avg) parts.push(`延迟 均${avg}ms·最近${Math.round(lat.last)}ms`);
    if (metrics.armedMs > 0) parts.push(`待唤醒 ${(metrics.armedMs / 1000).toFixed(0)}s`);
    return parts.join(' · ');
  };

  const render = () => {
    const btn = buttonEl();
    const status = statusEl();
    if (btn) {
      const active = state !== 'disabled';
      btn.textContent = active ? '关闭语音助手' : '开启语音助手';
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-pressed', String(active));
      btn.disabled = state === 'loading';
    }
    if (status) {
      status.textContent = '';
      const stateSpan = document.createElement('span');
      stateSpan.textContent = STATE_TEXT[state] || '';
      status.appendChild(stateSpan);
      const m = metricsText();
      if (m) {
        const mSpan = document.createElement('span');
        mSpan.className = 'va-metrics';
        mSpan.textContent = m;
        mSpan.style.display = 'block';
        mSpan.style.fontSize = '12px';
        mSpan.style.opacity = '0.78';
        mSpan.style.marginTop = '2px';
        status.appendChild(mSpan);
      }
      status.classList.toggle('is-error', state === 'error');
      status.classList.toggle('is-awake', state === 'listening' || state === 'acknowledged');
    }
    document.dispatchEvent(new CustomEvent('lux-voice-assistant-state', { detail: { state } }));
  };

  const logEvent = (type, extra) => {
    try {
      console.log('[voice-assistant]', type, { at: new Date().toISOString(), state, ...(extra || {}) });
    } catch { /* ignore */ }
  };

  const setState = (next) => {
    const now = performance.now();
    if (state === 'armed' && next !== 'armed' && armedSince) {
      metrics.armedMs += now - armedSince;
      armedSince = 0;
    }
    if (next === 'armed') armedSince = now;
    state = next;
    render();
  };

  // 短提示音：唤醒应答（预合成，避免依赖 TTS）。
  let chimeCtx = null;
  const chime = () => {
    try {
      if (!chimeCtx) chimeCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (chimeCtx.state === 'suspended') chimeCtx.resume();
      const now = chimeCtx.currentTime;
      [[880, 0], [1174.66, 0.16]].forEach(([freq, at]) => {
        const osc = chimeCtx.createOscillator();
        const gain = chimeCtx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + at);
        gain.gain.linearRampToValueAtTime(0.25, now + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + at + 0.28);
        osc.connect(gain).connect(chimeCtx.destination);
        osc.start(now + at);
        osc.stop(now + at + 0.32);
      });
    } catch { /* 提示音失败不影响唤醒状态 */ }
  };

  const resetRing = (sampleRate) => {
    ringRate = sampleRate;
    ring = new Float32Array(0);
  };
  const pushRing = (samples) => {
    if (!ring) { ring = new Float32Array(0); }
    const cap = Math.floor(ringRate * RING_SECONDS);
    const keep = ring.length + samples.length > cap ? cap - samples.length : ring.length;
    const next = new Float32Array(Math.max(0, keep) + samples.length);
    if (keep > 0) next.set(ring.subarray(ring.length - Math.max(0, keep)));
    next.set(samples, Math.max(0, keep));
    ring = next;
  };
  const takeRing = () => ({ samples: ring ? ring.slice() : new Float32Array(0), sampleRate: ringRate });

  const recordHit = (hits) => {
    const now = performance.now();
    const latency = lastFeedAt ? Math.max(0, now - lastFeedAt) : 0;
    metrics.hits += 1;
    metrics.lastHitAt = now;
    const lat = metrics.latencyMs;
    lat.last = latency;
    lat.min = Math.min(lat.min, latency);
    lat.max = Math.max(lat.max, latency);
    lat.sum += latency;
    lat.n += 1;
    logEvent('hit', { count: metrics.hits, latencyMs: Math.round(latency), detections: (hits || []).length });
  };

  const onWake = () => {
    if (state !== 'armed') return;
    setState('acknowledged');
    // 若 TTS 已就绪，由 voice_output.js 播放预合成的“我在，请说”并取消本提示音；
    // 否则退回本地提示音，保证唤醒永远有即时反馈。
    const wakeEv = new CustomEvent('lux-voice:wake', { cancelable: true });
    if (document.dispatchEvent(wakeEv)) chime();
    logEvent('wake');
    // 冷却期防止提示音/回声被再次当作唤醒词。
    wakeCooldownUntil = performance.now() + cfg.wakeCooldownMs;
    setTimeout(() => { if (state === 'acknowledged') setState('listening'); }, 320);
    // 里程碑 C：进入聆听态后，聆听/VAD/转写/追问的编排交给 voice_dialogue.js；
    // 轮次结束由它回发 lux-voice:rearm，这里不再用固定超时自行返回待唤醒。
  };

  const disposeCapture = async (rec) => {
    if (!rec) return;
    clearInterval(rec.timer);
    rec.stream?.getTracks().forEach((t) => t.stop());
    rec.source?.disconnect();
    rec.node?.disconnect();
    rec.sink?.disconnect();
    if (rec.context && rec.context.state !== 'closed') await rec.context.close();
  };

  const stopCapture = async () => {
    const rec = capture;
    capture = null;
    await disposeCapture(rec);
  };

  const startCapture = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    } });
    const context = new (window.AudioContext || window.webkitAudioContext)();
    if (!context.audioWorklet) throw new Error('当前浏览器不支持 AudioWorklet，无法进行本地唤醒。');
    const processor = `
      class LuxKwsPcm extends AudioWorkletProcessor {
        constructor() { super(); this.parts = []; this.count = 0; }
        flush() {
          if (!this.count) return;
          const samples = new Float32Array(this.count);
          let offset = 0;
          for (const part of this.parts) { samples.set(part, offset); offset += part.length; }
          this.parts = []; this.count = 0;
          this.port.postMessage(samples, [samples.buffer]);
        }
        process(inputs) {
          const channel = inputs[0]?.[0];
          if (channel?.length) { this.parts.push(new Float32Array(channel)); this.count += channel.length; }
          if (this.count >= 2048) this.flush();
          return true;
        }
      }
      registerProcessor('lux-kws-pcm', LuxKwsPcm);
    `;
    const url = URL.createObjectURL(new Blob([processor], { type: 'application/javascript' }));
    try { await context.audioWorklet.addModule(url); }
    finally { URL.revokeObjectURL(url); }
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, 'lux-kws-pcm');
    const sink = context.createGain(); sink.gain.value = 0;
    const rec = { stream, context, source, node, sink, timer: null };
    resetRing(context.sampleRate);
    node.port.onmessage = (event) => {
      const samples = event.data;
      if (!(samples instanceof Float32Array) || !samples.length) return;
      pushRing(samples);
      // 把 PCM 交给语音对话编排器做 VAD 与轮次录音（不论当前状态，由它自行取舍）。
      document.dispatchEvent(new CustomEvent('lux-voice:pcm', { detail: { samples, sampleRate: context.sampleRate } }));
      if (state === 'armed' && performance.now() >= wakeCooldownUntil && spotter) {
        lastFeedAt = performance.now();
        spotter.processAudio(samples, context.sampleRate);
      }
      // 唤醒后（acknowledged/listening）暂停喂入 KWS，避免把应答音/后续当作二次唤醒；
      // 语音对话闭环由 voice_dialogue.js 通过 lux-voice:pcm 消费这里的音频。
    };
    source.connect(node); node.connect(sink); sink.connect(context.destination);
    await context.resume();
    return rec;
  };

  const resetMetrics = () => {
    metrics.hits = 0;
    metrics.lastHitAt = 0;
    metrics.armedMs = 0;
    metrics.enabledAt = performance.now();
    metrics.latencyMs = { last: 0, min: Infinity, max: 0, sum: 0, n: 0 };
    armedSince = 0;
    lastFeedAt = 0;
  };

  const enable = async () => {
    if (state !== 'disabled') return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setState('error');
      setStatusDetail('麦克风需要 localhost 或 HTTPS，请使用安全地址。');
      return;
    }
    resetMetrics();
    logEvent('enable');
    setState('loading');
    try {
      const { KwsSpotter } = await import(urls.main);
      spotter = new KwsSpotter({ workerUrl: urls.worker });
      await spotter.init({
        dataUrl: urls.data,
        metadataUrl: urls.metadata,
        onHit: (hits) => {
          if (hits && hits.length) { recordHit(hits); onWake(); }
        },
      });
      capture = await startCapture();
      setState('armed');
      logEvent('armed');
    } catch (error) {
      setState('error');
      setStatusDetail(String(error && error.message || error));
      logEvent('error', { message: String(error && error.message || error) });
      if (spotter) { try { spotter.dispose(); } catch { /* ignore */ } spotter = null; }
      await stopCapture();
    }
  };

  const disable = async () => {
    logEvent('disable');
    await stopCapture();
    if (spotter) { try { spotter.dispose(); } catch { /* ignore */ } spotter = null; }
    ring = null;
    setState('disabled');
  };

  const setStatusDetail = (text) => {
    const status = statusEl();
    if (status) status.textContent = text;
  };

  document.addEventListener('click', (event) => {
    if (event.target.closest('#task-voice-assistant')) {
      if (state === 'disabled') enable();
      else disable();
      return;
    }
  });

  // 语音对话编排器完成一轮（或打断/超时）后，请求回到待唤醒并重新开启 KWS。
  document.addEventListener('lux-voice:rearm', () => {
    if (state === 'acknowledged' || state === 'listening') {
      setState('armed');
      pushRing(new Float32Array(Math.floor(ringRate * 0.2))); // 停顿静音冲刷
      logEvent('rearm');
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && state !== 'disabled') disable();
  });
  window.addEventListener('pagehide', () => {
    if (spotter) { try { spotter.dispose(); } catch { /* ignore */ } }
    if (capture) disposeCapture(capture);
  });
}
