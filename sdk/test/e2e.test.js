// End-to-End Tests for Enterprise HRMS Copilot SDK (<hrms-copilot>)
// Run: node --test sdk/test/e2e.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// ---------------------------------------------------------------------------------------------------------------
// Lightweight headless DOM & Browser Environment for E2E Component Testing
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
  if (sel.startsWith('.')) return el.classList.contains(sel.slice(1));
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
global.HTMLElement = CustomHTMLElement;
global.customElements = {
  get(name) {
    return customRegistry.get(name);
  },
  define(name, cls) {
    customRegistry.set(name, cls);
  },
};

const windowListeners = new Map();
const postedMessages = [];
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

global.document = {
  createElement(tag) {
    return new FakeElement(tag);
  },
};

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

global.MediaRecorder = class {
  constructor(stream) {
    this.stream = stream;
    this.state = 'inactive';
  }
  start() {
    this.state = 'recording';
    if (this.ondataavailable) {
      this.ondataavailable({ data: { size: 100 } });
    }
  }
  stop() {
    this.state = 'inactive';
    if (this.onstop) this.onstop();
  }
};

global.Blob = class {
  constructor(parts, opts) {
    this.parts = parts;
    this.opts = opts;
  }
};

global.FileReader = class {
  readAsDataURL() {
    setTimeout(() => {
      this.result = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
      if (this.onloadend) this.onloadend();
    }, 0);
  }
};

Object.defineProperty(global, 'navigator', {
  value: {
    mediaDevices: {
      getUserMedia: async () => ({
        getTracks: () => [{ stop: () => {} }],
      }),
    },
  },
  configurable: true,
  writable: true,
});

// Load copilot.js inside the fake browser environment
const COPILOT_PATH = path.join(__dirname, '..', 'copilot.js');
const COPILOT_SRC = fs.readFileSync(COPILOT_PATH, 'utf8');
eval(COPILOT_SRC);

const core = require('../copilot.js');
const CopilotElement = customElements.get('hrms-copilot');

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

function dispatchWindowMessage(origin, data) {
  const listeners = windowListeners.get('message') || [];
  for (const fn of listeners) {
    fn({ origin, data });
  }
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
// Behaviour 1: XSS through formatMarkdown
// ---------------------------------------------------------------------------------------------------------------

// (1) Positive Path: legitimate Markdown constructs
test('B1-POS-01: formatMarkdown converts headings h2, h3, h4', () => {
  const html = core.formatMarkdown('# Heading 2\n## Heading 3\n### Heading 4');
  assert.match(html, /<h2 class="md-h2">Heading 2<\/h2>/);
  assert.match(html, /<h3 class="md-h3">Heading 3<\/h3>/);
  assert.match(html, /<h4 class="md-h4">Heading 4<\/h4>/);
});

test('B1-POS-02: formatMarkdown converts blockquotes', () => {
  const html = core.formatMarkdown('> This is an important statutory compliance notice.');
  assert.match(html, /<blockquote class="md-quote">This is an important statutory compliance notice\.<\/blockquote>/);
});

test('B1-POS-03: formatMarkdown converts bold, italics, and triple asterisks', () => {
  const html = core.formatMarkdown('**bold** __also bold__ *italic* ***bold italic***');
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<strong>also bold<\/strong>/);
  assert.match(html, /<em>italic<\/em>/);
  assert.match(html, /<strong><em>bold italic<\/em><\/strong>/);
});

test('B1-POS-04: formatMarkdown converts inline code and fenced code blocks with lang', () => {
  const html = core.formatMarkdown('Run `npm test`\n\n```python\ndef audit():\n    return True\n```');
  assert.match(html, /<code class="inline-code">npm test<\/code>/);
  assert.match(html, /<pre class="code-block"><code class="lang-python">def audit\(\):\n    return True<\/code><\/pre>/);
});

test('B1-POS-05: formatMarkdown converts unordered and ordered lists with li elements', () => {
  const html = core.formatMarkdown('- Item A\n- Item B\n\n1. First\n2. Second');
  assert.match(html, /<ul class="md-ul">[\s\S]*<li>Item A<\/li>[\s\S]*<li>Item B<\/li>[\s\S]*<\/ul>/);
  assert.match(html, /<ol class="md-ol">[\s\S]*<li>First<\/li>[\s\S]*<li>Second<\/li>[\s\S]*<\/ol>/);
});

test('B1-POS-06: formatMarkdown turns valid https links into safe anchors with rel=noopener noreferrer', () => {
  const html = core.formatMarkdown('[HR Portal](https://hr.acme.test/portal)');
  assert.match(
    html,
    /<a href="https:\/\/hr\.acme\.test\/portal" target="_blank" rel="noopener noreferrer" class="md-link">HR Portal<\/a>/
  );
});

test('B1-POS-07: formatMarkdown supports HTTPS links with multi-parameter query strings and fragments', () => {
  const html = core.formatMarkdown('[API Docs](https://api.acme.test/v1/search?query=leave&page=2&limit=50#sec-3)');
  assert.match(html, /href="https:\/\/api\.acme\.test\/v1\/search\?query=leave&amp;page=2&amp;limit=50#sec-3"/);
});

// (2) Boundary Values: exact limits, empty, max sizes, unicode, nested brackets
test('B1-BND-01: formatMarkdown boundary empty and falsy values return empty string', () => {
  assert.equal(core.formatMarkdown(''), '');
  assert.equal(core.formatMarkdown(null), '');
  assert.equal(core.formatMarkdown(undefined), '');
  assert.equal(core.formatMarkdown('   \n\n   '), '');
});

test('B1-BND-02: formatMarkdown handles large payloads (50KB) efficiently without crash or corruption', () => {
  const repeated = '# Title\n\n**Notice** for `EMP-01`.\n\n'.repeat(1000);
  const start = Date.now();
  const html = core.formatMarkdown(repeated);
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 2000, `Took ${elapsed}ms`);
  assert.ok(html.length > 50000);
  assert.match(html, /<h2 class="md-h2">Title<\/h2>/);
});

test('B1-BND-03: formatMarkdown handles multilingual unicode and emoji safely', () => {
  const text = 'Arabic: مرحبا | Chinese: 员工薪资 | Japanese: 給与明細 | Emoji: 💼 🚀 👨‍👩‍👧‍👦 | RTL line: \u202Ereversed';
  const html = core.formatMarkdown(text);
  assert.ok(html.includes('مرحبا'));
  assert.ok(html.includes('员工薪资'));
  assert.ok(html.includes('給与明細'));
  assert.ok(html.includes('👨‍👩‍👧‍👦'));
  assert.doesNotMatch(html, /<script/i);
});

test('B1-BND-04: formatMarkdown handles unicode line separators (U+2028, U+2029)', () => {
  const text = 'Line 1\u2028Line 2\u2029Line 3';
  const html = core.formatMarkdown(text);
  assert.doesNotMatch(html, /<script/i);
  assert.ok(html.includes('Line 1'));
});

test('B1-BND-05: formatMarkdown handles nested brackets in markdown links safely', () => {
  const text1 = '[[Nested Link](https://inner.test)](https://outer.test)';
  const html1 = core.formatMarkdown(text1);
  assert.doesNotMatch(html1, /<[a-z][^>]*\son[a-z]+/i);
  for (const m of html1.matchAll(/href="([^"]*)"/g)) {
    assert.match(m[1], /^https:\/\//);
  }

  const text2 = '[Link with [bracket] inside](https://example.test/path)';
  const html2 = core.formatMarkdown(text2);
  assert.doesNotMatch(html2, /<script/i);
});

test('B1-BND-06: formatMarkdown placeholder collision resistance (literal ⟦CB0⟧ and ⟦IC0⟧ in input)', () => {
  const text = 'Attacker payload ⟦CB0⟧ and ⟦IC0⟧ without code blocks';
  const html = core.formatMarkdown(text);
  // Placeholders that do not correspond to parsed blocks resolve to empty string, not undefined
  assert.doesNotMatch(html, /undefined/);
  assert.doesNotMatch(html, /<script/i);
});

// (3) Negative Paths: script injections, event handlers, javascript/data links, entity tricks
const ADVERSARIAL_XSS_VECTORS = [
  '<script>alert("xss")</script>',
  '<SCRIPT SRC="https://evil.test/xss.js"></SCRIPT>',
  '<script\n>alert(1)</script>',
  '<script/x>alert(1)</script>',
  '<script defer>alert(1)</script>',
  '<img src="invalid" onerror="alert(1)">',
  '<svg onload="alert(document.cookie)">',
  '<body onload="alert(1)">',
  '<input autofocus onfocus="alert(1)">',
  '<b onmouseover="alert(1)">hover me</b>',
  '<details ontoggle="alert(1)">',
  '<iframe src="javascript:alert(1)"></iframe>',
  '<embed src="javascript:alert(1)">',
  '<object data="javascript:alert(1)"></object>',
  '[click here](javascript:alert(document.domain))',
  '[click here](JAVASCRIPT:alert(1))',
  '[click here](javascript&colon;alert(1))',
  '[click here](java\0script:alert(1))',
  '[click here](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
  '[click here](vbscript:msgbox(1))',
  '[click here](file:///etc/passwd)',
  '[click here](blob:https://evil.test/uuid)',
  '[click here](http://non-tls-evil.test)',
  '[click here](//protocol-relative.test)',
  '[xss](https://evil.test/&quot;onmouseover=&quot;alert(1))',
  '[xss](https://evil.test/&#x22;onfocus=alert(1))',
  '[xss](https://evil.test/&apos;onclick=&apos;alert(1))',
  '[xss](https://evil.test/&lt;script&gt;alert(1)&lt;/script&gt;)',
  '[xss](https://evil.test/" onmouseover="alert(1))',
  '[xss](https://evil.test/\' onfocus=\'alert(1))',
  '[xss](https://evil.test/><script>alert(1)</script>)',
];

for (const vector of ADVERSARIAL_XSS_VECTORS) {
  test(`B1-NEG-XSS: neutralises ${vector.slice(0, 45)}`, () => {
    const html = core.formatMarkdown(vector);
    // 1. Raw dangerous tags must never be present
    assert.doesNotMatch(html, /<(script|img|svg|iframe|body|input|embed|object|details)\b/i);
    // 2. Real HTML event handlers must never be present
    assert.doesNotMatch(html, /<[a-z][^>]*\son[a-z]+\s*=/i);
    // 3. Every href must strictly begin with https:// and contain no unescaped quotes or angle brackets
    for (const m of html.matchAll(/href="([^"]*)"/g)) {
      assert.match(m[1], /^https:\/\/([^<>"'&\s⟦⟧]|&amp;)+$/);
    }
  });
}

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 2: postMessage Origin Trust
// ---------------------------------------------------------------------------------------------------------------

test('B2-POS-01: postMessage from exact configured hostOrigin updates component context and badge', () => {
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test' });
  const event = {
    origin: 'https://hr.acme.test',
    data: { type: 'HRMS_NAVIGATE', entity: 'Employee', record_id: 'HR-EMP-1001' },
  };
  dispatchWindowMessage(event.origin, event.data);

  assert.deepEqual(copilot.currentContext, { entity: 'Employee', record_id: 'HR-EMP-1001' });
  const badge = copilot.shadowRoot.querySelector('#context-badge');
  assert.equal(badge.textContent, 'Context: Employee #HR-EMP-1001');
  assert.equal(badge.style.display, 'inline-block');
});

test('B2-POS-02: postMessage with port in hostOrigin trusted on exact match', () => {
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test:8443' });
  dispatchWindowMessage('https://hr.acme.test:8443', {
    type: 'HRMS_NAVIGATE',
    entity: 'Job Requisition',
    record_id: 'REQ-4001',
  });
  assert.deepEqual(copilot.currentContext, { entity: 'Job Requisition', record_id: 'REQ-4001' });
});

test('B2-POS-03: APPLY_ARCHETYPE action dispatches window.postMessage strictly to hostOrigin', () => {
  postedMessages.length = 0;
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test' });
  copilot.handleAction({
    action_type: 'APPLY_ARCHETYPE',
    name: 'Lead Architect Template',
    archetype: { role: 'Lead Architect', min_experience_years: 8 },
  });

  assert.equal(postedMessages.length, 1);
  assert.equal(postedMessages[0].targetOrigin, 'https://hr.acme.test');
  assert.equal(postedMessages[0].data.type, 'HRMS_APPLY_ARCHETYPE');
  assert.equal(postedMessages[0].data.archetype.role, 'Lead Architect');
});

test('B2-BND-01: postMessage boundary entity and record_id lengths accepted', () => {
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test' });
  // entity length 1, record_id length 1
  dispatchWindowMessage('https://hr.acme.test', {
    type: 'HRMS_NAVIGATE',
    entity: 'E',
    record_id: '1',
  });
  assert.deepEqual(copilot.currentContext, { entity: 'E', record_id: '1' });

  // entity length 64, record_id length 140
  const maxEntity = 'A' + 'b'.repeat(63);
  const maxRecordId = 'R'.repeat(140);
  dispatchWindowMessage('https://hr.acme.test', {
    type: 'HRMS_NAVIGATE',
    entity: maxEntity,
    record_id: maxRecordId,
  });
  assert.deepEqual(copilot.currentContext, { entity: maxEntity, record_id: maxRecordId });
});

test('B2-NEG-01: postMessage rejected from unauthorized origins (domain, subdomain, port, scheme)', () => {
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test' });
  const badOrigins = [
    'https://evil.test',
    'https://hr.acme.test.evil.com',
    'https://attacker-hr.acme.test',
    'https://sub.hr.acme.test',
    'http://hr.acme.test',
    'https://hr.acme.test:8080',
  ];

  for (const origin of badOrigins) {
    copilot.currentContext = {};
    dispatchWindowMessage(origin, {
      type: 'HRMS_NAVIGATE',
      entity: 'Employee',
      record_id: 'EMP-01',
    });
    assert.deepEqual(copilot.currentContext, {}, `Failed to reject origin: ${origin}`);
  }
});

test('B2-NEG-02: postMessage rejected when hostOrigin is empty, wildcard, or missing', () => {
  for (const badHost of ['', '*', null, undefined]) {
    const copilot = createCopilot({ 'data-host-origin': badHost });
    dispatchWindowMessage('https://hr.acme.test', {
      type: 'HRMS_NAVIGATE',
      entity: 'Employee',
      record_id: 'EMP-01',
    });
    assert.deepEqual(copilot.currentContext, {});
  }
});

test('B2-NEG-03: postMessage rejected when message data is non-object or wrong type', () => {
  const copilot = createCopilot({ 'data-host-origin': 'https://hr.acme.test' });
  const badPayloads = [
    null,
    undefined,
    'HRMS_NAVIGATE',
    42,
    [],
    { type: 'OTHER', entity: 'Employee', record_id: '1' },
    { type: 'HRMS_NAVIGATE_EXTRA', entity: 'Employee', record_id: '1' },
    { entity: 'Employee', record_id: '1' },
  ];

  for (const payload of badPayloads) {
    copilot.currentContext = {};
    dispatchWindowMessage('https://hr.acme.test', payload);
    assert.deepEqual(copilot.currentContext, {});
  }
});

test('B2-NEG-04: APPLY_ARCHETYPE with unconfigured hostOrigin fails closed without posting message', () => {
  postedMessages.length = 0;
  const copilot = createCopilot({ 'data-host-origin': '' });
  copilot.handleAction({ action_type: 'APPLY_ARCHETYPE', archetype: { role: 'Dev' } });

  assert.equal(postedMessages.length, 0);
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /data-host-origin is not set/);
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 3: Context Sanitisation
// ---------------------------------------------------------------------------------------------------------------

test('B3-POS-01: sanitizeContext extracts entity and record_id and drops all unauthorized fields', () => {
  const result = core.sanitizeContext({
    entity: 'Employee',
    record_id: 'EMP-777',
    user_role: 'SUPER_ADMIN',
    tenant_id: 'injected-tenant',
    approver_id: 'FABRICATED-APPROVER',
    session_token: 'secret',
  });
  assert.deepEqual(result, { entity: 'Employee', record_id: 'EMP-777' });
  assert.equal(result.user_role, undefined);
  assert.equal(result.tenant_id, undefined);
  assert.equal(result.approver_id, undefined);
});

test('B3-BND-01: sanitizeContext boundary checks on entity length (1 and 64 allowed, 65 rejected)', () => {
  assert.notEqual(core.sanitizeContext({ entity: 'E', record_id: '1' }), null);
  assert.notEqual(core.sanitizeContext({ entity: 'A' + 'b'.repeat(63), record_id: '1' }), null);
  assert.equal(core.sanitizeContext({ entity: 'A' + 'b'.repeat(64), record_id: '1' }), null);
});

test('B3-BND-02: sanitizeContext boundary checks on record_id length (1 and 140 allowed, 141 rejected)', () => {
  assert.notEqual(core.sanitizeContext({ entity: 'Employee', record_id: 'X' }), null);
  assert.notEqual(core.sanitizeContext({ entity: 'Employee', record_id: 'X'.repeat(140) }), null);
  assert.equal(core.sanitizeContext({ entity: 'Employee', record_id: 'X'.repeat(141) }), null);
});

test('B3-BND-03: sanitizeContext permits valid record_id special characters: dot, underscore, slash, dash', () => {
  const result = core.sanitizeContext({ entity: 'Employee', record_id: 'HR.2026/EMP_01-V2' });
  assert.deepEqual(result, { entity: 'Employee', record_id: 'HR.2026/EMP_01-V2' });
});

test('B3-NEG-01: sanitizeContext rejects entities with HTML, digits at start, spaces at start, or invalid chars', () => {
  const invalidEntities = [
    '<script>alert(1)</script>',
    '1Employee',
    ' Employee',
    '',
    'Employee; DROP TABLE',
    'Employee"quote',
    'Employée', // non-ASCII rejected by ENTITY_RE
    null,
    42,
    {},
  ];
  for (const ent of invalidEntities) {
    assert.equal(core.sanitizeContext({ entity: ent, record_id: 'EMP-01' }), null);
  }
});

test('B3-NEG-02: sanitizeContext rejects record_ids with spaces, quotes, brackets, control characters', () => {
  const invalidRecordIds = [
    '',
    'EMP 01',
    'EMP"01',
    'EMP<01>',
    'EMP\n01',
    'EMP\x001',
    null,
    12345,
    ['EMP-01'],
  ];
  for (const rid of invalidRecordIds) {
    assert.equal(core.sanitizeContext({ entity: 'Employee', record_id: rid }), null);
  }
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 4: apiUrl Validation
// ---------------------------------------------------------------------------------------------------------------

test('B4-POS-01: validateApiUrl accepts valid https origins and normalises paths', () => {
  assert.equal(core.validateApiUrl('https://ai.acme.test'), 'https://ai.acme.test');
  assert.equal(core.validateApiUrl('https://ai.acme.test/plugin///'), 'https://ai.acme.test/plugin');
  assert.equal(core.validateApiUrl('https://ai.acme.test:8443/api'), 'https://ai.acme.test:8443/api');
});

test('B4-POS-02: validateApiUrl permits localhost and 127.0.0.1 development endpoints', () => {
  assert.equal(core.validateApiUrl('http://localhost:8000'), 'http://localhost:8000');
  assert.equal(core.validateApiUrl('http://127.0.0.1:8000'), 'http://127.0.0.1:8000');
  assert.equal(core.validateApiUrl('http://localhost'), 'http://localhost');
  assert.equal(core.validateApiUrl('http://127.0.0.1'), 'http://127.0.0.1');
});

test('B4-BND-01: validateApiUrl strips query parameters and hash fragments to prevent SSRF/smuggling', () => {
  assert.equal(
    core.validateApiUrl('https://ai.acme.test/api?token=secret#section'),
    'https://ai.acme.test/api'
  );
});

test('B4-NEG-01: validateApiUrl rejects insecure remote HTTP and userinfo credential smuggling', () => {
  assert.equal(core.validateApiUrl('http://ai.acme.test'), null);
  assert.equal(core.validateApiUrl('http://localhost.evil.com:8000'), null);
  assert.equal(core.validateApiUrl('http://127.0.0.1.attacker.com:8000'), null);
  assert.equal(core.validateApiUrl('https://user:password@ai.acme.test'), null);
  assert.equal(core.validateApiUrl('https://user@ai.acme.test'), null);
});

test('B4-NEG-02: validateApiUrl rejects dangerous protocols, empty strings, and malformed URLs', () => {
  assert.equal(core.validateApiUrl('javascript:alert(1)'), null);
  assert.equal(core.validateApiUrl('data:text/html,<script>alert(1)</script>'), null);
  assert.equal(core.validateApiUrl('file:///etc/passwd'), null);
  assert.equal(core.validateApiUrl(''), null);
  assert.equal(core.validateApiUrl(null), null);
  assert.equal(core.validateApiUrl('invalid-url-string'), null);
});

test('B4-NEG-03: copilot component with invalid data-api-url fails closed without sending fetch', async () => {
  const copilot = createCopilot({ 'data-api-url': 'http://remote-insecure.test' });
  copilot.tokenProvider = async () => 'test-token';
  const fetchCalls = [];
  global.fetch = async (...args) => {
    fetchCalls.push(args);
    return { ok: true, json: async () => ({}) };
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Hello';
  const sendBtn = copilot.shadowRoot.querySelector('#send-btn');
  await sendBtn.click();
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(fetchCalls.length, 0);
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /data-api-url must be https/);
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 5: Token Provider + Headers
// ---------------------------------------------------------------------------------------------------------------

test('B5-POS-01: buildHeaders formats Bearer token without client-injected tenant', () => {
  const headers = core.buildHeaders('valid.jwt.payload');
  assert.deepEqual(headers, {
    'Content-Type': 'application/json',
    Authorization: 'Bearer valid.jwt.payload',
  });
  assert.equal(headers['X-Tenant-Id'], undefined);
});

test('B5-POS-02: copilot component calls async tokenProvider on each request and attaches Authorization', async () => {
  let tokenCounter = 0;
  const copilot = createCopilot({}, async () => `token-version-${++tokenCounter}`);
  const fetchCalls = [];
  global.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Acknowledged"}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ reply_text: 'Acknowledged' }),
    };
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  const sendBtn = copilot.shadowRoot.querySelector('#send-btn');

  input.value = 'Message 1';
  await sendBtn.click();
  await new Promise((r) => setTimeout(r, 20));

  input.value = 'Message 2';
  await sendBtn.click();
  await new Promise((r) => setTimeout(r, 20));

  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[0].opts.headers.Authorization, 'Bearer token-version-1');
  assert.equal(fetchCalls[1].opts.headers.Authorization, 'Bearer token-version-2');
  assert.equal(fetchCalls[0].opts.headers['X-Tenant-Id'], undefined);
});

test('B5-BND-01: buildHeaders accepts long JWT token boundary (4096 chars)', () => {
  const longToken = 'header.' + 'payload'.repeat(500) + '.signature';
  const headers = core.buildHeaders(longToken);
  assert.equal(headers.Authorization, `Bearer ${longToken}`);
});

test('B5-NEG-01: buildHeaders rejects empty, whitespace, multiline, or non-string tokens', () => {
  const invalidTokens = ['', '   ', 'token with space', 'token\nnewline', 'token\ttab', null, undefined, 42, {}];
  for (const t of invalidTokens) {
    assert.throws(() => core.buildHeaders(t), /A valid access token is required/);
  }
});

test('B5-NEG-02: copilot component without tokenProvider fails closed and renders sign-in warning', async () => {
  const copilot = createCopilot({}, null); // no tokenProvider
  const fetchCalls = [];
  global.fetch = async (...args) => {
    fetchCalls.push(args);
    return { ok: true, json: async () => ({}) };
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Check leave policy';
  const sendBtn = copilot.shadowRoot.querySelector('#send-btn');
  await sendBtn.click();
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(fetchCalls.length, 0);
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Not signed in: the host page must set tokenProvider/);
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 6: Every fetch checks res.ok and renders describeHttpError
// ---------------------------------------------------------------------------------------------------------------

test('B6-POS-01: /v1/chat 200 OK renders reply_text, citations, thoughts, and suggested actions', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Payroll reconciliation complete for 45 employees.',
          thought_process: ['Validated tax slabs against Section 115BAC'],
          statutory_citations: ['Indian Income Tax Act, 1961 - Sec 115BAC'],
          suggested_actions: [{ label: 'Export Report', name: 'export' }],
        }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Reconcile payroll';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 10));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Payroll reconciliation complete for 45 employees/);
  assert.match(chatLogs.textContent, /Validated tax slabs against Section 115BAC/);
  assert.match(chatLogs.textContent, /Indian Income Tax Act, 1961 - Sec 115BAC/);
  assert.match(chatLogs.textContent, /Export Report/);
});

test('B6-POS-02: /v1/voice/turn 200 OK renders spoken audio response and plays audio', async () => {
  audioInstances.length = 0;
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/voice/turn')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Statutory gratuity is AED 35,000.',
          reply_audio_base64: 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=',
        }),
      };
    }
  };

  await copilot.sendVoiceAudio('fake-input-audio-base64');
  await new Promise((r) => setTimeout(r, 10));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Statutory gratuity is AED 35,000/);
  assert.equal(audioInstances.length, 1);
  assert.match(audioInstances[0].src, /^data:audio\/wav;base64,/);
  assert.equal(audioInstances[0].played, true);
});

test('B6-BND-01: describeHttpError formats with error code or status fallback cleanly', () => {
  assert.equal(core.describeHttpError(401, { error: 'INVALID_TOKEN' }), 'Request failed (401): INVALID_TOKEN');
  assert.equal(core.describeHttpError(403, { error: 'SAME_PERSON' }), 'Request failed (403): SAME_PERSON');
  assert.equal(core.describeHttpError(409, { error: 'CONFLICT' }), 'Request failed (409): CONFLICT');
  assert.equal(core.describeHttpError(429, { error: 'LLM_BUDGET_EXHAUSTED' }), 'Request failed (429): LLM_BUDGET_EXHAUSTED');
  assert.equal(core.describeHttpError(500, null), 'Request failed (500)');
  assert.equal(core.describeHttpError(502, { detail: 'Bad gateway' }), 'Request failed (502)');
  assert.equal(core.describeHttpError(503, {}), 'Request failed (503)');
});

const HTTP_ERROR_STATUSES = [
  { status: 401, body: { error: 'INVALID_TOKEN' }, expected: 'Request failed (401): INVALID_TOKEN' },
  { status: 403, body: { error: 'FORBIDDEN' }, expected: 'Request failed (403): FORBIDDEN' },
  { status: 409, body: { error: 'CONFLICT' }, expected: 'Request failed (409): CONFLICT' },
  { status: 422, body: { detail: [{ msg: 'Invalid entity' }] }, expected: 'Request failed (422)' },
  { status: 429, body: { error: 'LLM_BUDGET_EXHAUSTED' }, expected: 'Request failed (429): LLM_BUDGET_EXHAUSTED' },
  { status: 500, body: null, expected: 'Request failed (500)' },
  { status: 502, body: null, expected: 'Request failed (502)' },
  { status: 503, body: { error: 'HOST_NOT_CONFIGURED' }, expected: 'Request failed (503): HOST_NOT_CONFIGURED' },
];

for (const { status, body, expected } of HTTP_ERROR_STATUSES) {
  test(`B6-NEG-HTTP: /v1/chat handles status ${status} and renders describeHttpError`, async () => {
    const copilot = createCopilot({}, async () => 'token-test');
    global.fetch = async () => ({
      ok: false,
      status,
      json: async () => {
        if (!body) throw new Error('Not JSON');
        return body;
      },
    });

    const input = copilot.shadowRoot.querySelector('#chat-input');
    input.value = 'Test trigger';
    await copilot.shadowRoot.querySelector('#send-btn').click();
    await new Promise((r) => setTimeout(r, 10));

    const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
    assert.match(chatLogs.textContent, new RegExp(expected.replace(/([()])/g, '\\$1')));
  });
}

test('B6-NEG-NET: /v1/chat handles network failure gracefully without crashing component', async () => {
  const copilot = createCopilot({}, async () => 'token-test');
  global.fetch = async () => {
    throw new TypeError('Failed to fetch: Connection refused');
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Trigger network failure';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 10));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Could not reach AI sidecar: Failed to fetch: Connection refused/);
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 7: Remediation Confirm -> POST /v1/writeback/proposals
// ---------------------------------------------------------------------------------------------------------------

test('B7-POS-01: remediation flow converts action to proposal, requires confirmation, and posts to /v1/writeback/proposals', async () => {
  const copilot = createCopilot({}, async () => 'token-proposer');
  const fetchCalls = [];

  global.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts, body: opts.body ? JSON.parse(opts.body) : null });
    if (url.endsWith('/v1/chat')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Probation period exceeds statutory limit.',
          suggested_actions: [
            {
              label: 'Adjust Probation Period',
              entity_type: 'EMPLOYEE',
              entity_id: 'HR-EMP-0042',
              patch: { probation_days: 180 },
            },
          ],
        }),
      };
    }
    if (url.endsWith('/v1/writeback/proposals')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ id: 'PROP-2026-99', status: 'PENDING' }),
      };
    }
  };

  // Step 1: Send chat prompt
  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Audit employee probation';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 10));

  // Step 2: Verify action button rendered
  const actionBtns = copilot.shadowRoot.querySelectorAll('.action-btn');
  assert.equal(actionBtns.length, 1);
  assert.equal(actionBtns[0].textContent, 'Adjust Probation Period');

  // Step 3: Click remediation action button -> confirms proposal
  await actionBtns[0].click();
  await new Promise((r) => setTimeout(r, 10));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Propose this change to Employee HR-EMP-0042\?/);
  assert.match(chatLogs.textContent, /probation_days → 180/);
  assert.match(chatLogs.textContent, /A different approver must approve it before anything is written\./);

  // Step 4: Click the generated confirmation button
  const updatedBtns = copilot.shadowRoot.querySelectorAll('.action-btn');
  const confirmBtn = updatedBtns[updatedBtns.length - 1];
  assert.equal(confirmBtn.textContent, '📝 Propose this change');

  await confirmBtn.click();
  await new Promise((r) => setTimeout(r, 10));

  // Step 5: Verify POST /v1/writeback/proposals request shape (/v1/chat/stream falls back to /v1/chat, then proposal)
  assert.equal(fetchCalls.length, 3);
  const propCall = fetchCalls[2];
  assert.equal(propCall.url, 'https://ai.acme.test/v1/writeback/proposals');
  assert.equal(propCall.opts.method, 'POST');
  assert.deepEqual(propCall.body, {
    doctype: 'Employee',
    name: 'HR-EMP-0042',
    patch: { probation_days: 180 },
  });
  assert.equal(propCall.opts.headers.Authorization, 'Bearer token-proposer');

  // Step 6: Verify success message rendered in chat logs
  assert.match(chatLogs.textContent, /📝 Proposal PROP-2026-99 created \(PENDING\)\./);
});

test('B7-BND-01: buildProposalBody boundary checks on entity_id length (1 and 140 accepted, 141 rejected)', () => {
  const minAction = { entity_type: 'EMPLOYEE', entity_id: 'E', patch: { active: true } };
  assert.deepEqual(core.buildProposalBody(minAction), { doctype: 'Employee', name: 'E', patch: { active: true } });

  const maxAction = { entity_type: 'EMPLOYEE', entity_id: 'A'.repeat(140), patch: { days: 30 } };
  assert.deepEqual(core.buildProposalBody(maxAction), {
    doctype: 'Employee',
    name: 'A'.repeat(140),
    patch: { days: 30 },
  });

  const overAction = { entity_type: 'EMPLOYEE', entity_id: 'A'.repeat(141), patch: { days: 30 } };
  assert.equal(core.buildProposalBody(overAction), null);
});

test('B7-BND-02: buildProposalBody permits internal dots and allowed symbols in entity_id', () => {
  const action = { entity_type: 'EMPLOYEE', entity_id: 'EMP.2026-01_A/B', patch: { rate: 25.5 } };
  assert.deepEqual(core.buildProposalBody(action), {
    doctype: 'Employee',
    name: 'EMP.2026-01_A/B',
    patch: { rate: 25.5 },
  });
});

test('B7-BND-03: buildProposalBody accepts multi-field patch and preserves falsy values (0, false)', () => {
  const action = {
    entity_type: 'EMPLOYEE',
    entity_id: 'EMP-01',
    patch: { notice_days: 0, is_active: false, role: 'Specialist' },
  };
  const body = core.buildProposalBody(action);
  assert.deepEqual(body.patch, { notice_days: 0, is_active: false, role: 'Specialist' });
});

test('B7-NEG-01: buildProposalBody rejects path traversal dot segments in entity_id', () => {
  const traversalIds = ['.', '..', './EMP', '../EMP', 'EMP/..', 'EMP/../OTHER', 'EMP/./OTHER'];
  for (const tid of traversalIds) {
    const action = { entity_type: 'EMPLOYEE', entity_id: tid, patch: { days: 10 } };
    assert.equal(core.buildProposalBody(action), null, `Failed to reject path traversal: ${tid}`);
  }
});

test('B7-NEG-02: buildProposalBody rejects unsupported or prototype entity_types', () => {
  const badTypes = ['PAYROLL', 'LEAVE', 'SALARY_SLIP', 'USER', 'toString', 'constructor', '__proto__', '', null];
  for (const t of badTypes) {
    const action = { entity_type: t, entity_id: 'EMP-01', patch: { days: 10 } };
    assert.equal(core.buildProposalBody(action), null, `Failed to reject entity_type: ${t}`);
  }
});

test('B7-NEG-03: buildProposalBody rejects empty, non-plain, array, or non-object patches', () => {
  const badPatches = [{}, [], null, undefined, 42, 'string-patch', new Date(), new RegExp('^a')];
  for (const p of badPatches) {
    const action = { entity_type: 'EMPLOYEE', entity_id: 'EMP-01', patch: p };
    assert.equal(core.buildProposalBody(action), null);
  }
});

test('B7-NEG-04: confirmRemediation renders error when buildProposalBody returns null and sends no POST', async () => {
  const copilot = createCopilot({}, async () => 'token-test');
  const fetchCalls = [];
  global.fetch = async (...args) => {
    fetchCalls.push(args);
    return { ok: true, json: async () => ({}) };
  };

  copilot.confirmRemediation({ entity_type: 'UNSUPPORTED', entity_id: 'EMP-01', patch: { a: 1 } });
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(fetchCalls.length, 0);
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /This suggestion does not identify a record this assistant may change/);
});

test('B7-NEG-05: proposeRemediation handles server rejection (409 Conflict, 422 Invalid) cleanly', async () => {
  const copilot = createCopilot({}, async () => 'token-test');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/writeback/proposals')) {
      return {
        ok: false,
        status: 409,
        json: async () => ({ error: 'CONFLICT' }),
      };
    }
  };

  await copilot.proposeRemediation({ doctype: 'Employee', name: 'EMP-01', patch: { days: 30 } });
  await new Promise((r) => setTimeout(r, 10));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /❌ Not proposed: Request failed \(409\): CONFLICT/);
});

// ---------------------------------------------------------------------------------------------------------------
// Behaviour 8: Streaming Rendering
// ---------------------------------------------------------------------------------------------------------------

test('B8-POS-01: streaming token chunks update markdown progressively until fully formatted', () => {
  const chunks = [
    'Computing',
    'Computing **statutory',
    'Computing **statutory gratuity**',
    'Computing **statutory gratuity** for `EMP-01`',
    'Computing **statutory gratuity** for `EMP-01`:\n\n- Gratuity: AED 25,000\n- Notice: 30 days\n\n[Policy](https://hr.acme.test/gratuity)',
  ];

  let lastHtml = '';
  for (const chunk of chunks) {
    lastHtml = core.formatMarkdown(chunk);
    // At every chunk, the result must be well-formed and non-empty
    assert.ok(lastHtml.length > 0);
    assert.doesNotMatch(lastHtml, /<script/i);
  }

  // Final chunk has full markdown structures
  assert.match(lastHtml, /<strong>statutory gratuity<\/strong>/);
  assert.match(lastHtml, /<code class="inline-code">EMP-01<\/code>/);
  assert.match(lastHtml, /<ul class="md-ul">[\s\S]*<li>Gratuity: AED 25,000<\/li>/);
  assert.match(lastHtml, /<a href="https:\/\/hr\.acme\.test\/gratuity"/);
});

test('B8-BND-01: stream boundary with unclosed formatting constructs remains safe at each token', () => {
  const tokenProgression = [
    '```',
    '```python\n',
    '```python\ndef calculate():',
    '```python\ndef calculate():\n    return 100',
    '```python\ndef calculate():\n    return 100\n```',
  ];

  for (const step of tokenProgression) {
    const html = core.formatMarkdown(step);
    // Unclosed code block does not crash parser or emit unescaped tags
    assert.doesNotMatch(html, /<script/i);
    assert.doesNotMatch(html, /undefined/);
  }

  const finalHtml = core.formatMarkdown(tokenProgression[tokenProgression.length - 1]);
  assert.match(finalHtml, /<pre class="code-block"><code class="lang-python">def calculate\(\):\n    return 100<\/code><\/pre>/);
});

test('B8-BND-02: stream boundary with partial link URL does not create premature anchor tag', () => {
  const steps = [
    '[Documentation](',
    '[Documentation](https://',
    '[Documentation](https://hr.acme.test',
    '[Documentation](https://hr.acme.test/guide',
    '[Documentation](https://hr.acme.test/guide)',
  ];

  for (let i = 0; i < steps.length - 1; i++) {
    const partialHtml = core.formatMarkdown(steps[i]);
    assert.doesNotMatch(partialHtml, /<a\b/i, `Step ${i} should not contain anchor tag: ${steps[i]}`);
  }

  const completeHtml = core.formatMarkdown(steps[steps.length - 1]);
  assert.match(completeHtml, /<a href="https:\/\/hr\.acme\.test\/guide"/);
});

test('B8-NEG-01: XSS attack payload streamed chunk-by-chunk never executes or emits unescaped tag at any step', () => {
  const scriptChunks = ['<', '<s', '<sc', '<scr', '<scri', '<script', '<script>', '<script>alert', '<script>alert(1)', '<script>alert(1)</', '<script>alert(1)</script>'];

  for (const chunk of scriptChunks) {
    const html = core.formatMarkdown(chunk);
    assert.doesNotMatch(html, /<script\b/i);
    assert.doesNotMatch(html, /<[a-z][^>]*\son[a-z]+/i);
  }
});

test('B8-NEG-02: attribute breakout attempt streamed chunk-by-chunk never injects event handler', () => {
  const breakoutChunks = [
    '[click](https://example.com',
    '[click](https://example.com"',
    '[click](https://example.com" ',
    '[click](https://example.com" onmouseover="',
    '[click](https://example.com" onmouseover="alert(1))',
    '[click](https://example.com" onmouseover="alert(1)")',
  ];

  for (const chunk of breakoutChunks) {
    const html = core.formatMarkdown(chunk);
    assert.doesNotMatch(html, /<[a-z][^>]*\son[a-z]+/i);
    for (const m of html.matchAll(/href="([^"]*)"/g)) {
      assert.match(m[1], /^https:\/\/([^<>"'&\s⟦⟧]|&amp;)+$/);
    }
  }
});

test('B8-POS-03: /v1/chat/stream renders tokens progressively into assistant bubble as plain text', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "token", "data": "Processing statutory "}\n\n',
        'data: {"event": "token", "data": "gratuity for EMP-01."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Calculate gratuity';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Processing statutory gratuity for EMP-01\./);
});

test('B8-POS-04: /v1/chat/stream renders tool_call step line Using <name>…', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "tool_call", "data": {"name": "calculate_gratuity"}}\n\n',
        'data: {"event": "token", "data": "Result ready."}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Run tool';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Using calculate_gratuity…/);
  assert.match(chatLogs.textContent, /Result ready\./);
});

test('B8-POS-05: /v1/chat/stream partial event renders notice that answer may be incomplete', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "partial", "data": true}\n\n',
        'data: {"event": "token", "data": "Partial output"}\n\n',
        'data: {"event": "done", "data": {}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Trigger partial';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /answer may be incomplete/i);
  assert.match(chatLogs.textContent, /Partial output/);
});

test('B8-POS-06: /v1/chat/stream error event renders error code', async () => {
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/chat/stream')) {
      return createSseStreamResponse([
        'data: {"event": "error", "data": {"error": "PII_EGRESS_BLOCKED"}}\n\n',
      ]);
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Trigger PII block';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /PII_EGRESS_BLOCKED/);
});

test('B8-POS-07: /v1/chat/stream 404/405 falls back to /v1/chat', async () => {
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
        json: async () => ({ reply_text: 'Fallback successful' }),
      };
    }
  };

  const input = copilot.shadowRoot.querySelector('#chat-input');
  input.value = 'Fallback test';
  await copilot.shadowRoot.querySelector('#send-btn').click();
  await new Promise((r) => setTimeout(r, 20));

  assert.ok(called.some((u) => u.endsWith('/v1/chat/stream')));
  assert.ok(called.some((u) => u.endsWith('/v1/chat')));
  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Fallback successful/);
});

test('B8-POS-08: /v1/voice/turn with audio_withheld renders notice and does not play audio', async () => {
  audioInstances.length = 0;
  const copilot = createCopilot({}, async () => 'token-ok');
  global.fetch = async (url) => {
    if (url.endsWith('/v1/voice/turn')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          reply_text: 'Sensitive salary details: AED 50,000.',
          audio_withheld: 'PII_EGRESS_BLOCKED',
          reply_audio_base64: 'UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=',
        }),
      };
    }
  };

  await copilot.sendVoiceAudio('voice-payload');
  await new Promise((r) => setTimeout(r, 20));

  const chatLogs = copilot.shadowRoot.querySelector('#chat-logs');
  assert.match(chatLogs.textContent, /Sensitive salary details: AED 50,000\./);
  assert.match(chatLogs.textContent, /Audio withheld: the reply contains personal data/);
  assert.equal(audioInstances.length, 0, 'No audio should play when audio_withheld is set');
});
