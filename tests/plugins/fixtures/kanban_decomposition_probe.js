// Exercise the browser bundle's actual creation component, not a reimplementation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('  function InlineCreate(props)');
const end = source.indexOf('  function TaskDrawer(props)', start);
assert(start >= 0 && end > start);
let states = [], cursor = 0;
const context = {
  useI18n: () => ({ t: () => '' }),
  useState: value => {
    const index = cursor++;
    if (!(index in states)) states[index] = value;
    return [states[index], value => { states[index] = value; }];
  },
  h: (type, props, ...children) => ({ type, props: props || {}, children: children.flat().filter(Boolean) }),
  tx: (_t, _key, fallback) => fallback,
  getColumnLabel: (_t, name) => name,
  selectChangeHandler: setter => ({ onChange: e => setter(e.target.value) }),
  Label: 'Label', Input: 'Input', Button: 'Button', Select: 'Select', SelectOption: 'SelectOption',
};
vm.createContext(context);
const component = vm.runInContext(source.slice(start, end) + '\nInlineCreate;', context);
function nodes(tree) { return typeof tree !== 'object' ? [] : [tree, ...tree.children.flatMap(nodes)]; }
const payloads = [];
const props = { columnName: 'triage', onSubmit: payload => payloads.push(JSON.parse(JSON.stringify(payload))) };
const render = () => { cursor = 0; return component(props); };
function submit() { nodes(render()).find(n => n.type === 'form').props.onSubmit({ preventDefault() {} }); }
function title() { nodes(render()).find(n => n.type === 'textarea').props.onChange({ target: { value: 'Implement bounded scope' } }); }
function safety() { return nodes(render()).find(n => n.type === 'label' && n.children.includes('I authorise worker decomposition: no approval or external gate is required')).children[0]; }
assert.equal(safety().props.checked, false);
title(); submit();
assert.equal(payloads[0].decomposition, undefined);
title(); safety().props.onChange({ target: { checked: true } }); submit();
assert.deepEqual(payloads[1].decomposition, { decomposable: true });
assert.equal(safety().props.checked, false, 'submission resets the grant');
props.columnName = 'ready'; title(); submit();
assert.equal(payloads[2].decomposition, undefined);
console.log(JSON.stringify(payloads));
