// Run with: node scripts/verify_chat_reconnect.cjs
// Executes production callbacks extracted with TypeScript AST; network and React setters are mocked.
const ts = require('../frontend/node_modules/typescript');
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function parse(path) {
  return ts.createSourceFile(path, fs.readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, child => find(child, predicate));
}
function callback(root, name) {
  const decl = find(root, n => ts.isVariableDeclaration(n) && n.name.getText(root) === name);
  return decl.initializer.arguments[0].getText(root);
}
function effect(root, marker) {
  const call = find(root, n => ts.isCallExpression(n) && n.expression.getText(root) === 'useEffect'
    && n.arguments[0].getText(root).includes(marker));
  return call.arguments[0].getText(root);
}
function evaluate(code, context) {
  return vm.runInContext(ts.transpileModule(code, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText, context);
}
async function run(platform) {
  const web = platform === 'Web';
  const root = parse(web ? 'frontend/src/pages/ChatPage.tsx' : 'apps/android/src/App.tsx');
  const sockets = [], timers = [];
  let requests = 0, messages = [], server = [], state;
  const noop = () => {};
  const local = { current: false }, pending = { current: false };
  const ctx = vm.createContext({
    console, configured: true, threadId: 'thread', BASE_PATH: '/',
    window: { location: { protocol: 'https:', host: 'localhost' } },
    appConfig: { backendBaseUrl: 'https://localhost' }, getToken: () => 'test-token',
    sendWsAuth: socket => socket.send(JSON.stringify({ type: 'auth', access_token: 'test-token' })),
    WebSocket: class {
      constructor(url) { this.url = url; sockets.push(this); }
      send(frame) { this.auth = JSON.parse(frame); }
      close() { this.onclose?.(); }
    },
    setTimeout: fn => { timers.push(fn); return fn; },
    clearTimeout: fn => { const i = timers.indexOf(fn); if (i >= 0) timers.splice(i, 1); },
    inFlightRef: local, streamingRef: local, historyRefreshPendingRef: pending,
    remoteTurnActiveRef: { current: false }, activeTurnRecordIdRef: { current: undefined },
    activeStreamThreadIdRef: { current: 'thread' }, abortRef: { current: null },
    prevMsgCountRef: { current: 0 },
    setMessages: value => { messages = typeof value === 'function' ? value(messages) : value; },
    setExecState: value => { state = value; }, setRemoteRunning: noop,
    setUnreadCount: noop, setHistoryCursor: noop, setHasMoreHistory: noop, setError: noop,
    setLoading: noop, setStreaming: noop, statusSynced: true,
    controller: { signal: { aborted: false } },
    getThreadMessages: async () => { requests++; return { messages: structuredClone(server), has_more: false }; },
    historyToMessages: rows => rows.map(row => ({ ...row, id: row.event_id })),
    genMessageId: () => { throw Error('server messages must have IDs'); },
    normalizeToolCallStatus: value => value, atBottomRef: { current: false },
  });
  const historyName = web ? 'loadRemoteMessages' : 'refreshHistory';
  const statusName = web ? 'applyExecutionStatus' : 'applyStatus';
  evaluate(`globalThis.${historyName} = ${callback(root, historyName)};\nglobalThis.${statusName} = ${callback(root, statusName)};`, ctx);
  if (!web) {
    const api = parse('apps/android/src/api/chat.ts');
    const fn = find(api, n => ts.isFunctionDeclaration(n) && n.name?.text === 'subscribeThreadEvents');
    evaluate(fn.getText(api).replace('export ', ''), ctx);
  }
  const cleanup = evaluate(`(${effect(root, web ? 'new WebSocket' : 'subscribeThreadEvents')})()`, ctx);
  const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
  const status = async (value, interrupting = false) => {
    sockets.at(-1).onmessage({ data: JSON.stringify({ type: 'execution_status', data: {
      state: value, interrupting, thread_id: 'thread', active_turn: value === 'running' ? { turn_record_id: 'turn' } : null,
    } }) });
    await tick();
  };
  const reconnect = () => { sockets.at(-1).close(); assert.equal(timers.length, 1); timers.shift()(); };
  sockets.at(-1).onopen();
  assert.equal(sockets.at(-1).auth.access_token, 'test-token');
  assert.match(sockets.at(-1).url, /^wss:/);
  await status('idle'); assert.equal(requests, 1);
  // A: miss an entire remote Turn, then reconnect to idle. Equal length / same text must still replace.
  messages = [{ id: 'old-user', content: 'same' }, { id: 'old-agent', content: 'same' }];
  server = [{ event_id: 'user', role: 'user', content: 'same', tool_calls: [] },
    { event_id: 'agent', role: 'agent', content: 'same', tool_calls: [] }];
  reconnect(); await status('idle');
  assert.equal(requests, 2); assert.deepEqual(messages.map(m => m.id), ['user', 'agent']);
  await status('idle'); assert.equal(requests, 2);
  console.log(`${platform} A PASS: reconnect idle restores missed Turn without duplicates`);
  // B: first running snapshot loads partial history; interrupting keeps remote flag until idle.
  reconnect(); await status('running'); assert.equal(requests, 3); assert.equal(state, 'running');
  await status('running', true); assert.equal(requests, 3);
  assert.equal(ctx.remoteTurnActiveRef.current, true);
  await status('idle'); assert.equal(requests, 4); assert.equal(state, 'idle');
  // First idle also matches remote completion: one request, not two.
  await status('running'); reconnect(); await status('idle'); assert.equal(requests, 5);
  console.log(`${platform} B PASS: running snapshot + terminal idle refresh, merged triggers`);
  // C: multiple reconnect snapshots while local stream active collapse to one finally refresh.
  local.current = true; messages = [{ id: 'optimistic', content: 'token' }];
  reconnect(); await status('running'); reconnect(); await status('idle');
  assert.equal(requests, 5); assert.equal(messages[0].id, 'optimistic'); assert.equal(pending.current, true);
  const final = find(root, n => ts.isTryStatement(n) && n.finallyBlock
    && n.finallyBlock.getText(root).includes(web ? 'const resolvedThreadId' : 'streamingRef.current = false'));
  await evaluate(`(async () => ${final.finallyBlock.getText(root)})()`, ctx);
  assert.equal(local.current, false); assert.equal(pending.current, false); assert.equal(requests, 6);
  assert.deepEqual(messages.map(m => m.id), ['user', 'agent']);
  console.log(`${platform} C PASS: no overwrite during local stream; one refresh after finally`);
  reconnect(); await status('busy'); assert.equal(requests, 7);
  if (web) {
    const push = { data: JSON.stringify({ type: 'new_message', data: { event_id: 'push', reply: 'hello' } }) };
    sockets.at(-1).onmessage(push); sockets.at(-1).onmessage(push);
    assert.equal(messages.filter(m => m.id === 'push').length, 1);
  }
  // Cancellation clears history, including the remote user's rolled back message.
  await status('running'); server = []; await status('idle');
  assert.equal(requests, 8); assert.equal(messages.length, 0);
  cleanup(); assert.equal(timers.length, 0);
  console.log(`${platform} PASS: busy snapshot, auth, reconnect reset and cleanup`);
}
(async () => { await run('Web'); await run('Android'); })().catch(error => { console.error(error); process.exitCode = 1; });
