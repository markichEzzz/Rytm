const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/ingestion/admin/candidate.js'), 'utf8');

function form(status) {
  const row = {hidden:false};
  const listeners = {};
  const select = {value:status, addEventListener:(event, callback) => {listeners[event] = callback;}};
  const price = {value:'700', disabled:false, required:false, closest:() => row};
  const document = {readyState:'complete', querySelectorAll:() => [],
    getElementById:id => ({id_price_status:select, id_price:price}[id] || null)};
  vm.runInNewContext(source, {document, window:{}});
  return {row, price, change:value => {select.value = value; listeners.change();}};
}
for (const status of ['paid', 'free', 'unknown']) {
  test(`${status} controls the entire price row on initial load`, () => {
    const {row, price} = form(status);
    assert.equal(row.hidden, status !== 'paid');
    assert.equal(price.disabled, status !== 'paid');
    assert.equal(price.required, status === 'paid');
  });
}
test('changing status toggles immediately without reloading', () => {
  const {row, price, change} = form('unknown');
  for (const status of ['paid', 'free', 'unknown', 'paid']) {
    change(status);
    assert.equal(row.hidden, status !== 'paid');
    assert.equal(price.disabled, status !== 'paid');
    assert.equal(price.required, status === 'paid');
  }
});
