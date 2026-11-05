() => {
  if (document.documentElement.dataset.luxIncidentRecordBound === '1') return;
  document.documentElement.dataset.luxIncidentRecordBound = '1';

  // 统一事件受理的前端来源标记。只负责说明“这条消息从哪个入口来”，
  // 用户名、角色、区域与权限仍由服务端认证上下文决定。
  const CONVERSATION = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 'conv-' + Date.now().toString(36);

  const hiddenEl = () => document.querySelector('#task-intake-hint textarea, #task-intake-hint input');
  const recordHintEl = () => document.querySelector('#incident-record-hint textarea, #incident-record-hint input');
  const editHintEl = () => document.querySelector('#incident-edit-hint textarea, #incident-edit-hint input');
  const recordBtn = () => document.querySelector('#task-record-toggle');

  let recordMode = false;   // “现场记录”模式：下一条消息作为事件登记
  let channel = '';         // dictation / voice_assistant；空表示手动输入
  let edited = false;       // 听写文字是否经过人工编辑
  let pendingSubmissionId = null;  // 本轮逻辑提交的稳定编号：失败/回执丢失重试沿用

  const setValue = (el, value) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const newId = () => (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 'id-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

  const submissionId = () => {
    if (!pendingSubmissionId) pendingSubmissionId = newId();
    return pendingSubmissionId;
  };

  const writeHint = () => {
    const el = hiddenEl();
    if (!el) return;
    // Even typed commands need a stable ID and per-page conversation marker.
    setValue(el, JSON.stringify({
      channel: channel || 'manual',
      record: recordMode,
      edited: channel === 'dictation' && edited,
      conversation_id: CONVERSATION,
      message_id: newId(),
      submission_id: submissionId(),
    }));
  };

  const paint = () => {
    const btn = recordBtn();
    if (!btn) return;
    btn.setAttribute('aria-pressed', recordMode ? 'true' : 'false');
    btn.classList.toggle('is-on', recordMode);
    const tip = document.querySelector('#task-record-tip');
    if (tip) {
      tip.textContent = recordMode
        ? '现场记录已开启：下一条消息会保存为异常记录并自动匹配。'
        : '开启“现场记录”后，下一条消息会保存为异常记录并自动匹配。';
    }
  };

  const resetAfterSuccess = () => {
    pendingSubmissionId = null;
    recordMode = false;
    channel = '';
    edited = false;
    writeHint();
    paint();
  };

  const bind = () => {
    const btn = recordBtn();
    if (!btn || btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => {
      recordMode = !recordMode;
      // 切换模式即开始一次新的逻辑提交，避免沿用上一次失败提交的编号。
      pendingSubmissionId = null;
      writeHint();
      paint();
    });
    paint();
  };

  // 发送前写入来源标记。capture 阶段先于 Gradio 的按钮处理。
  document.addEventListener('click', (event) => {
    if (event.target.closest('#task-send')) {
      if (recordMode || channel) submissionId();
      writeHint();
    }
    // 表单入口：点击时若没有稳定编号就生成；成功后由服务端清空，下次自动新建。
    if (event.target.closest('#incident-record-button')) {
      const el = recordHintEl();
      if (el && !el.value.trim()) setValue(el, newId());
    }
    if (event.target.closest('#incident-edit-save')) {
      const el = editHintEl();
      if (el && !el.value.trim()) setValue(el, newId());
    }
    if (event.target.closest('#incident-question-save')) {
      const el = document.querySelector('#incident-question-hint textarea, #incident-question-hint input');
      if (el && !el.value.trim()) setValue(el, newId());
    }
  }, true);

  // 服务端返回登记结果后才清理状态：
  // saved=true 表示事务已提交；失败或回执丢失时保留同一提交编号与记录模式，重试不会重复建档。
  document.addEventListener('lux-intake:result', (event) => {
    let result = event.detail;
    try { if (typeof result === 'string') result = JSON.parse(result); } catch (_) { return; }
    if (!result || !result.intake) {
      // 普通对话：清理来源标记，但不影响下一次独立记录。
      pendingSubmissionId = null;
      channel = '';
      edited = false;
      writeHint();
      return;
    }
    if (result.saved) {
      resetAfterSuccess();
    } else {
      writeHint();  // 保留 pendingSubmissionId，用户可直接重发
    }
  });

  // 供语音听写 / 语音助手脚本调用。
  window.LUX_INTAKE = {
    markDictation(isEdited) {
      channel = 'dictation';
      edited = !!isEdited;
      pendingSubmissionId = null;  // 新识别结果视为一次新的逻辑提交
    },
    markDictationEdited() { if (channel === 'dictation') edited = true; },
    markVoiceAssistant() {
      channel = 'voice_assistant';
      edited = false;
      pendingSubmissionId = null;
    },
    isRecordMode: () => recordMode,
    reset() { recordMode = false; channel = ''; edited = false; pendingSubmissionId = null; paint(); },
  };

  // 样式：与现有工具栏按钮一致，深色/浅色主题都使用主题变量。
  const style = document.createElement('style');
  style.id = 'lux-intake-record-styles';
  style.textContent = [
    '#task-intake-hint,#task-intake-feedback,#incident-record-hint,#incident-edit-hint,#incident-question-hint{display:none!important;}',
    '#task-record-host{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:2px 0 6px;}',
    '#task-record-toggle{font-size:13px;padding:4px 12px;border-radius:6px;',
    'border:1px solid var(--border-color,#3a3f4b);background:var(--bg-secondary,transparent);',
    'color:var(--text-primary,inherit);cursor:pointer;}',
    '#task-record-toggle.is-on{border-color:#e5484d;color:#e5484d;font-weight:600;}',
    '#task-record-tip{font-size:12px;opacity:.75;}',
    '.incident-fold{margin-top:10px;}',
    '.incident-fold summary{cursor:pointer;font-weight:600;padding:6px 0;}',
  ].join('');
  document.head.appendChild(style);
  document.addEventListener('DOMContentLoaded', bind);
  if (document.readyState !== 'loading') setTimeout(bind, 0);
  else document.addEventListener('DOMContentLoaded', bind);
}
