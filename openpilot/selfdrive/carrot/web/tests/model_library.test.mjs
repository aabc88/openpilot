import assert from 'node:assert/strict';
import test from 'node:test';
import { renderLibrary } from '../src/features/tools/model_library.js';
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.events = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelector(tag) { return this.children.find(e => e.tag === tag) || this.children.map(e => e.querySelector?.(tag)).find(Boolean); }
}
function setupDocument() {
  const elements = new Map();
  for (const id of ['externalModelLibrary', 'modelLibraryDialog', 'modelLibraryEditor',
    'modelLibraryTitle', 'btnCloseModelLibrary', 'egpuModelCard', 'egpuModelState', 'egpuModelAmount',
    'internalModelLibrary', 'internalModelLibraryTitle', 'internalModelSummary', 'internalModelChoices', 'internalModelStatus']) {
    elements.set(id, new Element('div'));
  }
  elements.get('egpuModelCard').hidden = true;
  globalThis.document = { getElementById: id => elements.get(id), createElement: tag => new Element(tag) };
  return { root: elements.get('externalModelLibrary'), editor: elements.get('modelLibraryEditor'), elements };
}
const data = { effective: 'n16', selected: 'flip-flop', reboot_required: true, models: [
  { id: 'n16', name: 'N16 Nav', noo: true, hidden: false, protected: true, egpu_bytes: 100, jetson_bytes: null },
  { id: 'flip-flop', name: 'Flip Flop', noo: false, hidden: false, protected: true, egpu_bytes: 200, jetson_bytes: 300 },
] };
test('model choice distinguishes current/pending/NOO and preserves in-flight choice across polls', t => {
  const old = globalThis.document;
  t.after(() => { globalThis.document = old; });
  const { root, editor, elements } = setupDocument();
  renderLibrary(data);
  const select = editor.querySelector('select');
  assert.equal(select.value, 'flip-flop');
  assert.deepEqual(select.children.map(x => x.value), ['n16', 'flip-flop']);
  assert.match(select.children[0].textContent, /NOO supported/);
  assert.match(select.children[1].textContent, /NOO not supported/);
  assert.match(root.querySelector('p').textContent, /This boot N16/);
  assert.equal(root.querySelector('strong').textContent, 'Flip Flop');
  assert.equal(root.querySelector('select'), undefined);
  assert.equal(root.querySelector('button').textContent, 'Change model');
  assert.equal(root.children.length, 2);
  root.querySelector('button').events.click();
  assert.equal(elements.get('modelLibraryDialog').open, true);
  elements.get('btnCloseModelLibrary').events.click();
  assert.equal(elements.get('modelLibraryDialog').open, false);
  assert.equal(root.children.some(x => x.className === 'model-library__row'), false);
  select.value = 'n16';
  select.disabled = true;
  renderLibrary({ ...data, jetson_inventory_fresh: true });
  assert.equal(editor.querySelector('select').value, 'n16');
  renderLibrary(data, true);
  assert.equal(editor.querySelector('select').value, 'flip-flop');
});

test('selection alone saves next boot and exposes no deletion action', async t => {
  const old = { document: globalThis.document, postJson: globalThis.postJson };
  t.after(() => Object.assign(globalThis, old));
  const { root, editor, elements } = setupDocument();
  const calls = [];
  globalThis.postJson = async (url, body) => { calls.push({url, body}); return {library: {...data, selected: body.model}}; };
  renderLibrary(data, true);
  const select = editor.querySelector('select');
  select.value = 'n16';
  await select.events.change();
  assert.deepEqual(calls, [{url: '/api/egpu/model/library', body: {action:'select',model:'n16'}}]);
  assert.equal(root.querySelector('button').textContent, 'Change model');
  assert.equal(editor.querySelector('select').value, 'n16');
});

test('failed selection restores the saved choice', async t => {
  const old = { document: globalThis.document, postJson: globalThis.postJson };
  t.after(() => Object.assign(globalThis, old));
  const { root, editor, elements } = setupDocument();
  globalThis.postJson = async () => { throw new Error('offline'); };
  renderLibrary(data, true);
  const select = editor.querySelector('select');
  select.value = 'n16';
  await select.events.change();
  assert.equal(editor.querySelector('select').value, 'flip-flop');
  assert.ok(!editor.querySelector('select').disabled);
});


test('status updates preserve the open dialog and focused selection across inventory polls', t => {
  const old = globalThis.document;
  t.after(() => { globalThis.document = old; });
  const { root, editor, elements } = setupDocument();
  const card = elements.get('egpuModelCard');
  card.hidden = false;
  card.dataset.state = 'downloading';
  elements.get('egpuModelState').textContent = 'Downloading';
  elements.get('egpuModelAmount').textContent = '25%';
  renderLibrary({ ...data, selected: 'n16', reboot_required: false });
  root.querySelector('button').events.click();
  const button = root.querySelector('button');
  const select = editor.querySelector('select');
  assert.equal(root.querySelector('p').textContent, 'Downloading · 25%');
  card.dataset.state = 'error';
  elements.get('egpuModelState').textContent = 'eGPU unavailable';
  elements.get('egpuModelAmount').textContent = '';
  renderLibrary({ ...data, selected: 'n16', reboot_required: false, jetson_inventory_fresh: true });
  assert.equal(editor.querySelector('select'), select);
  assert.equal(root.querySelector('button'), button);
  assert.equal(elements.get('modelLibraryDialog').open, true);
  assert.equal(root.dataset.state, 'error');
  assert.equal(root.querySelector('p').textContent, 'eGPU unavailable');
});

const internal = {
  selected: 'cd210', effective: 'cd210', can_change: true, can_prepare: true, preparing: false, error: null,
  models: [{id: 'cd210', name: 'CD210', ready: true}, {id: 'polaris-vega', name: 'Polaris Vega', ready: false}],
};

test('without an external model the internal dialog prepares, then explicitly selects for the next ignition', async t => {
  const old = {document: globalThis.document, postJson: globalThis.postJson};
  t.after(() => Object.assign(globalThis, old));
  const {root, editor, elements} = setupDocument();
  const calls = [];
  const ready = {...internal, models: internal.models.map(model => ({...model, ready: true}))};
  globalThis.postJson = async (url, body) => {
    calls.push({url, body});
    return {ok: true, internal: body.action === 'prepare' ? {...internal, preparing: true} : {...ready, selected: body.model}};
  };
  renderLibrary(undefined, false, internal);
  assert.equal(root.hidden, false);
  assert.equal(editor.hidden, true);
  root.querySelector('button').events.click();
  assert.equal(elements.get('modelLibraryDialog').open, true);
  assert.equal(elements.get('internalModelLibrary').hidden, false);
  const vega = elements.get('internalModelChoices').children[1].querySelector('button');
  assert.equal(vega.textContent, 'Prepare');
  await vega.events.click();
  assert.equal(vega.disabled, true);
  assert.match(elements.get('internalModelStatus').textContent, /Downloading and preparing/);
  assert.match(elements.get('internalModelSummary').textContent, /Next ignition · CD210/);
  renderLibrary(data, false, ready);
  assert.equal(vega, elements.get('internalModelChoices').children[1].querySelector('button'));
  assert.equal(vega.textContent, 'Select');
  assert.equal(vega.disabled, false);
  assert.equal(editor.hidden, false);
  assert.equal(editor.querySelector('select').value, 'flip-flop');
  await vega.events.click();
  assert.match(elements.get('internalModelSummary').textContent, /Running · CD210 · Next ignition · Polaris Vega/);
  assert.deepEqual(calls, [
    {url: '/api/egpu/model/internal', body: {action: 'prepare', model: 'polaris-vega'}},
    {url: '/api/egpu/model/internal', body: {action: 'select', model: 'polaris-vega'}},
  ]);
});

test('internal model actions respect vehicle and device gates and reflect preparation failures', async t => {
  const old = {document: globalThis.document, postJson: globalThis.postJson};
  t.after(() => Object.assign(globalThis, old));
  const {elements} = setupDocument();
  const status = elements.get('internalModelStatus');
  let calls = 0;
  globalThis.postJson = async () => { calls++; throw new Error('offline'); };
  renderLibrary(data, false, {...internal, can_change: false}, true);
  const vega = elements.get('internalModelChoices').children[1].querySelector('button');
  assert.equal(vega.disabled, true);
  assert.match(status.textContent, /Turn off the ignition/);
  await vega.events.click();
  renderLibrary(data, false, {...internal, can_prepare: false}, true);
  assert.equal(vega.disabled, true);
  assert.match(status.textContent, /cannot prepare/);
  renderLibrary(data, false, internal, false);
  assert.equal(vega.disabled, true);
  assert.match(status.textContent, /Unable to refresh/);
  await vega.events.click();
  assert.equal(calls, 0);
  renderLibrary(data, false, {...internal, error: 'runtime_rejected'}, true);
  assert.equal(vega.disabled, false);
  assert.match(status.textContent, /Polaris Vega could not start/);
  renderLibrary(data, false, {...internal, error: 'preparation_interrupted'}, true);
  assert.equal(vega.disabled, false);
  assert.match(status.textContent, /Preparation was interrupted/);
  renderLibrary(data, false, internal, true);
  await vega.events.click();
  assert.equal(calls, 1);
  assert.match(status.textContent, /request failed/);
  assert.equal(vega.disabled, false);
  assert.match(elements.get('internalModelSummary').textContent, /Next ignition · CD210/);
});
