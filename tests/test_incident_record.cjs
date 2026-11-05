// Browser-event contract without microphone/network dependencies.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const listeners = {};
class Input {
  constructor() { this.v = ''; }
  get value() { return this.v; }
  set value(v) { this.v = v; }
  dispatchEvent() {}
}
const hints = Object.fromEntries(['task-intake', 'incident-record', 'incident-edit', 'incident-question']
  .map(key => [key, new Input()]));
const toggle = {dataset: {}, setAttribute() {}, classList: {toggle() {}}, addEventListener() {}};
const document = {
  documentElement: {dataset: {}}, readyState: 'complete',
  querySelector(selector) {
    if (selector === '#task-record-toggle') return toggle;
    for (const [key, input] of Object.entries(hints)) if (selector.startsWith('#' + key + '-hint ')) return input;
    return null;
  },
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
  createElement() { return {}; }, head: {appendChild() {}},
};
let id = 0;
const crypto = {randomUUID: () => 'test-' + (++id)};
const window = {crypto};
const context = vm.createContext({document, window, crypto, HTMLTextAreaElement: Input,
  HTMLInputElement: Input, Event: class {}, setTimeout: fn => fn()});
vm.runInContext('(' + fs.readFileSync(path.join(__dirname, '../frontend/incident_record.js'), 'utf8') + ')()', context);
const fire = (name, event) => (listeners[name] || []).forEach(fn => fn(event));
const click = id => fire('click', {target: {closest: selector => selector === '#' + id ? {} : null}});
const hint = () => JSON.parse(hints['task-intake'].value);
click('task-send');
const initial = hint();
assert.equal(initial.channel, 'manual');
assert.ok(initial.conversation_id);
click('task-send');
assert.equal(hint().submission_id, initial.submission_id);
fire('lux-intake:result', {detail: {intake: true, saved: false}});
click('task-send');
assert.equal(hint().submission_id, initial.submission_id);
fire('lux-intake:result', {detail: {intake: true, saved: true}});
click('task-send');
assert.notEqual(hint().submission_id, initial.submission_id);
window.LUX_INTAKE.markVoiceAssistant();
click('task-send');
assert.equal(hint().channel, 'voice_assistant');
assert.equal(hint().conversation_id, initial.conversation_id);
click('incident-question-save');
const questionId = hints['incident-question'].value;
assert.ok(questionId);
click('incident-question-save');
assert.equal(hints['incident-question'].value, questionId);
const app = fs.readFileSync(path.join(__dirname, '../app.py'), 'utf8');
assert.ok(app.includes("document.dispatchEvent(new CustomEvent('lux-tts:answer-ready'))"));
console.log('PASS: typed/voice source, stable retry IDs, question IDs, TTS event target');
