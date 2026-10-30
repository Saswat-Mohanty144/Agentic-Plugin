// Adversarial audit tests for copilot SDK: SSE parsing, resilience to stream cuts,
// fallback mechanics, audio withholding, and event handling.
// Run: node --test sdk/test/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../copilot.js');

test('parseSseFrames parses full standard streaming events', () => {
  const streamText = [
    'data: {"event":"token","data":"Hello "}\n\n',
    'data: {"event":"token","data":"world!"}\n\n',
    'data: {"event":"tool_call","data":{"tool":"test_tool","args":{}}}\n\n',
    'data: {"event":"done","data":{"finish_reason":"stop","truncated":false}}\n\n'
  ].join('');

  const result = core.parseSseFrames(streamText);
  assert.equal(result.events.length, 4);
  assert.equal(result.remaining, '');

  assert.deepEqual(result.events[0], { event: 'token', data: 'Hello ' });
  assert.deepEqual(result.events[1], { event: 'token', data: 'world!' });
  assert.equal(result.events[2].event, 'tool_call');
  assert.equal(result.events[2].data.tool, 'test_tool');
  assert.equal(result.events[3].event, 'done');
});

test('parseSseFrames resilience: stream cut mid-frame preserves remaining buffer', () => {
  // A chunk cut mid-data line
  const partialChunk = 'data: {"event":"token","data":"First"}\n\ndata: {"event":"token","data":"Sec';
  const result1 = core.parseSseFrames(partialChunk);

  assert.equal(result1.events.length, 1);
  assert.deepEqual(result1.events[0], { event: 'token', data: 'First' });
  assert.equal(result1.remaining, 'data: {"event":"token","data":"Sec');

  // Next chunk arrives completing the frame
  const nextChunk = result1.remaining + 'ond"}\n\n';
  const result2 = core.parseSseFrames(nextChunk);

  assert.equal(result2.events.length, 1);
  assert.deepEqual(result2.events[0], { event: 'token', data: 'Second' });
  assert.equal(result2.remaining, '');
});

test('parseSseFrames resilience: ignores malformed or non-json lines without crashing', () => {
  const malformed = [
    'data: {invalid json}\n\n',
    'random text without data prefix\n\n',
    ': comment line\n\n',
    'data: {"event":"done","data":{"finish_reason":"stop"}}\n\n'
  ].join('');

  const result = core.parseSseFrames(malformed);
  // Malformed JSON is ignored, done event is preserved
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].event, 'done');
});

test('describeHttpError formats error status and code', () => {
  assert.equal(core.describeHttpError(401, { error: 'REAUTH_REQUIRED' }), 'Request failed (401): REAUTH_REQUIRED');
  assert.equal(core.describeHttpError(403, { error: 'SAME_PERSON' }), 'Request failed (403): SAME_PERSON');
  assert.equal(core.describeHttpError(409, { error: 'REVIEW_REQUIRED' }), 'Request failed (409): REVIEW_REQUIRED');
  assert.equal(core.describeHttpError(503, { error: 'AUDIT_SIGNING_NOT_CONFIGURED' }), 'Request failed (503): AUDIT_SIGNING_NOT_CONFIGURED');
  assert.equal(core.describeHttpError(504, {}), 'Request failed (504)');
});

// ==============================================================================
// CONFIRMED BUGS
// ==============================================================================

test('BUG-6: parseSseFrames must preserve thinking and history_trimmed events emitted by backend', () => {
  // Backend orchestrator (orchestrator.py:192, 196) emits "thinking" and "history_trimmed" events.
  // In sdk/copilot.js:156, KNOWN_SSE_EVENTS omits them, so parseSseFrames silently drops them.
  const text = [
    'data: {"event":"thinking","data":"Synthesizing payroll rules..."}\n\n',
    'data: {"event":"history_trimmed","data":{"dropped_turns":2}}\n\n',
    'data: {"event":"token","data":"Result"}\n\n'
  ].join('');

  const result = core.parseSseFrames(text);
  const eventTypes = result.events.map(e => e.event);

  assert.ok(
    eventTypes.includes('thinking'),
    `parseSseFrames dropped 'thinking' event. Parsed events: ${JSON.stringify(eventTypes)}`
  );
  assert.ok(
    eventTypes.includes('history_trimmed'),
    `parseSseFrames dropped 'history_trimmed' event. Parsed events: ${JSON.stringify(eventTypes)}`
  );
});
