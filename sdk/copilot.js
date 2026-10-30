/**
 * Enterprise Agentic HRMS Copilot Embeddable SDK (<hrms-copilot>)
 * Version: 2.1.0
 * Features: Shadow DOM isolation, postMessage Host Context Sync, Interactive Diff Remediation Modal, Voice AI, Rich Markdown Formatting
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------------------------------------------
  // Security core (piece D): pure functions, unit-tested under Node (sdk/test/copilot.test.js).
  // ---------------------------------------------------------------------------------------------------------------
  const ENTITY_RE = /^[A-Za-z][A-Za-z ]{0,63}$/;
  const RECORD_ID_RE = /^[A-Za-z0-9._\/-]{1,140}$/;
  const JURISDICTIONS = ['IN', 'AE', 'SA', 'US'];

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Everything is escaped FIRST; markdown is then applied to inert text, so model output can never become markup.
  function formatMarkdown(text) {
    if (!text) return '';
    let str = escapeHtml(String(text).replace(/\r\n/g, '\n'));

    const codeBlocks = [];
    str = str.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      codeBlocks.push('<pre class="code-block"><code class="lang-' + (lang || 'text') + '">' + code.trim() + '</code></pre>');
      return '⟦CB' + (codeBlocks.length - 1) + '⟧';
    });
    const inlineCodes = [];
    str = str.replace(/`([^`\n]+)`/g, function (_, code) {
      inlineCodes.push('<code class="inline-code">' + code + '</code>');
      return '⟦IC' + (inlineCodes.length - 1) + '⟧';
    });

    str = str.replace(/^&gt;\s+(.*$)/gim, '<blockquote class="md-quote">$1</blockquote>');
    str = str.replace(/^###\s+(.*$)/gim, '<h4 class="md-h4">$1</h4>');
    str = str.replace(/^##\s+(.*$)/gim, '<h3 class="md-h3">$1</h3>');
    str = str.replace(/^#\s+(.*$)/gim, '<h2 class="md-h2">$1</h2>');
    // Links: https only. The (already escaped) URL may contain '&amp;' but no other entity (so no quotes or angle
    // brackets), no whitespace and no code placeholder (agy D-F4, D-F9).
    str = str.replace(/\[([^\]]+)\]\((https:\/\/(?:[^\s)&⟦⟧]|&amp;)+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');
    str = str.replace(/\*\*\*(.*?)\*\*\*/g, '<strong><em>$1</em></strong>');
    str = str.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    str = str.replace(/__(.*?)__/g, '<strong>$1</strong>');
    str = str.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');

    const out = [];
    let list = null;
    for (const line of str.split('\n')) {
      const bullet = line.match(/^[*\-+]\s+(.*)/);
      const ordered = line.match(/^\d+\.\s+(.*)/);
      const kind = bullet ? 'ul' : ordered ? 'ol' : null;
      if (kind !== list) {
        if (list) out.push('</' + list + '>');
        if (kind) out.push('<' + kind + ' class="md-' + kind + '">');
        list = kind;
      }
      out.push(kind ? '<li>' + (bullet || ordered)[1] + '</li>' : line);
    }
    if (list) out.push('</' + list + '>');
    str = out.join('\n');

    str = str.split(/\n\n+/).map(function (chunk) {
      chunk = chunk.trim();
      if (!chunk) return '';
      if (/^(<h[234]|<ul|<ol|<blockquote|⟦CB)/.test(chunk)) return chunk;
      return '<p class="md-p">' + chunk.replace(/\n/g, '<br/>') + '</p>';
    }).filter(Boolean).join('\n');

    str = str.replace(/⟦CB(\d+)⟧/g, function (_, i) { return codeBlocks[parseInt(i, 10)] || ''; });
    str = str.replace(/⟦IC(\d+)⟧/g, function (_, i) { return inlineCodes[parseInt(i, 10)] || ''; });
    return str;
  }

  function isTrustedMessage(event, hostOrigin) {
    return Boolean(
      typeof hostOrigin === 'string' && hostOrigin && hostOrigin !== '*' &&
      event && event.origin === hostOrigin &&
      event.data && typeof event.data === 'object' && event.data.type === 'HRMS_NAVIGATE'
    );
  }

  function sanitizeContext(data) {
    if (!data || typeof data !== 'object') return null;
    const entity = data.entity;
    const recordId = data.record_id;
    if (typeof entity !== 'string' || !ENTITY_RE.test(entity)) return null;
    if (typeof recordId !== 'string' || !RECORD_ID_RE.test(recordId)) return null;
    return { entity: entity, record_id: recordId };  // roles are never taken from the page; they come from the token
  }

  function validateApiUrl(raw) {
    let url;
    try { url = new URL(String(raw || '')); } catch (e) { return null; }
    if (url.username || url.password) return null;
    const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
    if (!(url.protocol === 'https:' || (url.protocol === 'http:' && local))) return null;
    return (url.origin + url.pathname).replace(/\/+$/, '');
  }

  function buildHeaders(token) {
    if (typeof token !== 'string' || !token || /\s/.test(token)) throw new Error('A valid access token is required');
    return { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token };
  }

  // Host document types the SDK may propose changes to; anything else is not offered (agy D-F6).
  const PROPOSAL_DOCTYPES = Object.freeze({ EMPLOYEE: 'Employee' });

  function isPlainObject(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
  }

  // A remediation becomes a write-back PROPOSAL; a different person approves it server-side (piece F2, agy D-F2).
  function buildProposalBody(action) {
    if (!action || typeof action !== 'object') return null;
    if (typeof action.entity_id !== 'string' || !RECORD_ID_RE.test(action.entity_id) ||
        /(^|\/)\.\.?(\/|$)/.test(action.entity_id)) return null;  // no dot path segments (defence in depth)
    if (typeof action.entity_type !== 'string' ||
        !Object.prototype.hasOwnProperty.call(PROPOSAL_DOCTYPES, action.entity_type)) return null;
    if (!isPlainObject(action.patch) || Object.keys(action.patch).length === 0) return null;  // agy D-F7
    return { doctype: PROPOSAL_DOCTYPES[action.entity_type], name: action.entity_id, patch: action.patch };
  }

  // Plain text: the caller renders it with textContent, so nothing is escaped here (agy D-F3).
  function describePatch(patch) {
    return Object.keys(patch || {}).map(function (k) { return k + ' → ' + String(patch[k]); });
  }

  function confirmationText(proposal) {
    return ['Propose this change to ' + proposal.doctype + ' ' + proposal.name + '?']
      .concat(describePatch(proposal.patch))
      .concat(['A different approver must approve it before anything is written.'])
      .join('\n');
  }

  function describeHttpError(status, body) {
    const code = body && typeof body.error === 'string' ? body.error : '';
    return 'Request failed (' + status + ')' + (code ? ': ' + code : '');
  }

  function normalizeJurisdiction(value) {
    return JURISDICTIONS.indexOf(value) >= 0 ? value : 'IN';
  }

  const KNOWN_SSE_EVENTS = new Set(['token', 'tool_call', 'partial', 'error', 'done', 'result', 'intent']);

  function parseSseFrames(buffer) {
    if (typeof buffer !== 'string' || !buffer) return { events: [], remaining: '' };
    const parts = buffer.split(/(?:\r?\n){2,}/);
    const remaining = parts.pop() || '';
    const events = [];
    for (const frame of parts) {
      if (!frame.trim()) continue;
      const lines = frame.split(/\r?\n/);
      for (const line of lines) {
        if (!line.startsWith('data: ') && !line.startsWith('data:')) continue;
        const raw = line.startsWith('data: ') ? line.slice(6) : line.slice(5);
        try {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object' && typeof parsed.event === 'string') {
            if (KNOWN_SSE_EVENTS.has(parsed.event)) {
              events.push({ event: parsed.event, data: parsed.data });
            }
          }
        } catch (e) {
          // malformed JSON line ignored
        }
      }
    }
    return { events, remaining };
  }

  const core = { escapeHtml, formatMarkdown, isTrustedMessage, sanitizeContext, validateApiUrl, buildHeaders,
    buildProposalBody, describePatch, confirmationText, describeHttpError, normalizeJurisdiction,
    parseSseFrames };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = core;
  }
  if (typeof HTMLElement === 'undefined') {
    return;  // Node (tests): no DOM, only the core is exported
  }

  class HrmsCopilotElement extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      // The host page sets `element.tokenProvider = async () => accessToken` (never an attribute).
      this.tokenProvider = null;
      this.apiUrl = validateApiUrl(this.getAttribute('data-api-url'));
      this.hostOrigin = this.getAttribute('data-host-origin') || '';
      this.jurisdiction = normalizeJurisdiction(this.getAttribute('data-jurisdiction'));
      this.isOpen = false;
      this.currentContext = {};
      this.mediaRecorder = null;
      this.audioChunks = [];
      this.isRecording = false;
    }

    connectedCallback() {
      this.render();
      this.initPostMessageListener();
    }

    initPostMessageListener() {
      window.addEventListener('message', (event) => {
        if (!isTrustedMessage(event, this.hostOrigin)) return;  // exact host origin only (fail closed)
        const context = sanitizeContext(event.data);
        if (!context) return;
        this.currentContext = context;
        this.updateContextBadge();
      });
    }

    updateContextBadge() {
      const badge = this.shadowRoot.querySelector('#context-badge');
      if (badge && this.currentContext.entity) {
        badge.textContent = `Context: ${this.currentContext.entity} #${this.currentContext.record_id}`;
        badge.style.display = 'inline-block';
      }
    }

    toggleDrawer() {
      this.isOpen = !this.isOpen;
      const drawer = this.shadowRoot.querySelector('#copilot-drawer');
      if (drawer) {
        drawer.classList.toggle('open', this.isOpen);
      }
    }

    async toggleVoiceRecording() {
      const micBtn = this.shadowRoot.querySelector('#mic-btn');
      if (this.isRecording) {
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
          this.mediaRecorder.stop();
        }
        this.isRecording = false;
        if (micBtn) micBtn.classList.remove('recording');
        return;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.audioChunks = [];
        this.mediaRecorder = new MediaRecorder(stream);
        this.mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) this.audioChunks.push(e.data);
        };
        this.mediaRecorder.onstop = async () => {
          const audioBlob = new Blob(this.audioChunks, { type: 'audio/wav' });
          const reader = new FileReader();
          reader.readAsDataURL(audioBlob);
          reader.onloadend = async () => {
            const base64Audio = reader.result.split(',')[1];
            this.sendVoiceAudio(base64Audio);
          };
          stream.getTracks().forEach((track) => track.stop());
        };
        this.mediaRecorder.start();
        this.isRecording = true;
        if (micBtn) micBtn.classList.add('recording');
      } catch (err) {
        this.appendMessage('assistant', `⚠️ Microphone unavailable: ${err.message}`);
      }
    }

    async authHeaders() {
      if (!this.apiUrl) throw new Error('data-api-url must be https:// (or http://localhost for development)');
      if (typeof this.tokenProvider !== 'function') throw new Error('Not signed in: the host page must set tokenProvider');
      return buildHeaders(await this.tokenProvider());
    }

    async sendVoiceAudio(base64Audio) {
      this.appendMessage('user', '🎙️ [Spoken Audio Message]');
      try {
        const res = await fetch(`${this.apiUrl}/v1/voice/turn`, {
          method: 'POST',
          headers: await this.authHeaders(),
          body: JSON.stringify({ audio_base64: base64Audio, jurisdiction: this.jurisdiction }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          this.appendPlain('assistant', `⚠️ ${describeHttpError(res.status, data)}`);
          return;
        }
        this.appendMessage('assistant', data.reply_text, data.suggested_actions, data.statutory_citations);
        if (data && data.audio_withheld) {
          this.appendPlain('assistant', 'Audio withheld: the reply contains personal data');
        } else if (data && data.reply_audio_base64) {
          const audio = new Audio('data:audio/wav;base64,' + data.reply_audio_base64);
          audio.play().catch(() => {});
        }
      } catch (err) {
        this.appendPlain('assistant', `⚠️ Voice processing error: ${err.message}`);
      }
    }

    async sendMessage() {
      const input = this.shadowRoot.querySelector('#chat-input');
      const text = (input.value || '').trim();
      if (!text) return;

      input.value = '';
      this.appendMessage('user', text);

      const payload = {
        message: text,
        jurisdiction: this.jurisdiction,
        context: this.currentContext,
        agentic: true,
      };

      try {
        let res = await fetch(`${this.apiUrl}/v1/chat/stream`, {
          method: 'POST',
          headers: await this.authHeaders(),
          body: JSON.stringify(payload),
        });
        if (!res) res = { ok: false, status: 404 };
        if (!res.ok) {
          if (res.status === 404 || res.status === 405) {
            return await this.sendFallbackChat(payload);
          }
          const data = await res.json().catch(() => null);
          this.appendPlain('assistant', `⚠️ ${describeHttpError(res.status, data)}`);
          return;
        }

        if (!res.body || typeof res.body.getReader !== 'function') {
          return await this.sendFallbackChat(payload);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let msgDiv = null;
        let bubble = null;
        const chatLogs = this.shadowRoot.querySelector('#chat-logs');

        const ensureMsgDiv = () => {
          if (!msgDiv) {
            msgDiv = document.createElement('div');
            msgDiv.className = 'msg assistant';
            chatLogs.appendChild(msgDiv);
          }
          return msgDiv;
        };

        const ensureBubble = () => {
          ensureMsgDiv();
          if (!bubble) {
            bubble = document.createElement('div');
            bubble.className = 'bubble';
            bubble.style.whiteSpace = 'pre-line';
            msgDiv.appendChild(bubble);
          }
          return bubble;
        };

        const handleEvents = (events) => {
          for (const item of events) {
            if (item.event === 'token') {
              const b = ensureBubble();
              b.textContent += String(item.data == null ? '' : item.data);
              chatLogs.scrollTop = chatLogs.scrollHeight;
            } else if (item.event === 'tool_call') {
              const m = ensureMsgDiv();
              const step = document.createElement('div');
              step.className = 'tool-step';
              const name = (item.data && typeof item.data === 'object' && item.data.name)
                ? item.data.name
                : String(item.data || '');
              step.textContent = `Using ${name}…`;
              m.appendChild(step);
              chatLogs.scrollTop = chatLogs.scrollHeight;
            } else if (item.event === 'partial') {
              const m = ensureMsgDiv();
              const notice = document.createElement('div');
              notice.className = 'notice';
              notice.textContent = 'Notice: The answer may be incomplete.';
              m.appendChild(notice);
              chatLogs.scrollTop = chatLogs.scrollHeight;
            } else if (item.event === 'error') {
              const m = ensureMsgDiv();
              const errBox = document.createElement('div');
              errBox.className = 'error-box';
              const code = (item.data && typeof item.data === 'object' && item.data.error)
                ? item.data.error
                : String(item.data || 'UNKNOWN_ERROR');
              errBox.textContent = `⚠️ Error: ${code}`;
              m.appendChild(errBox);
              chatLogs.scrollTop = chatLogs.scrollHeight;
              return true;
            } else if (item.event === 'result') {
              const m = ensureMsgDiv();
              const resData = item.data && typeof item.data === 'object' ? item.data : {};
              if (Array.isArray(resData.statutory_citations) && resData.statutory_citations.length > 0) {
                this.renderCitations(m, resData.statutory_citations);
              }
              if (Array.isArray(resData.suggested_actions) && resData.suggested_actions.length > 0) {
                this.renderActions(m, resData.suggested_actions);
              }
              chatLogs.scrollTop = chatLogs.scrollHeight;
            } else if (item.event === 'done') {
              // do NOT stop reading at done; continue until intent or end of stream
            } else if (item.event === 'intent') {
              return true;
            }
          }
          return false;
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSseFrames(buffer);
          buffer = parsed.remaining;
          if (handleEvents(parsed.events)) break;
        }

        buffer += decoder.decode();
        if (buffer) {
          const parsed = parseSseFrames(buffer + '\n\n');
          handleEvents(parsed.events);
        }
      } catch (err) {
        this.appendPlain('assistant', `⚠️ Could not reach AI sidecar: ${err.message}`);
      }
    }

    async sendFallbackChat(payload) {
      const res = await fetch(`${this.apiUrl}/v1/chat`, {
        method: 'POST',
        headers: await this.authHeaders(),
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        this.appendPlain('assistant', `⚠️ ${describeHttpError(res.status, data)}`);
        return;
      }
      this.appendMessage(
        'assistant', data.reply_text, data.suggested_actions, data.statutory_citations, data.thought_process
      );
    }

    formatMarkdown(text) {
      return formatMarkdown(text);
    }

    // Text we compose ourselves (errors, confirmations) is shown verbatim, never parsed as markdown (agy D-F3).
    appendPlain(sender, text, actions = []) {
      return this.appendMessage(sender, text, actions, [], [], { plain: true });
    }

    renderCitations(container, citations) {
      if (!citations || !citations.length) return;
      const box = document.createElement('div');
      box.className = 'citations';
      box.innerHTML = '<small>📚 Statutory Citations:</small><ul></ul>';
      const list = box.querySelector('ul');
      citations.forEach((c) => {
        const li = document.createElement('li');
        const small = document.createElement('small');
        small.textContent = String(c);
        li.appendChild(small);
        list.appendChild(li);
      });
      container.appendChild(box);
    }

    renderActions(container, actions) {
      if (!actions || !actions.length) return;
      const bar = document.createElement('div');
      bar.className = 'actions';
      actions.forEach((action) => {
        const btn = document.createElement('button');
        btn.className = 'action-btn';
        btn.textContent = action.label || `⚡ Action (${action.violation_id || action.name || 'Execute'})`;
        btn.addEventListener('click', () => this.handleAction(action));  // closure, not data in markup
        bar.appendChild(btn);
      });
      container.appendChild(bar);
    }

    appendMessage(sender, text, actions = [], citations = [], thoughts = [], options = {}) {
      const chatLogs = this.shadowRoot.querySelector('#chat-logs');
      const msgDiv = document.createElement('div');
      msgDiv.className = `msg ${sender}`;

      if (thoughts && thoughts.length > 0) {
        const thought = document.createElement('div');
        thought.className = 'thought-box';
        thought.innerHTML = `<small>💭 ${formatMarkdown(thoughts[thoughts.length - 1])}</small>`;
        msgDiv.appendChild(thought);
      }
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      if (options.plain) {
        bubble.style.whiteSpace = 'pre-line';
        bubble.textContent = String(text == null ? '' : text);
      } else {
        bubble.innerHTML = formatMarkdown(text);  // escaped before any markup is produced
      }
      msgDiv.appendChild(bubble);

      if (citations && citations.length > 0) {
        this.renderCitations(msgDiv, citations);
      }

      if (actions && actions.length > 0) {
        this.renderActions(msgDiv, actions);
      }

      chatLogs.appendChild(msgDiv);
      chatLogs.scrollTop = chatLogs.scrollHeight;
    }

    handleAction(action) {
      if (action.action_type === 'APPLY_ARCHETYPE' || action.archetype) {
        if (!this.hostOrigin) {
          this.appendPlain('assistant', '⚠️ Cannot hand the archetype to the host: data-host-origin is not set.');
          return;
        }
        window.postMessage({ type: 'HRMS_APPLY_ARCHETYPE', archetype: action.archetype || action }, this.hostOrigin);
        this.appendPlain('assistant', `⚡ Action dispatched: auto-filling the requisition form with ${action.name || action.badge || 'archetype'}.`);
        return;
      }
      this.confirmRemediation(action);
    }

    confirmRemediation(action) {
      const proposal = buildProposalBody(action);
      if (!proposal) {
        this.appendPlain('assistant', '⚠️ This suggestion does not identify a record this assistant may change, so it cannot be proposed.');
        return;
      }
      const confirm = { label: '📝 Propose this change', name: 'confirm' };
      this.appendPlain('assistant', confirmationText(proposal), [confirm]);
      const buttons = this.shadowRoot.querySelectorAll('.action-btn');
      const confirmBtn = buttons[buttons.length - 1];
      const fresh = confirmBtn.cloneNode(true);
      confirmBtn.replaceWith(fresh);
      fresh.addEventListener('click', () => this.proposeRemediation(proposal), { once: true });
    }

    async proposeRemediation(proposal) {
      try {
        const res = await fetch(`${this.apiUrl}/v1/writeback/proposals`, {
          method: 'POST',
          headers: await this.authHeaders(),
          body: JSON.stringify(proposal),
        });
        const result = await res.json().catch(() => null);
        if (!res.ok) {
          this.appendPlain('assistant', `❌ Not proposed: ${describeHttpError(res.status, result)}`);
          return;
        }
        this.appendPlain('assistant', `📝 Proposal ${result.id} created (${result.status}). A different approver must approve it in the HRMS before anything is written.`);
      } catch (err) {
        this.appendPlain('assistant', `❌ Proposal error: ${err.message}`);
      }
    }

    render() {
      this.shadowRoot.innerHTML = `
        <style>
          :host {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 99999;
          }
          #launcher {
            width: 56px;
            height: 56px;
            border-radius: 28px;
            background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
            box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            border: none;
          }
          #launcher:hover {
            transform: scale(1.05);
            box-shadow: 0 6px 20px rgba(79, 70, 229, 0.5);
          }
          #copilot-drawer {
            display: none;
            position: fixed;
            bottom: 90px;
            right: 24px;
            width: 400px;
            height: 560px;
            background: #ffffff;
            border-radius: 16px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
            border: 1px solid #e2e8f0;
            flex-direction: column;
            overflow: hidden;
          }
          #copilot-drawer.open {
            display: flex;
          }
          .header {
            background: #4f46e5;
            color: white;
            padding: 14px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
          }
          .header h4 {
            margin: 0;
            font-size: 15px;
            font-weight: 600;
          }
          #context-badge {
            display: none;
            background: #3730a3;
            color: #c7d2fe;
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 10px;
          }
          #chat-logs {
            flex: 1;
            padding: 14px;
            overflow-y: auto;
            background: #f8fafc;
            display: flex;
            flex-direction: column;
            gap: 12px;
          }
          .msg {
            display: flex;
            flex-direction: column;
            max-width: 90%;
          }
          .msg.user {
            align-self: flex-end;
          }
          .msg.user .bubble {
            background: #4f46e5;
            color: white;
            border-radius: 14px 14px 2px 14px;
          }
          .msg.assistant {
            align-self: flex-start;
          }
          .msg.assistant .bubble {
            background: #ffffff;
            color: #1e293b;
            border: 1px solid #e2e8f0;
            border-radius: 14px 14px 14px 2px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
          }
          .bubble {
            padding: 10px 14px;
            font-size: 13px;
            line-height: 1.5;
            word-break: break-word;
            overflow-wrap: break-word;
          }
          .bubble p.md-p {
            margin: 0 0 8px 0;
          }
          .bubble p.md-p:last-child {
            margin-bottom: 0;
          }
          .bubble ul.md-ul, .bubble ol.md-ol {
            margin: 6px 0 8px 0;
            padding-left: 20px;
          }
          .bubble li {
            margin-bottom: 4px;
            line-height: 1.45;
          }
          .bubble li:last-child {
            margin-bottom: 0;
          }
          .bubble strong {
            font-weight: 600;
          }
          .bubble em {
            font-style: italic;
          }
          .bubble h2.md-h2, .bubble h3.md-h3, .bubble h4.md-h4 {
            margin: 10px 0 6px 0;
            font-weight: 600;
            line-height: 1.3;
          }
          .bubble h2.md-h2 { font-size: 15px; }
          .bubble h3.md-h3 { font-size: 14px; }
          .bubble h4.md-h4 { font-size: 13px; }
          .bubble blockquote.md-quote {
            border-left: 3px solid #6366f1;
            margin: 6px 0;
            padding: 4px 8px;
            background: rgba(99, 102, 241, 0.08);
            border-radius: 0 4px 4px 0;
            font-size: 12px;
            color: #4338ca;
          }
          .bubble code.inline-code {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            padding: 1px 4px;
            border-radius: 4px;
          }
          .msg.assistant .bubble code.inline-code {
            background: #f1f5f9;
            color: #0f172a;
            border: 1px solid #e2e8f0;
          }
          .msg.user .bubble code.inline-code {
            background: rgba(255, 255, 255, 0.25);
            color: #ffffff;
          }
          .bubble pre.code-block {
            background: #0f172a;
            color: #f8fafc;
            padding: 10px 12px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 8px 0;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            line-height: 1.4;
          }
          .bubble pre.code-block code {
            background: transparent;
            padding: 0;
            border: none;
            color: inherit;
          }
          .bubble a.md-link {
            color: #4f46e5;
            text-decoration: underline;
          }
          .msg.user .bubble a.md-link {
            color: #e0e7ff;
          }
          .citations {
            margin-top: 4px;
            background: #f1f5f9;
            padding: 6px 8px;
            border-radius: 6px;
            font-size: 11px;
            color: #475569;
          }
          .citations ul {
            margin: 4px 0 0 0;
            padding-left: 14px;
          }
          .actions {
            margin-top: 6px;
          }
          .action-btn {
            background: #10b981;
            color: white;
            border: none;
            padding: 6px 10px;
            font-size: 11px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
          }
          .thought-box {
            background: #ede9fe;
            border-left: 3px solid #8b5cf6;
            padding: 4px 8px;
            margin-bottom: 4px;
            border-radius: 4px;
            font-size: 11px;
            color: #5b21b6;
            font-style: italic;
          }
          .tool-step {
            font-size: 11px;
            color: #64748b;
            margin-bottom: 4px;
            font-style: italic;
          }
          .notice {
            background: #fef3c7;
            border-left: 3px solid #f59e0b;
            padding: 4px 8px;
            margin-top: 4px;
            border-radius: 4px;
            font-size: 11px;
            color: #92400e;
          }
          .error-box {
            background: #fee2e2;
            border-left: 3px solid #ef4444;
            padding: 4px 8px;
            margin-top: 4px;
            border-radius: 4px;
            font-size: 11px;
            color: #b91c1c;
          }
          .action-btn:hover {
            background: #059669;
          }
          .input-box {
            padding: 10px;
            background: #ffffff;
            border-top: 1px solid #e2e8f0;
            display: flex;
            gap: 8px;
          }
          .input-box input {
            flex: 1;
            padding: 8px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            font-size: 13px;
            outline: none;
          }
          #mic-btn {
            background: #f1f5f9;
            color: #475569;
            border: 1px solid #cbd5e1;
            padding: 0 10px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
          }
          #mic-btn:hover {
            background: #e2e8f0;
            color: #1e293b;
          }
          #mic-btn.recording {
            background: #ef4444;
            color: white;
            border-color: #dc2626;
            animation: pulse-ring 1.5s infinite;
          }
          @keyframes pulse-ring {
            0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.5); }
            70% { box-shadow: 0 0 0 8px rgba(239, 68, 68, 0); }
            100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
          }
          .input-box button#send-btn {
            background: #4f46e5;
            color: white;
            border: none;
            padding: 0 14px;
            border-radius: 8px;
            cursor: pointer;
          }
        </style>

        <button id="launcher" aria-label="Open HR Copilot">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
          </svg>
        </button>

        <div id="copilot-drawer">
          <div class="header">
            <h4>HR Intelligence Copilot</h4>
            <span id="context-badge"></span>
          </div>
          <div id="chat-logs">
            <div class="msg assistant">
              <div class="bubble">
                <p class="md-p">Hello! I am your <strong>Enterprise HR Copilot</strong>.</p>
                <p class="md-p">Ask me to compute <strong>statutory gratuities</strong>, audit <strong>job requisitions for bias</strong>, evaluate <strong>leave policies</strong>, or reconcile <strong>payroll</strong>.</p>
              </div>
            </div>
          </div>
          <div class="input-box">
            <input type="text" id="chat-input" placeholder="Type or speak a message..." />
            <button id="mic-btn" title="Voice AI (Sarvam / Whisper)">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
            </button>
            <button id="send-btn">Send</button>
          </div>
        </div>
      `;

      this.shadowRoot.querySelector('#launcher').addEventListener('click', () => this.toggleDrawer());
      this.shadowRoot.querySelector('#mic-btn').addEventListener('click', () => this.toggleVoiceRecording());
      this.shadowRoot.querySelector('#send-btn').addEventListener('click', () => this.sendMessage());
      this.shadowRoot.querySelector('#chat-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') this.sendMessage();
      });
    }
  }

  if (!customElements.get('hrms-copilot')) {
    customElements.define('hrms-copilot', HrmsCopilotElement);
  }
})();
