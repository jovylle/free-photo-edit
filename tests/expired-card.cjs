// Focused DOM test of the existing card renderer; no browser/API/provider calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const cardSource = source.slice(source.indexOf('  function cardFor(j)'), source.indexOf('  async function loadHistory()'));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; }
  querySelector(tag) { const child = new Element(tag); this.children.push(child); return child; }
  appendChild(child) { this.children.push(child); }
}
const context = vm.createContext({document: {createElement: tag => new Element(tag)}, fmtTime: () => 'date'});
vm.runInContext(cardSource + '\nthis.render = cardFor;', context);
for (const status of ['done', 'failed']) {
  const card = context.render({status, preset: 'custom', artifacts_expired: true, inputs: [], image_url: null});
  const elements = [];
  function visit(e) { elements.push(e); e.children.forEach(visit); }
  visit(card);
  assert(elements.some(e => /expired/i.test(e.textContent)), 'expired label missing');
  assert(!elements.some(e => ['img', 'button', 'input'].includes(e.tag)), 'expired files must have no images/download/compare');
  assert(!elements.some(e => /Editing/.test(e.textContent)), 'expired done must not look running');
}
console.log('expired card: done and failed passed');
