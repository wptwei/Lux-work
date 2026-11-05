() => {
  if (document.documentElement.dataset.luxLiveVoiceBound === '1') return;
  document.documentElement.dataset.luxLiveVoiceBound = '1';
  let capture = null;
  let requesting = false;
  let lastOrder = 0;
  let permissionGeneration = 0;
  let activeId = null;
  let lastRevision = -1;
  let waiting = false;
  let waitTimer;
  let ackTimer;
  let receivedSeq = -1;
  let finalReceived = false;
  let receivedSeconds = 0;
  // 当前语音会话“拥有”的输入框区间：partial 替换该区间，final 校正一次后释放。
  let voiceRegion = null;      // {id, start, end, text, manual}
  let applyingVoice = false;   // 程序化写入不算用户编辑
  let composing = false;       // 输入法组合期间不覆盖、暂存识别结果
  let pendingVoiceResult = null;
  let lastComposerValue = '';
  let lastEditStart = -1;
  let lastEditEnd = -1;
  const message = (text) => {
    const line = document.querySelector('#voice-inline');
    if (line) line.hidden = !text;
    const cancel = document.querySelector('#voice-cancel');
    if (cancel) cancel.hidden = !(capture || requesting || waiting);
    const el = document.querySelector('#voice-live-status');
    if (el) el.textContent = text;
  };
  const preview = (text) => {
    const el = document.querySelector('#voice-live-preview');
    if (el) el.textContent = text;
  };
  const controls = (active, pending = false) => {
    const meter = document.querySelector('#voice-level');
    if (meter) { meter.hidden = !active; if (!active) meter.style.setProperty('--voice-level', '0'); }
    const button = document.querySelector('#task-voice');
    if (!button) return;
    button.disabled = pending;
    button.textContent = active ? '结束说话' : waiting ? '转写中…' : '语音输入';
    button.classList.toggle('is-recording', active);
    button.setAttribute('aria-pressed', String(active));
  };
  const setInput = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(input, value); else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const emit = (payload) => {
    const input = document.querySelector('#voice-pcm-channel textarea');
    if (!input) throw new Error('input unavailable');
    setInput(input, JSON.stringify(payload));
  };
  const composerEl = () => document.querySelector('#task-composer textarea');
  const voiceJoin = (value) => (value && !value.endsWith('\n')) ? '\n' : '';
  const voiceResultingText = (input, text) => {
    const value = input.value || '';
    if (!voiceRegion) return value + voiceJoin(value) + text;
    const full = (voiceRegion.sep && text) ? voiceRegion.sep + text : text;
    return value.slice(0, voiceRegion.start) + full + value.slice(voiceRegion.end);
  };
  const writeVoiceRegion = (input, text) => {
    const value = input.value || '';
    if (!voiceRegion) {
      const join = voiceJoin(value);
      voiceRegion = { id: activeId, start: value.length, end: value.length + join.length + text.length, text: join + text, sep: join, manual: false };
      return value + join + text;
    }
    const before = value.slice(0, voiceRegion.start);
    const after = value.slice(voiceRegion.end);
    const full = (voiceRegion.sep && text) ? voiceRegion.sep + text : text;
    voiceRegion.text = full;
    voiceRegion.end = voiceRegion.start + full.length;
    return before + full + after;
  };
  const removeVoiceRegion = (input) => {
    if (!voiceRegion) return;
    const value = input.value || '';
    applyingVoice = true;
    try {
      setInput(input, value.slice(0, voiceRegion.start) + value.slice(voiceRegion.end));
      lastComposerValue = input.value;
    } finally { applyingVoice = false; }
  };
  const releaseRegion = () => { voiceRegion = null; };

  const applyVoiceResult = (result) => {
    const input = composerEl();
    if (!input) { message('输入框暂不可用，请复制下方文字。'); preview(result.ok ? result.text : ''); return; }

    if (!result.final) {
      if (!result.ok) {
        if (result.blocked) {
          // 命中的 live 假设不得留在输入框里，回退本会话自动管理的语音区间。
          if (voiceRegion && !voiceRegion.manual) removeVoiceRegion(input);
          releaseRegion();
          preview('');
          if (!waiting) message(result.message || '语音文字命中安全检查，请改述。');
        }
        return; // 空假设（如“正在聆听…”）不影响已有语音区间
      }
      if (voiceRegion && voiceRegion.manual) {
        preview(result.text);
        if (!waiting) message('检测到手动编辑，识别文字见预览，可手动合并。');
        return;
      }
      if (Array.from(voiceResultingText(input, result.text)).length > result.max_chars) {
        preview(result.text);
        if (!waiting) message('识别文字较长，输入框长度不足，请停止后分段处理。');
        return;
      }
      applyingVoice = true;
      try { setInput(input, writeVoiceRegion(input, result.text)); lastComposerValue = input.value; }
      finally { applyingVoice = false; }
      preview('');
      if (!waiting) message('正在聆听，再点语音按钮结束。');
      return;
    }

    // final：校正同一区间一次，然后释放管理权。
    const regionManaged = !!(voiceRegion && !voiceRegion.manual);
    let replacement = null;
    let fallbackPreview = '';
    let finalMessage = '';

    if (!result.ok) {
      if (regionManaged) removeVoiceRegion(input);
      releaseRegion();
      finalMessage = result.message || '识别未通过，未填入输入框。';
    } else if (voiceRegion && voiceRegion.manual) {
      fallbackPreview = result.text;
      releaseRegion();
      finalMessage = '识别完成，但检测到手动编辑，请手动合并预览文字后发送。';
    } else {
      const resulting = voiceResultingText(input, result.text);
      if (Array.from(resulting).length > result.max_chars) {
        if (regionManaged) removeVoiceRegion(input);
        releaseRegion();
        fallbackPreview = result.text;
        finalMessage = '识别完成，但输入框长度不足，原草稿已保留，请复制预览文字分段处理。';
      } else {
        replacement = writeVoiceRegion(input, result.text);
        releaseRegion();
        finalMessage = '识别完成，文字已填入，可编辑后发送。';
      }
    }

    activeId = null; waiting = false; clearTimeout(waitTimer);
    controls(false);
    if (replacement !== null) {
      applyingVoice = true;
      try { setInput(input, replacement); lastComposerValue = input.value; } finally { applyingVoice = false; }
      preview('');
      // 来源标记：听写结果已填入，先按“未编辑”记录；用户随后改动会置为已编辑。
      window.LUX_INTAKE?.markDictation?.(false);
    } else {
      preview(fallbackPreview);
    }
    message(finalMessage);
  };

  window.addEventListener('lux-voice-result', (event) => {
    let result;
    try { result = JSON.parse(event.detail); } catch { return; }
    if (!activeId || result.id !== activeId) return;
    clearTimeout(ackTimer);
    if (result.error) {
      activeId = null;
      permissionGeneration++;
      if (capture) { dispose(capture); capture = null; }
      waiting = false; clearTimeout(waitTimer);
      const input = composerEl();
      if (input && voiceRegion && !voiceRegion.manual) removeVoiceRegion(input);
      releaseRegion();
      preview(''); message(result.error); controls(false);
      return;
    }
    if (result.kind === 'ack') {
      // Receive acknowledgements and inference results have independent sequences.
      if (result.received_seq < receivedSeq) return;
      receivedSeq = result.received_seq;
      receivedSeconds = Math.max(receivedSeconds, result.seconds || 0);
      finalReceived ||= result.phase === 'final';
      if (waiting) message(finalReceived ? `已收到 ${receivedSeconds} 秒录音，正在转文字…` : '正在接收最后一段录音…');
      else if (lastRevision < 0) message(receivedSeconds > 0 ? `已收到 ${receivedSeconds} 秒录音，正在识别…` : '语音已连接，请说话。');
      return;
    }
    if (result.revision <= lastRevision) return;
    lastRevision = result.revision;
    if (composing) { pendingVoiceResult = result; return; }
    applyVoiceResult(result);
  });
  const encode = (blocks) => {
    const count = blocks.reduce((sum, block) => sum + block.length, 0);
    const bytes = new Uint8Array(count * 2);
    const view = new DataView(bytes.buffer);
    let offset = 0;
    for (const block of blocks) for (const raw of block) {
      const value = Math.max(-1, Math.min(1, raw));
      view.setInt16(offset, Math.round(value * (value < 0 ? 32768 : 32767)), true);
      offset += 2;
    }
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    }
    return btoa(binary);
  };
  const flush = (rec, final) => {
    if (!rec.blocks.length && !final) return;
    const blocks = rec.blocks;
    rec.blocks = [];
    emit({ id: rec.id, seq: ++rec.seq, rate: rec.context.sampleRate,
           pcm: encode(blocks), final });
  };
  const dispose = async (rec) => {
    clearInterval(rec.timer);
    clearTimeout(rec.limit);
    rec.stream?.getTracks().forEach((track) => track.stop());
    rec.source?.disconnect();
    rec.node?.disconnect();
    rec.sink?.disconnect();
    if (rec.context && rec.context.state !== 'closed') await rec.context.close();
  };
  const stop = async () => {
    const rec = capture;
    if (!rec) {
      if (requesting) { permissionGeneration++; message('已取消录音请求。'); }
      return;
    }
    if (rec.stopping) return;
    rec.stopping = true;
    waiting = true;
    controls(false, true);
    clearInterval(rec.timer);
    clearTimeout(rec.limit);
    message('正在接收最后一段录音…');
    try {
      // The worklet sends all pending samples before its acknowledgement, in port order.
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error('stop timeout')), 3000);
        rec.stopped = () => { clearTimeout(timeout); resolve(); };
        rec.node.port.postMessage({ kind: 'stop' });
      });
      if (activeId === rec.id) flush(rec, true);
      if (waiting) message('正在转文字…');
    } catch {
      // Do not silently discard already delivered PCM if the worklet fails to acknowledge.
      try { if (activeId === rec.id) flush(rec, true); } catch {}
      message('录音连接中断，最后一段可能不完整，请核对文字或重新录音。');
    } finally {
      await dispose(rec);
      if (capture === rec) capture = null;
      controls(false, waiting);
      if (waiting) waitTimer = setTimeout(() => {
        activeId = null; waiting = false; controls(false);
        releaseRegion();
        clearTimeout(ackTimer);
        message(finalReceived ? '后端已收到完整录音，但识别结果等待超时；请查看 LUX 终端的 [voice] 日志。' :
          '未收到完整录音回执，请检查 LUX 连接并重试。');
      }, 90000);
    }
  };
  const cancel = () => {
    permissionGeneration++; activeId = null; waiting = false;
    clearTimeout(ackTimer); clearTimeout(waitTimer);
    const rec = capture; capture = null;
    if (rec) dispose(rec);
    const input = composerEl();
    const wasManual = !!(voiceRegion && voiceRegion.manual);
    if (input && voiceRegion && !voiceRegion.manual) removeVoiceRegion(input);
    releaseRegion();
    pendingVoiceResult = null;
    preview(''); controls(false);
    message(wasManual ? '已取消录音，检测到的手动编辑已保留。' : '已取消录音，本次语音文字已移除，原草稿已保留。');
    const button = document.querySelector('#voice-cancel');
    if (button) button.hidden = true;
  };
  const start = async () => {
    if (capture || requesting || waiting) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      message('麦克风需要 localhost 或 HTTPS，请使用安全地址。');
      return;
    }
    requesting = true;
    const generation = ++permissionGeneration;
    controls(false, true);
    message('请允许麦克风访问…');
    preview('');
    const rec = { id: crypto.randomUUID(), seq: 0, blocks: [], stopping: false };
    activeId = rec.id; lastRevision = -1;
    receivedSeq = -1; receivedSeconds = 0; finalReceived = false;
    voiceRegion = null; pendingVoiceResult = null;
    lastComposerValue = composerEl()?.value || '';
    lastEditStart = lastEditEnd = -1;
    try {
      rec.stream = await navigator.mediaDevices.getUserMedia({ audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true
      } });
      if (generation !== permissionGeneration) throw { cancelled: true };
      rec.context = new AudioContext();
      if (!rec.context.audioWorklet) throw new Error('worklet unavailable');
      const processor = `
        class LuxPCM extends AudioWorkletProcessor {
          constructor() {
            super(); this.active = true; this.parts = []; this.count = 0;
            this.port.onmessage = (event) => {
              if (event.data.kind === 'stop') {
                this.active = false; this.flush(); this.port.postMessage({kind: 'stopped'});
              }
            };
          }
          flush() {
            if (!this.count) return;
            const samples = new Float32Array(this.count); let offset = 0;
            for (const part of this.parts) { samples.set(part, offset); offset += part.length; }
            this.parts = []; this.count = 0;
            this.port.postMessage({kind: 'pcm', samples}, [samples.buffer]);
          }
          process(inputs) {
            const channel = inputs[0]?.[0];
            if (this.active && channel?.length) {
              this.parts.push(new Float32Array(channel)); this.count += channel.length;
              if (this.count >= 4096) this.flush();
            }
            return this.active;
          }
        }
        registerProcessor('lux-pcm', LuxPCM);
      `;
      const url = URL.createObjectURL(new Blob([processor], { type: 'application/javascript' }));
      try { await rec.context.audioWorklet.addModule(url); }
      finally { URL.revokeObjectURL(url); }
      rec.source = rec.context.createMediaStreamSource(rec.stream);
      rec.node = new AudioWorkletNode(rec.context, 'lux-pcm');
      rec.sink = rec.context.createGain(); rec.sink.gain.value = 0;
      rec.node.port.onmessage = (event) => {
        if (event.data.kind === 'pcm' && activeId === rec.id && !rec.stopping) {
          rec.blocks.push(event.data.samples);
          const samples=event.data.samples;
          let energy=0; for(const sample of samples) energy+=sample*sample;
          const level=Math.min(1, Math.sqrt(energy/Math.max(1,samples.length))*6);
          document.querySelector('#voice-level')?.style.setProperty('--voice-level',String(level));
        } else if (event.data.kind === 'pcm' && activeId === rec.id && rec.stopping) {
          rec.blocks.push(event.data.samples); // Preserve final worklet tail before stop acknowledgement.
        }
        if (event.data.kind === 'stopped') rec.stopped?.();
      };
      lastOrder = Math.max(Date.now(), lastOrder + 1);
      ackTimer = setTimeout(() => {
        if (activeId !== rec.id) return;
        activeId = null; permissionGeneration++; waiting = false;
        clearTimeout(waitTimer);
        releaseRegion();
        if (capture === rec) capture = null;
        dispose(rec); controls(false);
        message('15 秒内未收到后端回执，请重启 LUX 并 Ctrl+F5 刷新；无需重装语音模型。');
      }, 15000);
      emit({ id: rec.id, order: lastOrder, seq: 0, kind: 'start' });
      if (generation !== permissionGeneration) throw { cancelled: true };
      capture = rec;
      rec.source.connect(rec.node); rec.node.connect(rec.sink); rec.sink.connect(rec.context.destination);
      await rec.context.resume();
      if (generation !== permissionGeneration || activeId !== rec.id) throw { cancelled: true };
      rec.timer = setInterval(() => {
        try { flush(rec, false); }
        catch { message('录音传输失败，请重新录音。'); stop(); }
      }, 2000);
      rec.limit = setTimeout(stop, 119000);
      controls(true);
      message('正在聆听，再点语音按钮结束。');
    } catch (error) {
      clearTimeout(ackTimer);
      await dispose(rec);
      if (capture === rec) capture = null;
      if (activeId === rec.id) activeId = null;
      controls(false);
      message(error?.cancelled ? '已取消录音请求。' : error?.name === 'NotAllowedError' ? '麦克风权限被拒绝，请在浏览器地址栏允许麦克风。' :
        '无法启动麦克风，请检查设备与浏览器。');
    } finally {
      requesting = false;
      const cancelButton = document.querySelector('#voice-cancel');
      if (cancelButton) cancelButton.hidden = !(capture || waiting);
    }
  };
  // 手动编辑侦测：录音中用户改动受管区间则停止自动替换，保留手工修改。
  const isComposer = (target) => !!(target && target.matches && target.matches('#task-composer textarea'));
  document.addEventListener('beforeinput', (event) => {
    if (!isComposer(event.target)) return;
    lastEditStart = event.target.selectionStart ?? -1;
    lastEditEnd = event.target.selectionEnd ?? lastEditStart;
  }, true);
  document.addEventListener('input', (event) => {
    if (!isComposer(event.target)) return;
    const input = event.target;
    const value = input.value || '';
    if (applyingVoice) { lastComposerValue = value; return; }
    // 听写填入后用户改过文字：来源仍记为听写，但标记 edited，避免冒充原始 ASR 输出。
    window.LUX_INTAKE?.markDictationEdited?.();
    if (composing) return; // 组合期间不判定，待 compositionend 后合并
    const prev = lastComposerValue;
    lastComposerValue = value;
    if (!voiceRegion) return;
    const delta = value.length - prev.length;
    if (lastEditStart < 0 || lastEditEnd < 0) {
      voiceRegion.manual = true;
      message('检测到输入变化，已停止自动更新语音文字；识别结果请手动合并。');
      return;
    }
    if (lastEditEnd <= voiceRegion.start) {
      voiceRegion.start += delta; voiceRegion.end += delta; // 区间之前的手动输入原样保留并平移
      return;
    }
    if (lastEditStart >= voiceRegion.end) return; // 区间之后的手动输入不覆盖
    voiceRegion.manual = true;
    message('检测到手动编辑，已停止自动更新语音文字；识别结果请手动合并。');
  }, true);
  document.addEventListener('compositionstart', (event) => { if (isComposer(event.target)) composing = true; }, true);
  document.addEventListener('compositionend', (event) => {
    if (!isComposer(event.target)) return;
    composing = false;
    if (pendingVoiceResult) {
      const pending = pendingVoiceResult; pendingVoiceResult = null;
      setTimeout(() => applyVoiceResult(pending), 0);
    }
  }, true);
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#task-send')) return;
    if (capture || waiting) {
      event.preventDefault();
      event.stopImmediatePropagation();
      message(capture ? '请先结束语音输入，再发送内容。' : '正在校正语音文字，请稍候再发送。');
    }
  }, true);
  document.addEventListener('click', (event) => {
    if (event.target.closest('#voice-cancel')) { cancel(); return; }
    if (event.target.closest('#task-voice')) { if (capture) stop(); else start(); }
    const tab = event.target.closest('.lux-tabs [role="tab"]');
    if (tab && tab.textContent.trim() !== '智能工作台') stop();
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', () => {
    permissionGeneration++; clearTimeout(ackTimer); clearTimeout(waitTimer);
    if (capture) dispose(capture);
  });
}
