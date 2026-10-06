const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('      function applyPatch(patch) {');
const end = source.indexOf('\n    };', start);
assert(start >= 0 && end > start);
const body = source.slice(start, end);
async function run(failAt) {
  const calls = []; let data, error;
  const SDK = {fetchJSON: async (url, opts) => {
    const method = opts ? opts.method : 'GET'; calls.push(method);
    if (method === failAt) throw new Error(method + ' refused');
    return {task: {priority: 7}};
  }};
  const props = {taskId: 'existing', onRefresh: () => calls.push('REFRESH')};
  const API = '/api'; const boardSlug = 'board';
  const withBoard = x => x; const withCompletionSummary = x => x;
  const setPatchErr = x => {error = x}; const setData = x => {data = x};
  const setErr = () => {}; const parseApiErrorMessage = e => e.message;
  const load = () => {throw Error('priority must use awaited readback')};
  const writer = eval('(' + body.trim() + ')');
  if (failAt) await assert.rejects(writer({priority: 7}), new RegExp(failAt + ' refused'));
  else await writer({priority: 7});
  assert.deepEqual(calls, failAt === 'PATCH' ? ['PATCH'] : failAt === 'GET' ? ['PATCH', 'GET'] : ['PATCH', 'GET', 'REFRESH']);
  if (failAt) assert.equal(error, failAt + ' refused');
  else assert.equal(data.task.priority, 7);
}
(async () => {await run('PATCH'); await run('GET'); await run(null); console.log('PASS actual priority writer PATCH/GET rejection and awaited readback');})().catch(e => {console.error(e); process.exitCode = 1});
