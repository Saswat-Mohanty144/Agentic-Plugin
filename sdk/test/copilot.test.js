// Security tests for the copilot SDK core (piece D). Run: node --test sdk/test/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const core = require('../copilot.js');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'copilot.js'), 'utf8');

test('escapeHtml neutralises every HTML-significant character', () => {
  assert.equal(core.escapeHtml(`<img src=x onerror="a('b')">&`), '&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;');
  assert.equal(core.escapeHtml(null), '');
  assert.equal(core.escapeHtml(42), '42');
});

// Vectors that must never produce an anchor at all (agy D-F8: link assertions were vacuous for them).
const NO_LINK = new Set([
  '<img src=x onerror=alert(1)>',
  '<script>alert(1)</script>',
  '[x](javascript:alert(1))',
  '**<svg/onload=alert(1)>**',
  '> <iframe src=javascript:alert(1)>',
  '```\n<script>alert(1)</script>\n```',
  '`<b onmouseover=alert(1)>`',
  '# <img src=x onerror=alert(1)>',
  '- <a href="javascript:alert(1)">x</a>',
  '[x](https://a"onmouseover="alert(1))',
  '[x](https://evil.test/"><svg onload=alert(1)>)',
]);
const XSS = [
  ...NO_LINK,
  '[x](https://example.com#`code`)',
  '[x](https://e.test/?a=1&b=2"onclick="x)',
  // literal entity text in a URL is re-escaped (&amp;lt;) — inert, so a link is allowed; href check still applies
  '[x](https://e.test/&lt;b&gt;)',
  '[x](https://e.test/&#106;s)',
  '> <iframe src=javascript:alert(1)>',
  '```\n<script>alert(1)</script>\n```',
  '`<b onmouseover=alert(1)>`',
  '# <img src=x onerror=alert(1)>',
  '- <a href="javascript:alert(1)">x</a>',
];

for (const vector of XSS) {
  test(`formatMarkdown neutralises: ${vector.slice(0, 40)}`, () => {
    const html = core.formatMarkdown(vector);
    assert.doesNotMatch(html, /<(script|img|svg|iframe)\b/i);
    // no REAL tag may carry an event-handler attribute (escaped text like '&lt;img onerror=' is inert)
    assert.doesNotMatch(html, /<[a-z][^>]*\son[a-z]+\s*=/i);
    assert.match(html.replace(/<\/?(p|h[234]|strong|em|ul|ol|li|br\/|blockquote|pre|code|a)( [^>]*)?>/g, ''), /^[^<>]*$/);
    if (NO_LINK.has(vector)) assert.doesNotMatch(html, /<a\b/i);
    // every href is https and contains no markup, quotes or entities other than &amp;
    for (const m of html.matchAll(/href="([^"]*)"/g)) {
      assert.match(m[1], /^https:\/\/([^<>"'&\s⟦⟧]|&amp;)+$/);
    }
  });
}

test('inline code inside a link URL cannot reach the href (agy D-F4)', () => {
  const html = core.formatMarkdown('[x](https://example.com#`code`)');
  assert.doesNotMatch(html, /href="[^"]*</);
  assert.doesNotMatch(html, /<a\b/);
});

test('https links with query parameters keep working (agy D-F9)', () => {
  assert.match(core.formatMarkdown('[Docs](https://example.com/api?a=1&b=2)'),
    /<a href="https:\/\/example\.com\/api\?a=1&amp;b=2" target="_blank" rel="noopener noreferrer" class="md-link">Docs<\/a>/);
});

test('formatMarkdown keeps legitimate formatting', () => {
  const html = core.formatMarkdown('# Title\n\n**bold** and [docs](https://hr.acme.test/policy)\n\n- one\n- two');
  assert.match(html, /<h2 class="md-h2">Title<\/h2>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<a href="https:\/\/hr\.acme\.test\/policy" target="_blank" rel="noopener noreferrer" class="md-link">docs<\/a>/);
  assert.match(html, /<ul class="md-ul">[\s\S]*<li>one<\/li>[\s\S]*<li>two<\/li>/);
});

test('http (non-TLS) links are not turned into anchors', () => {
  assert.doesNotMatch(core.formatMarkdown('[x](http://evil.test)'), /<a /);
});

// postMessage ----------------------------------------------------------------------------------------------
const HOST = 'https://hr.acme.test';
const nav = (origin, data) => ({ origin, data });

test('messages from the configured host origin are trusted', () => {
  assert.equal(core.isTrustedMessage(nav(HOST, { type: 'HRMS_NAVIGATE' }), HOST), true);
});

for (const [label, event, host] of [
  ['other origin', nav('https://evil.test', { type: 'HRMS_NAVIGATE' }), HOST],
  ['port variant', nav('https://hr.acme.test:444', { type: 'HRMS_NAVIGATE' }), HOST],
  ['no host origin configured', nav(HOST, { type: 'HRMS_NAVIGATE' }), ''],
  ['wildcard host origin', nav(HOST, { type: 'HRMS_NAVIGATE' }), '*'],
  ['non-object data', nav(HOST, 'HRMS_NAVIGATE'), HOST],
  ['wrong type', nav(HOST, { type: 'OTHER' }), HOST],
]) {
  test(`untrusted message rejected: ${label}`, () => assert.equal(core.isTrustedMessage(event, host), false));
}

test('sanitizeContext keeps only validated navigation fields', () => {
  assert.deepEqual(core.sanitizeContext({ entity: 'Employee', record_id: 'HR-EMP-00001', user_role: 'SUPER_ADMIN' }),
    { entity: 'Employee', record_id: 'HR-EMP-00001' });
});

for (const bad of [
  { entity: '<img>', record_id: '1' }, { entity: 'Employee', record_id: 'x"y' },
  { entity: 'Employee', record_id: 'a'.repeat(141) }, { entity: '', record_id: '1' }, { entity: 'Employee' }, null,
]) {
  test(`sanitizeContext rejects ${JSON.stringify(bad)}`, () => assert.equal(core.sanitizeContext(bad), null));
}

test('record_id boundary 140 accepted', () => {
  assert.notEqual(core.sanitizeContext({ entity: 'Employee', record_id: 'a'.repeat(140) }), null);
});

// API URL + headers ------------------------------------------------------------------------------------------
for (const [url, expected] of [
  ['https://ai.acme.test', 'https://ai.acme.test'],
  ['https://ai.acme.test/plugin/', 'https://ai.acme.test/plugin'],
  ['http://localhost:8000', 'http://localhost:8000'],
  ['http://127.0.0.1:8000', 'http://127.0.0.1:8000'],
  ['http://ai.acme.test', null],
  ['https://user:pw@ai.acme.test', null],
  ['javascript:alert(1)', null],
  ['', null],
]) {
  test(`validateApiUrl(${url || '<empty>'})`, () => assert.equal(core.validateApiUrl(url), expected));
}

test('buildHeaders sends a bearer token and no client-chosen tenant', () => {
  const h = core.buildHeaders('abc.def.ghi');
  assert.deepEqual(h, { 'Content-Type': 'application/json', Authorization: 'Bearer abc.def.ghi' });
});

for (const bad of ['', '  ', 'a b', 'a\nb', null, 42]) {
  test(`buildHeaders rejects token ${JSON.stringify(bad)}`, () => assert.throws(() => core.buildHeaders(bad)));
}

// Remediation → write-back PROPOSAL (approval is a different person, server-side; agy D-F2) ---------------------
test('a remediation becomes a proposal for the action entity, with no approval claims', () => {
  const body = core.buildProposalBody({ violation_id: 'V1', entity_type: 'EMPLOYEE', entity_id: 'HR-EMP-00007',
    patch: { probation_days: 180 } });
  assert.deepEqual(body, { doctype: 'Employee', name: 'HR-EMP-00007', patch: { probation_days: 180 } });
});

const GOOD = { violation_id: 'V1', entity_type: 'EMPLOYEE', entity_id: 'E1', patch: { a: 1 } };
for (const [why, bad] of [
  ['no entity_id', { ...GOOD, entity_id: undefined }],
  ['non-string entity_id', { ...GOOD, entity_id: 7 }],
  ['entity_id with a path', { ...GOOD, entity_id: '../x' }],
  ['no entity_type', { ...GOOD, entity_type: undefined }],
  ['unsupported entity_type (agy D-F6)', { ...GOOD, entity_type: 'PAYROLL' }],
  ['inherited entity_type', { ...GOOD, entity_type: 'toString' }],
  ['no patch', { ...GOOD, patch: undefined }],
  ['array patch (agy D-F7)', { ...GOOD, patch: [] }],
  ['empty patch', { ...GOOD, patch: {} }],
  ['non-plain patch', { ...GOOD, patch: new Date() }],
  ['null action', null],
]) {
  test(`proposal refused: ${why}`, () => assert.equal(core.buildProposalBody(bad), null));
}

test('confirmation text is plain (rendered with textContent), never pre-escaped (agy D-F3)', () => {
  const text = core.confirmationText({ doctype: 'Employee', name: 'EMP & 01', patch: { note: '<b>x</b>', days: 180 } });
  assert.equal(text, 'Propose this change to Employee EMP & 01?\nnote → <b>x</b>\ndays → 180\n' +
    'A different approver must approve it before anything is written.');
});

for (const [status, body, expected] of [
  [401, { error: 'INVALID_TOKEN' }, 'Request failed (401): INVALID_TOKEN'],
  [422, { detail: [{ msg: 'x' }] }, 'Request failed (422)'],
  [500, null, 'Request failed (500)'],
  [429, { error: 'LLM_BUDGET_EXHAUSTED' }, 'Request failed (429): LLM_BUDGET_EXHAUSTED'],
]) {
  test(`describeHttpError ${status} (agy D-F5)`, () => assert.equal(core.describeHttpError(status, body), expected));
}

for (const [value, expected] of [['AE', 'AE'], ['us', 'IN'], ['', 'IN'], [null, 'IN'], ['SA', 'SA']]) {
  test(`normalizeJurisdiction(${value})`, () => assert.equal(core.normalizeJurisdiction(value), expected));
}

// Source-level guarantees ------------------------------------------------------------------------------------------
test('no wildcard postMessage target, no approver claims, no X-Tenant-Id, no hard-coded entity', () => {
  assert.doesNotMatch(SOURCE, /postMessage\([^)]*['"]\*['"]/);
  assert.doesNotMatch(SOURCE, /approver_(id|role)/);
  assert.doesNotMatch(SOURCE, /X-Tenant-Id/);
  assert.doesNotMatch(SOURCE, /EMP-AUTO/);
  assert.doesNotMatch(SOURCE, /data-action=/);
  assert.doesNotMatch(SOURCE, /\/v1\/remediate\/execute/);  // proposals only (agy D-F2)
  assert.match(SOURCE, /\/v1\/writeback\/proposals/);
});

test('every fetch response is checked before use (agy D-F5)', () => {
  const fetches = SOURCE.match(/await fetch\(/g) || [];
  const checks = SOURCE.match(/if \(!res\.ok\)/g) || [];
  assert.ok(fetches.length >= 3 && checks.length === fetches.length, `${fetches.length} fetches, ${checks.length} checks`);
});
