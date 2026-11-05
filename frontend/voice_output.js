() => {
  if (document.documentElement.dataset.luxVoiceOutputBound === '1') return;
  document.documentElement.dataset.luxVoiceOutputBound = '1';

  // 里程碑 B：回答朗读（本地 TTS 已在服务器合成，浏览器只负责播放）。
  // 朗读的文本由服务器在每次 respond 时登记“本轮最终通过输出检查的答案”，
  // 本脚本只发“朗读最新答案”的请求，不自行抓取聊天框、也不把文本传给服务器。
  const resolveUrl = (p) => (/^https?:\/\//i.test(p) ? p : new URL(p, window.location.origin).href);

  // 浏览器会话标识：写进 cookie（服务端在对话与朗读路由读取），用于按设备隔离。
  // 同一账号在手机和电脑同时登录时，各自只朗读自己的回答，不会串台。
  const SESSION = (() => {
    try {
      const saved = localStorage.getItem('lux:voice:sid');
      if (saved) return saved;
    } catch { /* ignore */ }
    const id = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { localStorage.setItem('lux:voice:sid', id); } catch { /* ignore */ }
    return id;
  })();
  try {
    document.cookie = `lux-voice-sid=${encodeURIComponent(SESSION)}; path=/; SameSite=Lax; max-age=31536000`;
  } catch { /* ignore */ }

  const SYNTH_TIMEOUT_MS = 60000;   // 合成兜底超时（服务器自身也有超时）
  const WAKE_TIMEOUT_MS = 15000;

  let audio = null;         // 当前 <audio>
  let gen = 0;              // 代际号：每次新朗读/停止 +1，迟到/过期结果据此丢弃
  let ready = false;        // 服务器确认 TTS 已开启且模型就绪
  let autoRead = false;     // 自动朗读，默认关闭
  let unlockCtx = null;     // 用一次用户手势解锁音频，缓解移动端自动播放限制
  let abortCtl = null;      // 在途合成请求：停止/打断时立即取消
  let mutedUntil = 0;       // 播放结束后的短暂静音窗，避免把尾音/回声当用户输入

  const host = () => document.querySelector('#lux-tts-host');
  const statusEl = () => host() && host().querySelector('.tts-status');

  const setStatus = (text) => {
    const s = statusEl();
    if (!s) return;
    s.textContent = text;
    s.classList.toggle('is-error', /失败|阻止|不可用/.test(text));
  };

  // 播报生命周期事件，供语音对话编排器据此判断“应答播完→开始聆听”“回答播完→追问窗口”。
  const emitPlayback = (state) => {
    document.dispatchEvent(new CustomEvent('lux-tts:playback', { detail: { state } }));
  };

  const unlock = () => {
    try {
      if (!unlockCtx) unlockCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (unlockCtx.state === 'suspended') unlockCtx.resume();
    } catch { /* ignore */ }
  };

  const stopAudio = () => {
    if (audio) {
      try { audio.pause(); audio.removeAttribute('src'); audio.load(); } catch { /* ignore */ }
      audio = null;
    }
  };

  const stop = () => {
    gen += 1;
    if (abortCtl) { try { abortCtl.abort(); } catch { /* ignore */ } abortCtl = null; }
    stopAudio();
    setStatus('');
    // 停止后短静音窗：避免扬声器尾音被麦克风当成新一轮提问。
    mutedUntil = performance.now() + 400;
    emitPlayback('stopped');
  };

  const play = (url, token) => {
    stopAudio();
    const a = new Audio();
    a.preload = 'auto';
    a.src = url;
    audio = a;
    const guard = (fn) => () => { if (token === gen) fn(); };
    a.addEventListener('playing', guard(() => { setStatus('正在朗读…'); emitPlayback('playing'); }));
    a.addEventListener('ended', guard(() => {
      stopAudio();
      setStatus('');
      // 追问窗口从“实际播放结束”开始计时，这里同时标记静音窗。
      mutedUntil = performance.now() + 400;
      emitPlayback('ended');
    }));
    a.addEventListener('error', guard(() => { stopAudio(); setStatus('音频加载失败，请重试。'); mutedUntil = performance.now() + 400; emitPlayback('error'); }));
    a.play().catch(guard(() => {
      // 移动端自动播放被拒：保留“点击朗读”入口，而不是静默失败。
      setStatus('浏览器阻止了自动播放，点「朗读」即可播放。');
      showPlayButton(url, token);
      emitPlayback('blocked');
    }));
  };

  const showPlayButton = (url, token) => {
    const h = host();
    if (!h) return;
    h.querySelectorAll('.tts-manual').forEach((el) => el.remove());
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tts-manual';
    btn.textContent = '▶ 点击播放';
    btn.addEventListener('click', () => {
      unlock();
      const a = audio || new Audio();
      a.src = url;
      audio = a;
      a.addEventListener('playing', () => { setStatus('正在朗读…'); emitPlayback('playing'); });
      a.addEventListener('ended', () => { stopAudio(); setStatus(''); mutedUntil = performance.now() + 400; emitPlayback('ended'); });
      a.addEventListener('error', () => { stopAudio(); setStatus('音频加载失败，请重试。'); mutedUntil = performance.now() + 400; emitPlayback('error'); });
      a.play().catch(() => { setStatus('无法播放，请检查浏览器设置。'); emitPlayback('error'); });
    });
    h.appendChild(btn);
  };

  // 带超时的 fetch：网络中断/服务器卡住时及时失败，界面可恢复操作。
  const fetchJson = async (url, body, timeoutMs) => {
    const ctl = new AbortController();
    abortCtl = ctl;
    const timer = setTimeout(() => { try { ctl.abort(); } catch { /* ignore */ } }, timeoutMs);
    try {
      const res = await fetch(resolveUrl(url), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctl.signal,
      });
      const data = await res.json().catch(() => ({}));
      return { res, data };
    } finally {
      clearTimeout(timer);
      if (abortCtl === ctl) abortCtl = null;
    }
  };

  const synthesize = async () => {
    const token = ++gen;
    if (abortCtl) { try { abortCtl.abort(); } catch { /* ignore */ } abortCtl = null; }
    setStatus('正在合成语音…');
    try {
      const { res, data } = await fetchJson('/lux-tts/synthesize', { session: SESSION }, SYNTH_TIMEOUT_MS);
      if (!res.ok) throw new Error(data.detail || '合成失败');
      if (token !== gen) return;  // 期间被停止或发起新朗读，丢弃迟到结果
      play(resolveUrl(data.url), token);
    } catch (e) {
      if (token !== gen) return;
      setStatus((e && e.name === 'AbortError') ? '合成超时或已取消，请重试。' : '朗读失败：' + (e && e.message ? e.message : '请重试'));
      emitPlayback('error');
    }
  };

  const playWakeReply = async () => {
    const token = ++gen;
    try {
      const { res, data } = await fetchJson('/lux-tts/wake-reply', { session: SESSION }, WAKE_TIMEOUT_MS);
      if (!res.ok) throw new Error(data.detail || '');
      if (token !== gen) return;
      play(resolveUrl(data.url), token);
    } catch {
      if (token === gen) { setStatus(''); emitPlayback('error'); }
    }
  };

  const injectControls = () => {
    const h = host();
    if (!h) return;
    h.classList.add('is-ready');
    if (!document.querySelector('#lux-tts-styles')) {
      const st = document.createElement('style');
      st.id = 'lux-tts-styles';
      st.textContent = [
        '#lux-tts-host{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0;min-height:32px;}',
        '#lux-tts-host .tts-read,#lux-tts-host .tts-stop,#lux-tts-host .tts-manual{',
        '  font-size:13px;padding:4px 12px;border-radius:6px;border:1px solid var(--border-color, #3a3f4b);',
        '  background:var(--bg-secondary, transparent);color:var(--text-primary, inherit);cursor:pointer;}',
        '#lux-tts-host .tts-read:hover,#lux-tts-host .tts-stop:hover{opacity:.85;}',
        '#lux-tts-host .tts-auto{display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer;}',
        '#lux-tts-host .tts-status{font-size:12px;opacity:.8;}',
        '#lux-tts-host .tts-status.is-error{color:#e5484d;}',
      ].join('');
      document.head.appendChild(st);
    }
    const read = document.createElement('button');
    read.type = 'button';
    read.className = 'tts-read';
    read.textContent = '朗读';
    read.setAttribute('aria-pressed', 'false');
    read.addEventListener('click', () => { unlock(); synthesize(); });

    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'tts-stop';
    stopBtn.textContent = '停止';
    stopBtn.addEventListener('click', stop);

    const auto = document.createElement('label');
    auto.className = 'tts-auto';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = autoRead;
    cb.addEventListener('change', () => {
      autoRead = cb.checked;
      localStorage.setItem('lux:tts:autoread', autoRead ? '1' : '0');
    });
    auto.append(cb, document.createTextNode(' 自动朗读'));

    const status = document.createElement('span');
    status.className = 'tts-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    h.append(read, stopBtn, auto, status);
  };

  const init = async () => {
    try {
      const res = await fetch(resolveUrl('/lux-tts/status'));
      const info = await res.json();
      if (!res.ok || !info.enabled || !info.ready) return;  // 未启用/模型未就绪则不显示控件
      ready = true;
      injectControls();
    } catch { /* ignore */ }
  };

  // 唤醒后播放预合成的“我在，请说”；若本脚本能处理则取消 voice_assistant 的提示音。
  document.addEventListener('lux-voice:wake', (e) => {
    if (!ready) return;
    e.preventDefault();
    playWakeReply();
  });

  // 新回答渲染完成：自动朗读开启时读最新答案；语音对话闭环自己负责播报，不重复朗读。
  document.addEventListener('lux-tts:answer-ready', () => {
    if (!ready || !autoRead) return;
    if (document.documentElement.dataset.luxVoiceDialogueActive === '1') return;
    synthesize();
  });

  // 任意点击解锁音频上下文，缓解移动端自动播放限制。
  document.addEventListener('click', unlock, { capture: true });

  // 切到后台：停止播放并取消在途合成（语音对话的轮次状态由 voice_dialogue.js 负责清理）。
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    gen += 1;
    if (abortCtl) { try { abortCtl.abort(); } catch { /* ignore */ } abortCtl = null; }
    stopAudio();
  });

  window.addEventListener('pagehide', () => {
    // 离开页面：停止播放并取消在途合成，避免设备间残留播放。
    gen += 1;
    if (abortCtl) { try { abortCtl.abort(); } catch { /* ignore */ } abortCtl = null; }
    stopAudio();
  });

  // 默认关闭自动朗读；读取一次用户上次的偏好。
  try { autoRead = localStorage.getItem('lux:tts:autoread') === '1'; } catch { autoRead = false; }

  // 供语音对话编排器调用：播放最新回答、播放唤醒应答、停止、查询是否就绪。
  // isPlaying()/isMuted() 用于半双工：扬声器正在出声或刚结束时，不把声音当用户提问。
  window.LUX_TTS = {
    isReady: () => ready,
    isPlaying: () => !!(audio && !audio.paused && !audio.ended),
    isMuted: () => performance.now() < mutedUntil,
    sessionId: SESSION,
    playAnswer: synthesize,
    playWakeReply,
    stop,
  };

  init();
}
