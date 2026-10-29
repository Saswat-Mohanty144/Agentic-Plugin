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

const XSS = [
  '<img src=x onerror=alert(1)>',
  '<script>alert(1)</script>',
  '[x](https://a"onmouseover="alert(1))',
  '[x](javascript:alert(1))',
  '[x](https://evil.test/"><svg onload=alert(1)>)',
  '**<svg/onload=alert(1)>**',
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
    assert.doesNotMatch(html, /href="(?!https:\/\/)/i);
    assert.doesNotMatch(html, /javascript:[^<]*<\/a>/i);
  });
}

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

// Remediation --------------------------------------------------------------------------------------------------
test('remediation body uses the action entity and carries no approver claims', () => {
  const body = core.buildRemediationBody({ violation_id: 'V1', entity_id: 'EMP-7', patch: { probation_days: 180 } });
  assert.deepEqual(body, { violation_id: 'V1', target_entity: 'EMPLOYEE', entity_id: 'EMP-7',
    patch_payload: { probation_days: 180 } });
});

for (const bad of [{ violation_id: 'V1', patch: {} }, { entity_id: 'E', patch: {} }, { violation_id: 'V1', entity_id: 'E' }]) {
  test(`remediation refused without ${JSON.stringify(bad)}`, () => assert.equal(core.buildRemediationBody(bad), null));
}

test('describePatch is escaped text for the confirmation step', () => {
  assert.deepEqual(core.describePatch({ probation_days: 180, note: '<b>x</b>' }),
    ['probation_days → 180', 'note → &lt;b&gt;x&lt;/b&gt;']);
});

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
});
