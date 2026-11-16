import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import ts from 'typescript';

// 直接加载生产 TypeScript；不复制实现，也不增加测试框架依赖。
function moduleUrl(file, replacements = {}) {
  let source = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  for (const [from, to] of Object.entries(replacements)) source = source.replace(from, to);
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
}
const modelUrl = moduleUrl('../src/features/chat/messageModel.ts');
const { ChatApiError, chatApiErrorFromPayload, chatApiErrorFromResponse } = await import(
  moduleUrl('../../shared/chat/errors.ts'),
);
const { normalizeToolCallStatus } = await import(modelUrl);
const { loadStored, saveStored } = await import(moduleUrl(
  '../src/features/chat/messageStorage.ts', { '"./messageModel"': JSON.stringify(modelUrl) },
));
let raw;
globalThis.localStorage = {
  getItem: () => raw,
  setItem: (_key, value) => { raw = value; },
};

test('unknown and running tools never display completion', () => {
  for (const status of [undefined, 'new_status', 'running', 'queued', 'interrupted_unknown']) {
    assert.equal(normalizeToolCallStatus(status), 'execution_unknown');
  }
  assert.equal(normalizeToolCallStatus('committed'), 'completed');
  assert.equal(normalizeToolCallStatus('execution_failed'), 'failed');
  assert.equal(normalizeToolCallStatus('preempted'), 'cancelled');
});
test('legacy history receives stable ids and empty placeholders are removed', () => {
  raw = JSON.stringify({ threadId: 't', messages: [
    { role: 'user', content: 'hello', traceId: '' },
    { role: 'agent', content: '', traceId: '' },
    { id: 'existing', role: 'agent', content: 'done', traceId: 'trace' },
  ] });
  const state = loadStored();
  assert.equal(state.threadId, 't');
  assert.equal(state.messages.length, 2);
  assert.ok(state.messages[0].id);
  assert.equal(state.messages[1].id, 'existing');
});
test('cache bounds history and tool results without mutating live messages', () => {
  const messages = Array.from({ length: 205 }, (_, id) => ({
    id: String(id), role: 'agent', content: 'ok', traceId: '', threadId: 't',
    toolCalls: [{ id: 'tool', name: 'shell', params: {}, status: 'completed', result: 'x'.repeat(5000) }],
  }));
  saveStored('t', messages);
  const saved = JSON.parse(raw);
  assert.equal(saved.messages.length, 200);
  assert.equal(saved.messages[0].id, '5');
  assert.ok(saved.messages[0].toolCalls[0].result.length < 4100);
  assert.equal(messages[0].toolCalls[0].result.length, 5000);
});
test('corrupt cache and unavailable storage do not crash the page', () => {
  raw = '{broken';
  assert.deepEqual(loadStored(), { threadId: undefined, messages: [] });
  const storage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem() { throw new Error('denied'); },
    setItem() { throw new Error('quota'); },
  };
  try {
    assert.deepEqual(loadStored(), { threadId: undefined, messages: [] });
    assert.doesNotThrow(() => saveStored('t', []));
  } finally { globalThis.localStorage = storage; }
});

test('chat error payload keeps structured fields and draft semantics', () => {
  const error = chatApiErrorFromPayload({
    message: '稍后再试', code: 'turn_in_progress', status: 202, retryable: true,
    trace_id: 'trace-1', draft_recoverable: true, retry_after_seconds: 3,
  });
  assert.ok(error instanceof ChatApiError);
  assert.equal(error.name, 'ChatApiError');
  assert.equal(error.code, 'turn_in_progress');
  assert.equal(error.status, 202);
  assert.equal(error.retryable, true);
  assert.equal(error.traceId, 'trace-1');
  assert.equal(error.draftRecoverable, true);
  assert.equal(error.retryAfterSeconds, 3);
});

test('http and sse failures share the same structured parsing', async () => {
  const nested = await chatApiErrorFromResponse(new Response(JSON.stringify({
    detail: { message: '冲突', code: 'turn_conflict', status: 409, retryable: false, draft_recoverable: true, trace_id: 't-2' },
  }), { status: 409 }));
  assert.equal(nested.code, 'turn_conflict');
  assert.equal(nested.status, 409);
  assert.equal(nested.draftRecoverable, true);
  assert.equal(nested.traceId, 't-2');

  const plain = await chatApiErrorFromResponse(new Response(JSON.stringify({ detail: '拒绝' }), { status: 403 }));
  assert.equal(plain.message, '拒绝');
  assert.equal(plain.status, 403);
  assert.equal(plain.code, 'chat_stream_failed');
  assert.equal(plain.draftRecoverable, false);

  const text = await chatApiErrorFromResponse(new Response('boom', { status: 500 }));
  assert.equal(text.status, 500);
  assert.equal(text.retryable, false);
  assert.match(text.message, /500/);
});

const { readChatEventStream } = await import(moduleUrl('../../shared/chat/stream.ts'));
const encoder = new TextEncoder();
function streamResponse(chunks) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
      controller.close();
    },
  }));
}
async function eventsFrom(chunks) {
  const events = [];
  await readChatEventStream(streamResponse(chunks), event => events.push(event));
  return events;
}

test('shared SSE parses a single event', async () => {
  assert.deepEqual(await eventsFrom(['event: token\ndata: {"text":"hello"}\n\n']), [
    { event: 'token', data: { text: 'hello' } },
  ]);
});

test('shared SSE preserves consecutive event order', async () => {
  assert.deepEqual(await eventsFrom(['event: token\ndata: {"i":1}\n\nevent: done\ndata: {"i":2}\n\n']), [
    { event: 'token', data: { i: 1 } }, { event: 'done', data: { i: 2 } },
  ]);
});

test('shared SSE handles CRLF split across chunks at every byte boundary', async () => {
  const bytes = encoder.encode('event: token\r\ndata: {"text":"你好🍊"}\r\n\r\n');
  const expected = [{ event: 'token', data: { text: '你好🍊' } }];
  for (let i = 1; i < bytes.length; i++) {
    assert.deepEqual(await eventsFrom([bytes.slice(0, i), bytes.slice(i)]), expected);
  }
});

test('shared SSE decodes UTF-8 characters across one-byte chunks', async () => {
  const bytes = encoder.encode('event: token\ndata: {"text":"你好🍊"}\n');
  assert.deepEqual(await eventsFrom([...bytes].map(byte => Uint8Array.of(byte))), [
    { event: 'token', data: { text: '你好🍊' } },
  ]);
});

test('shared SSE skips invalid JSON and continues with valid events', async () => {
  assert.deepEqual(await eventsFrom(['event: bad\ndata: {broken}\n\nevent: done\ndata: {}\n']), [
    { event: 'done', data: {} },
  ]);
});

test('shared SSE ignores empty data lines', async () => {
  assert.deepEqual(await eventsFrom(['event: token\ndata:\ndata:   \ndata: {}\n']), [
    { event: 'token', data: {} },
  ]);
});

test('shared SSE emits the final event without a trailing newline', async () => {
  assert.deepEqual(await eventsFrom(['event: done\ndata: {"ok":true}']), [
    { event: 'done', data: { ok: true } },
  ]);
});

test('shared SSE propagates callback errors and releases the reader', async () => {
  for (const ending of ['\n\n', '']) {
    const response = streamResponse(['event: done\ndata: {}' + ending]);
    const error = new Error('callback failure');
    await assert.rejects(readChatEventStream(response, () => { throw error; }), e => e === error);
    assert.equal(response.body.locked, false);
  }
});

test('shared SSE rejects a response without a body', async () => {
  await assert.rejects(readChatEventStream(new Response(null), () => {}), /服务器没有返回流式响应/);
});
