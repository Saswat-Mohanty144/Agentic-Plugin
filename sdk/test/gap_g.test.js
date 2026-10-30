// Tests for Task g (decision D9): Chat streaming via SSE and Voice audio withholding
// Run: node --test sdk/test/gap_g.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// ---------------------------------------------------------------------------------------------------------------
// Lightweight headless DOM & Browser Environment
// ---------------------------------------------------------------------------------------------------------------

class FakeClassList {
  constructor(el) {
    this._el = el;
  }
  get _classes() {
    return (this._el.className || '').split(/\s+/).filter(Boolean);
  }
  set _classes(list) {
    this._el.className = list.join(' ');
  }
  add(c) {
    if (!this.contains(c)) this._classes = [...this._classes, c];
  }
  remove(c) {
    this._classes = this._classes.filter((x) => x !== c);
  }
  toggle(c, force) {
    const has = this.contains(c);
    const shouldHave = force !== undefined ? force : !has;
    if (shouldHave && !has) this.add(c);
    else if (!shouldHave && has) this.remove(c);
    return shouldHave;
  }
  contains(c) {
    return this._classes.includes(c);
  }
}

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.attributes = new Map();
    this.style = {};
    this.classList = new FakeClassList(this);
    this._listeners = new Map();
    this._value = '';
    this._innerHTML = '';
    this._textContent = '';
    this.scrollTop = 0;
    this.scrollHeight = 100;
  }
  get className() {
    return this.attributes.get('class') || '';
  }
  set className(v) {
    this.attributes.set('class', String(v));
  }
  get id() {
    return this.attributes.get('id') || '';
  }
  set id(v) {
    this.attributes.set('id', String(v));
  }
  get value() {
    return this._value;
  }
  set value(v) {
    this._value = String(v);
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  setAttribute(name, val) {
    this.attributes.set(name, String(val));
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  replaceWith(newNode) {
    if (!this.parentElement) return;
    const idx = this.parentElement.children.indexOf(this);
    if (idx >= 0) {
      newNode.parentElement = this.parentElement;
      this.parentElement.children[idx] = newNode;
    }
  }
  cloneNode(deep) {
    const clone = new FakeElement(this.tagName.toLowerCase());
    for (const [k, v] of this.attributes) clone.setAttribute(k, v);
    clone.style = { ...this.style };
    clone._textContent = this._textContent;
    clone._innerHTML = this._innerHTML;
    clone._value = this._value;
    if (deep) {
      for (const ch of this.children) clone.appendChild(ch.cloneNode(true));
    }
    return clone;
  }
  addEventListener(type, listener, options) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push({ listener, options });
  }
  removeEventListener(type, listener) {
    if (!this._listeners.has(type)) return;
    this._listeners.set(
      type,
      this._listeners.get(type).filter((x) => x.listener !== listener)
    );
  }
  async click() {
    const list = (this._listeners.get('click') || []).slice();
    for (const item of list) {
      await item.listener({ type: 'click', target: this });
      if (item.options && item.options.once) {
        this._listeners.set(
          'click',
          (this._listeners.get('click') || []).filter((x) => x !== item)
        );
      }
    }
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = parseHtmlToElements(html);
    for (const ch of this.children) ch.parentElement = this;
  }
  get textContent() {
    if (this._textContent) return this._textContent;
    return this.children.map((c) => c.textContent).join('');
  }
  set textContent(t) {
    this._textContent = String(t);
    this.children = [];
    this._innerHTML = '';
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    const res = [];
    const walk = (el) => {
      for (const ch of el.children) {
        if (matchesSelector(ch, sel)) res.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return res;
  }
  attachShadow({ mode }) {
    this.shadowRoot = new FakeElement('shadow-root');
    return this.shadowRoot;
  }
}

function matchesSelector(el, sel) {
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) {
    const classes = sel.slice(1).split('.');
    return classes.every((c) => el.classList.contains(c));
  }
  return el.tagName.toLowerCase() === sel.toLowerCase();
}

function parseHtmlToElements(html) {
  const root = [];
  const stack = [];
  const tagRe = /<\/?([a-zA-Z0-9_-]+)([^>]*)>|([^<]+)/g;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    if (m[3]) {
      const text = m[3];
      const textNode = new FakeElement('#text');
      textNode._textContent = text;
      if (stack.length > 0) stack[stack.length - 1].appendChild(textNode);
      continue;
    }
    const isClosing = m[0].startsWith('</');
    const isSelfClosing =
      m[0].endsWith('/>') || ['input', 'br', 'img', 'hr'].includes(m[1].toLowerCase());
    const tagName = m[1].toLowerCase();
    if (isClosing) {
      if (stack.length > 0 && stack[stack.length - 1].tagName.toLowerCase() === tagName) {
        stack.pop();
      }
    } else {
      const el = new FakeElement(tagName);
      const attrRe = /([a-zA-Z0-9_-]+)(?:=["']([^"']*)["'])?/g;
      let am;
      while ((am = attrRe.exec(m[2])) !== null) {
        if (am[1]) el.setAttribute(am[1], am[2] || '');
      }
      if (stack.length === 0) {
        root.push(el);
      } else {
        stack[stack.length - 1].appendChild(el);
      }
      if (!isSelfClosing) {
        stack.push(el);
      }
    }
  }
  return root;
}

let pendingAttrs = null;
class CustomHTMLElement extends FakeElement {
  constructor(tagName) {
    super(tagName);
    if (pendingAttrs) {
      for (const [k, v] of Object.entries(pendingAttrs)) {
        this.setAttribute(k, v);
      }
      pendingAttrs = null;
    }
  }
}

const customRegistry = new Map();
if (!global.HTMLElement) global.HTMLElement = CustomHTMLElement;
if (!global.customElements) {
  global.customElements = {
    get(name) {
      return customRegistry.get(name);
    },
    define(name, cls) {
      customRegistry.set(name, cls);
    },
  };
}

const windowListeners = new Map();
const postedMessages = [];
if (!global.window) {
  global.window = {
    addEventListener(type, fn) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      if (!windowListeners.has(type)) return;
      windowListeners.set(
        type,
        windowListeners.get(type).filter((x) => x !== fn)
      );
    },
    postMessage(data, targetOrigin) {
      postedMessages.push({ data, targetOrigin });
    },
  };
}

if (!global.document) {
  global.document = {
    createElement(tag) {
      return new FakeElement(tag);
    },
  };
}

let audioInstances = [];
global.Audio = class {
  constructor(src) {
    this.src = src;
    audioInstances.push(this);
  }
  async play() {
    this.played = true;
  }
};

const COPILOT_PATH = path.join(__dirname, '..', 'copilot.js');
const COPILOT_SRC = fs.readFileSync(COPILOT_PATH, 'utf8');
eval(COPILOT_SRC);

const core = require('../copilot.js');
const CopilotElement = global.customElements.get('hrms-copilot');

function createCopilot(attrs = {}, tokenProvider = null) {
  pendingAttrs = {
    'data-api-url': 'https://ai.acme.test',
    'data-host-origin': 'https://hr.acme.test',
    'data-jurisdiction': 'IN',
    ...attrs,
  };
  const el = new CopilotElement();
  if (tokenProvider) el.tokenProvider = tokenProvider;
  el.connectedCallback();
  return el;
}

function createSseStreamResponse(chunks, status = 200) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return {
    ok: status >= 200 && status < 300,
    status,
    body: stream,
    json: async () => ({}),
  };
}

// ---------------------------------------------------------------------------------------------------------------
// 1. Pure SSE Parsing Logic (parseSseFrames)
// ---------------------------------------------------------------------------------------------------------------

test('parseSseFrames exists as exported function on core', () => {
  assert.equal(typeof core.parseSseFrames, 'function');
});

test('parseSseEvents alias is removed from core', () => {
  assert.equal(core.parseSseEvents, undefined);
});

test('parseSseFrames: result event is recognized in KNOWN_SSE_EVENTS and parsed', () => {
  const payload =
    'data: {"event": "result", "data": {"statutory_citations": ["UAE Labour Law Article 51"], ' +
    '"suggested_actions": [{"label": "Adjust Probation", "entity_type": "EMPLOYEE", "entity_id": "HR-EMP-0042", ' +
    '"patch": {"probation_days": 180}}], "partial": false, "truncated": false}}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [
    {
      event: 'result',
      data: {
        statutory_citations: ['UAE Labour Law Article 51'],
        suggested_actions: [
          {
            label: 'Adjust Probation',
            entity_type: 'EMPLOYEE',
            entity_id: 'HR-EMP-0042',
            patch: { probation_days: 180 },
          },
        ],
        partial: false,
        truncated: false,
      },
    },
  ]);
  assert.equal(res.remaining, '');
});

test('parseSseFrames: single complete frame parsed', () => {
  const res = core.parseSseFrames('data: {"event": "token", "data": "Hello"}\n\n');
  assert.deepEqual(res.events, [{ event: 'token', data: 'Hello' }]);
  assert.equal(res.remaining, '');
});

test('parseSseFrames: frames split in the middle across chunks', () => {
  const chunk1 = 'data: {"event": "token", "data": "Hel';
  const res1 = core.parseSseFrames(chunk1);
  assert.deepEqual(res1.events, []);
  assert.equal(res1.remaining, chunk1);

  const chunk2 = 'lo world"}\n\n';
  const res2 = core.parseSseFrames(res1.remaining + chunk2);
  assert.deepEqual(res2.events, [{ event: 'token', data: 'Hello world' }]);
  assert.equal(res2.remaining, '');
});

test('parseSseFrames: multiple frames in a single chunk', () => {
  const payload =
    'data: {"event": "token", "data": "Part 1"}\n\n' +
    'data: {"event": "token", "data": " Part 2"}\n\n' +
    'data: {"event": "done", "data": {"finish_reason": "stop"}}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [
    { event: 'token', data: 'Part 1' },
    { event: 'token', data: ' Part 2' },
    { event: 'done', data: { finish_reason: 'stop' } },
  ]);
  assert.equal(res.remaining, '');
});

test('parseSseFrames: malformed JSON line ignored', () => {
  const payload =
    'data: {malformed json: true\n\n' +
    'data: {"event": "token", "data": "Valid Token"}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [{ event: 'token', data: 'Valid Token' }]);
  assert.equal(res.remaining, '');
});

test('parseSseFrames: unknown events ignored', () => {
  const payload =
    'data: {"event": "custom_metric", "data": 123}\n\n' +
    'data: {"event": "thinking", "data": "thinking details"}\n\n' +
    'data: {"event": "token", "data": "Content"}\n\n' +
    'data: {"event": "unknown_future_event", "data": {}}\n\n' +
    'data: {"event": "done", "data": {}}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [
    { event: 'token', data: 'Content' },
    { event: 'done', data: {} },
  ]);
});

test('parseSseFrames: boundary empty, null, undefined, whitespace inputs', () => {
  assert.deepEqual(core.parseSseFrames(''), { events: [], remaining: '' });
  assert.deepEqual(core.parseSseFrames(null), { events: [], remaining: '' });
  assert.deepEqual(core.parseSseFrames(undefined), { events: [], remaining: '' });
  assert.deepEqual(core.parseSseFrames('   \n\n   \n\n'), { events: [], remaining: '' });
});

test('parseSseFrames: non-data lines (comments, ids) and non-object json ignored', () => {
  const payload =
    ': keep-alive comment\n' +
    'id: 42\n' +
    'retry: 1000\n' +
    'data: "just a raw string"\n\n' +
    'data: 12345\n\n' +
    'data: {"event": "token", "data": "Recovered"}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [{ event: 'token', data: 'Recovered' }]);
});

test('parseSseFrames: handles CRLF (\\r\\n) frame and line delimiters', () => {
  const payload =
    'data: {"event": "token", "data": "Line 1"}\r\n\r\n' +
    'data: {"event": "done", "data": {}}\r\n\r\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [
    { event: 'token', data: 'Line 1' },
    { event: 'done', data: {} },
  ]);
  assert.equal(res.remaining, '');
});

test('parseSseFrames: preserves falsy data values (false, 0, empty string)', () => {
  const payload =
    'data: {"event": "partial", "data": false}\n\n' +
    'data: {"event": "token", "data": ""}\n\n';
  const res = core.parseSseFrames(payload);
  assert.deepEqual(res.events, [
    { event: 'partial', data: false },
    { event: 'token', data: '' },
  ]);
});

// ---------------------------------------------------------------------------------------------------------------
// 2. Chat Streaming (/v1/chat/stream) DOM Rendering Tests
// ---------------------------------------------------------------------------------------------------------------

test('streaming chat: streams tokens progressively into assistant bubble as plain text', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  let streamCalled = false;
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      streamCalled = true;
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Your statutory "}\n\n',
        'data: {"event": "token", "data": "gratuity is AED 25,000."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Calculate gratuity';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.equal(streamCalled, true);
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Your statutory gratuity is AED 25,000\./);
});

test('streaming chat: tool_call renders small step line "Using <name>…" via textContent', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "tool_call", "data": {"name": "fetch_policy"}}\n\n',
        'data: {"event": "token", "data": "Policy retrieved."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Check policy';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Using fetch_policy…/);
  assert.match(chatLogs.textContent, /Policy retrieved\./);
});

test('streaming chat: partial event renders notice that answer may be incomplete', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "partial", "data": true}\n\n',
        'data: {"event": "token", "data": "Truncated reply"}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Long report';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /answer may be incomplete/i);
  assert.match(chatLogs.textContent, /Truncated reply/);
});

test('streaming chat: in-band error event renders error code', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "error", "data": {"error": "LLM_BUDGET_EXHAUSTED"}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Trigger budget error';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /LLM_BUDGET_EXHAUSTED/);
});

test('streaming chat: done and intent events terminate stream cleanly', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Finished reply."}\n\n',
        'data: {"event": "done", "data": {"finish_reason": "stop"}}\n\n',
        'data: {"event": "intent", "data": "hrms_policy"}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Complete task';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Finished reply\./);
});

test('streaming chat: handles frames split across stream chunks', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "da',
        'ta": "Split tok',
        'en"}\n\ndata: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Test split chunk';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Split token/);
});

test('streaming chat: falls back to /v1/chat when /v1/chat/stream returns 404', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  const called = [];
  global.fetch = async (url) => {
    called.push(url);
    if (url.endsWith('/v1/chat/stream')) {
      return { ok: false, status: 404, json: async () => ({ error: 'NOT_FOUND' }) };
    }
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ reply_text: 'Fallback response from /v1/chat' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Test 404 fallback';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.ok(called.some((u) => u.endsWith('/v1/chat/stream')));
  assert.ok(called.some((u) => u.endsWith('/v1/chat')));
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Fallback response from \/v1\/chat/);
});

test('streaming chat: falls back to /v1/chat when /v1/chat/stream returns 405', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  const called = [];
  global.fetch = async (url) => {
    called.push(url);
    if (url.endsWith('/v1/chat/stream')) {
      return { ok: false, status: 405, json: async () => ({ error: 'METHOD_NOT_ALLOWED' }) };
    }
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ reply_text: 'Fallback from 405' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Test 405 fallback';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.ok(called.some((u) => u.endsWith('/v1/chat/stream')));
  assert.ok(called.some((u) => u.endsWith('/v1/chat')));
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Fallback from 405/);
});

test('streaming chat: falls back to /v1/chat when response.body or getReader is unavailable', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  const called = [];
  global.fetch = async (url) => {
    called.push(url);
    if (url.endsWith('/v1/chat/stream')) {
      return { ok: true, status: 200, body: null, json: async () => ({}) };
    }
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ reply_text: 'Fallback when body unavailable' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Test body null fallback';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.ok(called.some((u) => u.endsWith('/v1/chat/stream')));
  assert.ok(called.some((u) => u.endsWith('/v1/chat')));
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Fallback when body unavailable/);
});

test('streaming chat: non-2xx status (401, 429, 500) shows error without fallback to /v1/chat', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  const called = [];
  global.fetch = async (url) => {
    called.push(url);
    if (url.endsWith('/v1/chat/stream')) {
      return {
        ok: false,
        status: 429,
        json: async () => ({ error: 'LLM_BUDGET_EXHAUSTED' }),
      };
    }
    if (url.endsWith('/v1/chat')) {
      return { ok: true, status: 200, json: async () => ({ reply_text: 'Should not be called' }) };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Trigger 429';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.ok(called.some((u) => u.endsWith('/v1/chat/stream')));
  assert.ok(!called.some((u) => u.endsWith('/v1/chat')), 'Must not fall back to /v1/chat on 429');
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Request failed \(429\): LLM_BUDGET_EXHAUSTED/);
});

test('streaming chat: token rendering is plain text and never injects unescaped HTML/tags', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "data": "<img src=x "}\n\n',
        'data: {"event": "token", "data": "onerror=alert(1)>"}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'XSS test in stream';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  const bubbles = chatLogs.querySelectorAll('.bubble');
  const lastBubble = bubbles[bubbles.length - 1];
  assert.equal(lastBubble.textContent, '<img src=x onerror=alert(1)>');
  assert.doesNotMatch(lastBubble.innerHTML, /<img\b/i);
});

test(
  'streaming chat: stream carries token, done, result with citation and suggested action, intent -> proposal flow ok',
  async () => {
    const copilot = createCopilot({}, async () => 'token-proposer');
    const fetchCalls = [];

    global.fetch = async (url, opts) => {
      fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (url.endsWith('/v1/chat/stream')) {
        const resultData = JSON.stringify({
          statutory_citations: ['UAE Labour Law Article 51'],
          suggested_actions: [{
            label: 'Adjust Probation Period',
            entity_type: 'EMPLOYEE',
            entity_id: 'HR-EMP-0042',
            patch: { probation_days: 180 },
          }],
          partial: false,
          truncated: false,
        });
        return createSseStreamResponse([
          'data: {"event": "token", "data": "Probation period exceeds statutory limit."}\n\n',
          'data: {"event": "done", "data": {"finish_reason": "stop"}}\n\n',
          `data: {"event": "result", "data": ${resultData}}\n\n`,
          'data: {"event": "intent", "data": "hrms_policy"}\n\n',
        ]);
      }
    if (url.endsWith('/v1/writeback/proposals')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ id: 'PROP-2026-99', status: 'PENDING' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Audit employee probation';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  const assistantMsgs = chatLogs.querySelectorAll('.msg.assistant');
  assert.ok(assistantMsgs.length >= 1, 'Should have an assistant message');
  const msgDiv = assistantMsgs[assistantMsgs.length - 1];

  // 1. Text token bubble
  const bubble = msgDiv.querySelector('.bubble');
  assert.ok(bubble, 'Assistant message should have a bubble');
  assert.match(bubble.textContent, /Probation period exceeds statutory limit\./);

  // 2. Citation in same assistant message
  const citationsBox = msgDiv.querySelector('.citations');
  assert.ok(citationsBox, 'Statutory citations should be rendered in assistant message');
  assert.match(citationsBox.textContent, /UAE Labour Law Article 51/);

  // 3. Suggested action button in same assistant message
  const actionBtns = msgDiv.querySelectorAll('.action-btn');
  assert.equal(actionBtns.length, 1, 'Should render suggested action button');
  assert.equal(actionBtns[0].textContent, 'Adjust Probation Period');

  // 4. Click action button -> confirmation text with confirm button
  await actionBtns[0].click();
  await new Promise((r) => setTimeout(r, 20));

  assert.match(chatLogs.textContent, /Propose this change to Employee HR-EMP-0042\?/);
  assert.match(chatLogs.textContent, /probation_days → 180/);
  assert.match(chatLogs.textContent, /A different approver must approve it before anything is written\./);

  // 5. Click confirm button -> posts to /v1/writeback/proposals
  const allActionBtns = copilot.shadowRoot.querySelectorAll('.action-btn');
  const confirmBtn = allActionBtns[allActionBtns.length - 1];
  assert.equal(confirmBtn.textContent, '📝 Propose this change');

  await confirmBtn.click();
  await new Promise((r) => setTimeout(r, 20));

  const proposalCalls = fetchCalls.filter((c) => c.url.endsWith('/v1/writeback/proposals'));
  assert.equal(proposalCalls.length, 1, 'Proposal should be submitted');
  assert.deepEqual(proposalCalls[0].body, {
    doctype: 'Employee',
    name: 'HR-EMP-0042',
    patch: { probation_days: 180 },
  });
  assert.match(chatLogs.textContent, /Proposal PROP-2026-99 created \(PENDING\)/);

  // 6. Confirm button once: true behavior - clicking again does not submit again
  await confirmBtn.click();
  await new Promise((r) => setTimeout(r, 20));
  const proposalCallsAfter = fetchCalls.filter((c) => c.url.endsWith('/v1/writeback/proposals'));
  assert.equal(proposalCallsAfter.length, 1, 'Proposal must not be re-submitted on second click (once: true)');
});

test('streaming chat: stream ends naturally without intent event and renders citations and actions', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      const resultData = JSON.stringify({
        statutory_citations: ['Article 12'],
        suggested_actions: [{
          label: 'Fix Record',
          entity_type: 'EMPLOYEE',
          entity_id: 'EMP-1',
          patch: { status: 'Active' },
        }],
      });
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Stream ending naturally."}\n\n',
        'data: {"event": "done", "data": {"finish_reason": "stop"}}\n\n',
        `data: {"event": "result", "data": ${resultData}}\n\n`,
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Test end without intent';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  const msgs = chatLogs.querySelectorAll('.msg.assistant');
  const msgDiv = msgs[msgs.length - 1];
  assert.ok(msgDiv.querySelector('.citations'));
  assert.match(msgDiv.querySelector('.citations').textContent, /Article 12/);
  assert.ok(msgDiv.querySelector('.actions'));
  assert.match(msgDiv.querySelector('.actions').textContent, /Fix Record/);
});

test('streaming chat: result event with empty or missing citations and actions does not render empty containers',
  async () => {
    const copilot = createCopilot({}, async () => 'token-ok');
    global.fetch = async (url) => {
      if (url.endsWith('/v1/chat/stream')) {
        return createSseStreamResponse([
          'data: {"event": "token", "data": "Clean answer without citations or actions."}\n\n',
          'data: {"event": "done", "data": {}}\n\n',
          'data: {"event": "result", "data": {"statutory_citations": [], "suggested_actions": []}}\n\n',
          'data: {"event": "intent", "data": "hrms_policy"}\n\n',
        ]);
      }
    };

    const input = copilot.shadowRoot.querySelector('#chat-input');
    input.value = 'Clean answer';
    await copilot.shadowRoot.querySelector('#send-btn').click();
    await new Promise((r) => setTimeout(r, 20));

    const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
    const msgs = chatLogs.querySelectorAll('.msg.assistant');
    const msgDiv = msgs[msgs.length - 1];
    assert.equal(msgDiv.querySelector('.citations'), null);
    assert.equal(msgDiv.querySelector('.actions'), null);
  }
);

test('streaming chat: citation HTML in result is safely escaped via textContent', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      const xssCitation = '<script>alert(1)</script><img src=x onerror=alert(1)>';
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Citation XSS test."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
        `data: {"event": "result", "data": {"statutory_citations": ["${xssCitation}"], "suggested_actions": []}}\n\n`,
        'data: {"event": "intent", "data": "hrms_policy"}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'XSS citation test';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  const citations = chatLogs.querySelector('.citations');
  assert.ok(citations);
  assert.doesNotMatch(citations.innerHTML, /<img\b|<script\b/i);
  assert.match(citations.textContent, /<script>alert\(1\)<\/script>/);
});

test('streaming chat: result event with non-object data is handled gracefully', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Graceful handling test."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
        'data: {"event": "result", "data": null}\n\n',
        'data: {"event": "intent", "data": "hrms_policy"}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Graceful handling';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Graceful handling test\./);
});

test('streaming chat: fallback path remains unchanged with citations and actions and proposal flow', async () => {
  const copilot = createCopilot({}, async () => 'token-proposer');
  const fetchCalls = [];

  global.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (url.endsWith('/v1/chat/stream')) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Fallback reply text.',
          statutory_citations: ['Fallback Law Section 4'],
          suggested_actions: [
            {
              label: 'Fallback Action',
              entity_type: 'EMPLOYEE',
              entity_id: 'HR-EMP-0042',
              patch: { probation_days: 90 },
            },
          ],
        }),
      };
    }
    if (url.endsWith('/v1/writeback/proposals')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ id: 'PROP-2026-100', status: 'PENDING' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Fallback flow test';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Fallback reply text\./);
  assert.match(chatLogs.textContent, /Fallback Law Section 4/);

  const actionBtn = copilot.shadowRoot.querySelector('.action-btn');
  assert.ok(actionBtn);
  assert.equal(actionBtn.textContent, 'Fallback Action');

  await actionBtn.click();
  await new Promise((r) => setTimeout(r, 20));

  assert.match(chatLogs.textContent, /Propose this change to Employee HR-EMP-0042\?/);
  const confirmBtn = copilot.shadowRoot.querySelectorAll('.action-btn');
  await confirmBtn[confirmBtn.length - 1].click();
  await new Promise((r) => setTimeout(r, 20));

  const proposalCalls = fetchCalls.filter((c) => c.url.endsWith('/v1/writeback/proposals'));
  assert.equal(proposalCalls.length, 1);
  assert.deepEqual(proposalCalls[0].body, {
    doctype: 'Employee',
    name: 'HR-EMP-0042',
    patch: { probation_days: 90 },
  });
  assert.match(chatLogs.textContent, /Proposal PROP-2026-100 created \(PENDING\)/);
});

// ---------------------------------------------------------------------------------------------------------------
// 3. Voice Turn Audio Withheld Tests
// ---------------------------------------------------------------------------------------------------------------

test('voice turn: audio_withheld set shows notice and does not play audio', async () => {
  audioInstances.length = 0;
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/voice/turn')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Employee PAN is ABCDE1234F.',
          audio_withheld: 'PII_EGRESS_BLOCKED',
          reply_audio_base64: null,
        }),
      };
    }
  };

  await copilot.sendVoiceAudio('input-audio-base64');
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Employee PAN is ABCDE1234F\./);
  assert.match(chatLogs.textContent, /Audio withheld: the reply contains personal data/);
  assert.equal(audioInstances.length, 0, 'No Audio instance should be created or played');
});

test('voice turn: audio_withheld with reply_audio_base64 present still does not play audio', async () => {
  audioInstances.length = 0;
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/voice/turn')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Private data reply.',
          audio_withheld: 'PII_EGRESS_BLOCKED',
          reply_audio_base64: 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=',
        }),
      };
    }
  };

  await copilot.sendVoiceAudio('input-audio-base64');
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Audio withheld: the reply contains personal data/);
  assert.equal(audioInstances.length, 0, 'Audio must not play even if base64 was returned');
});

test('voice turn: audio_withheld not set plays audio normally', async () => {
  audioInstances.length = 0;
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/voice/turn')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Standard response.',
          audio_withheld: null,
          reply_audio_base64: 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=',
        }),
      };
    }
  };

  await copilot.sendVoiceAudio('input-audio-base64');
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Standard response\./);
  assert.doesNotMatch(chatLogs.textContent, /Audio withheld/);
  assert.equal(audioInstances.length, 1);
  assert.equal(audioInstances[0].played, true);
});
