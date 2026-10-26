/**
 * Enterprise Agentic HRMS Copilot Embeddable SDK (<hrms-copilot>)
 * Version: 2.1.0
 * Features: Shadow DOM isolation, postMessage Host Context Sync, Interactive Diff Remediation Modal, Voice AI, Rich Markdown Formatting
 */

(function () {
  'use strict';

  class HrmsCopilotElement extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.tenantId = this.getAttribute('data-tenant') || 'DEFAULT';
      this.apiUrl = this.getAttribute('data-api-url') || 'http://localhost:8000';
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
        if (!event.data || typeof event.data !== 'object') return;
        if (event.data.type === 'HRMS_NAVIGATE') {
          this.currentContext = {
            entity: event.data.entity,
            recordId: event.data.record_id,
            userRole: event.data.user_role || 'EMPLOYEE',
          };
          this.updateContextBadge();
        }
      });
    }

    updateContextBadge() {
      const badge = this.shadowRoot.querySelector('#context-badge');
      if (badge && this.currentContext.entity) {
        badge.textContent = `Context: ${this.currentContext.entity} #${this.currentContext.recordId}`;
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

    async sendVoiceAudio(base64Audio) {
      this.appendMessage('user', '🎙️ [Spoken Audio Message]');
      try {
        const res = await fetch(`${this.apiUrl}/v1/voice/turn`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': this.tenantId,
          },
          body: JSON.stringify({
            audio_base64: base64Audio,
            user_id: 'USR-COPILOT',
            jurisdiction: 'IN',
          }),
        });
        const data = await res.json();
        this.appendMessage('assistant', data.reply_text, data.suggested_actions, data.statutory_citations);
        if (data.reply_audio_base64) {
          const audio = new Audio('data:audio/wav;base64,' + data.reply_audio_base64);
          audio.play().catch(() => {});
        }
      } catch (err) {
        this.appendMessage('assistant', `⚠️ Voice processing error: ${err.message}`);
      }
    }

    async sendMessage() {
      const input = this.shadowRoot.querySelector('#chat-input');
      const text = (input.value || '').trim();
      if (!text) return;

      input.value = '';
      this.appendMessage('user', text);

      try {
        const res = await fetch(`${this.apiUrl}/v1/chat`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': this.tenantId,
          },
          body: JSON.stringify({
            message: text,
            context: this.currentContext,
            agentic: true,
          }),
        });

        const data = await res.json();
        this.appendMessage('assistant', data.reply_text, data.suggested_actions, data.statutory_citations, data.thought_process);
      } catch (err) {
        this.appendMessage('assistant', `⚠️ Could not reach AI sidecar: ${err.message}`);
      }
    }

    formatMarkdown(text) {
      if (!text) return '';
      let str = String(text).replace(/\r\n/g, '\n');

      // 1. Extract and preserve code blocks
      const codeBlocks = [];
      str = str.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
        const idx = codeBlocks.length;
        const safeCode = code
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
        codeBlocks.push('<pre class="code-block"><code class="lang-' + (lang || 'text') + '">' + safeCode.trim() + '</code></pre>');
        return '⟦CB' + idx + '⟧';
      });

      // 2. Extract and preserve inline code
      const inlineCodes = [];
      str = str.replace(/`([^`\n]+)`/g, function (_, code) {
        const idx = inlineCodes.length;
        const safeCode = code
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
        inlineCodes.push('<code class="inline-code">' + safeCode + '</code>');
        return '⟦IC' + idx + '⟧';
      });

      // 3. Process Blockquotes before escaping >
      str = str.replace(/^>\s+(.*$)/gim, '<blockquote class="md-quote">$1</blockquote>');

      // 4. Escape general HTML (preserving deliberate blockquote tags)
      str = str
        .replace(/&(?!amp;|lt;|gt;|quot;|#39;)/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

      str = str.replace(/&lt;blockquote class="md-quote"&gt;(.*?)&lt;\/blockquote&gt;/gi, '<blockquote class="md-quote">$1</blockquote>');

      // 5. Headers
      str = str.replace(/^###\s+(.*$)/gim, '<h4 class="md-h4">$1</h4>');
      str = str.replace(/^##\s+(.*$)/gim, '<h3 class="md-h3">$1</h3>');
      str = str.replace(/^#\s+(.*$)/gim, '<h2 class="md-h2">$1</h2>');

      // 6. Links [text](url)
      str = str.replace(/\[([^\]]+)\]\((https?:\/\/[^\s\)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');

      // 7. Bold & Italics
      str = str.replace(/\*\*\*(.*?)\*\*\*/g, '<strong><em>$1</em></strong>');
      str = str.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
      str = str.replace(/__(.*?)__/g, '<strong>$1</strong>');
      str = str.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
      str = str.replace(/_([^_\n]+)_/g, '<em>$1</em>');

      // 8. Lists (Bullet & Ordered)
      const rawLines = str.split('\n');
      const groupedLines = [];
      let inUl = false;
      let inOl = false;

      for (let i = 0; i < rawLines.length; i++) {
        const line = rawLines[i];
        const bulletMatch = line.match(/^[\*\-\+]\s+(.*)/);
        const orderedMatch = line.match(/^\d+\.\s+(.*)/);

        if (bulletMatch) {
          if (inOl) {
            groupedLines.push('</ol>');
            inOl = false;
          }
          if (!inUl) {
            groupedLines.push('\n<ul class="md-ul">');
            inUl = true;
          }
          groupedLines.push('<li>' + bulletMatch[1] + '</li>');
        } else if (orderedMatch) {
          if (inUl) {
            groupedLines.push('</ul>');
            inUl = false;
          }
          if (!inOl) {
            groupedLines.push('\n<ol class="md-ol">');
            inOl = true;
          }
          groupedLines.push('<li>' + orderedMatch[1] + '</li>');
        } else {
          if (inUl) {
            groupedLines.push('</ul>\n');
            inUl = false;
          }
          if (inOl) {
            groupedLines.push('</ol>\n');
            inOl = false;
          }
          groupedLines.push(line);
        }
      }
      if (inUl) groupedLines.push('</ul>\n');
      if (inOl) groupedLines.push('</ol>\n');

      str = groupedLines.join('\n');

      // 9. Paragraphs
      const chunks = str.split(/\n\n+/);
      const formattedChunks = chunks.map(function (chunk) {
        chunk = chunk.trim();
        if (!chunk) return '';
        if (
          chunk.startsWith('<h2') ||
          chunk.startsWith('<h3') ||
          chunk.startsWith('<h4') ||
          chunk.startsWith('<ul') ||
          chunk.startsWith('<ol') ||
          chunk.startsWith('<blockquote') ||
          chunk.startsWith('⟦CB')
        ) {
          return chunk;
        }
        return '<p class="md-p">' + chunk.replace(/\n/g, '<br/>') + '</p>';
      });

      str = formattedChunks.filter(Boolean).join('\n');

      // 10. Restore code blocks and inline code
      str = str.replace(/⟦CB(\d+)⟧/g, function (_, idx) {
        return codeBlocks[parseInt(idx, 10)] || '';
      });
      str = str.replace(/⟦IC(\d+)⟧/g, function (_, idx) {
        return inlineCodes[parseInt(idx, 10)] || '';
      });

      return str;
    }

    appendMessage(sender, text, actions = [], citations = [], thoughts = []) {
      const chatLogs = this.shadowRoot.querySelector('#chat-logs');
      const msgDiv = document.createElement('div');
      msgDiv.className = `msg ${sender}`;
      
      let html = '';
      if (thoughts && thoughts.length > 0) {
        html += `<div class="thought-box"><small>💭 ${this.formatMarkdown(thoughts[thoughts.length - 1])}</small></div>`;
      }
      html += `<div class="bubble">${this.formatMarkdown(text)}</div>`;

      if (citations && citations.length > 0) {
        html += `<div class="citations"><small>📚 Statutory Citations:</small><ul>`;
        citations.forEach((c) => {
          html += `<li><small>${c}</small></li>`;
        });
        html += `</ul></div>`;
      }

      if (actions && actions.length > 0) {
        html += `<div class="actions">`;
        actions.forEach((a) => {
          const btnLabel = a.label || `⚡ Action (${a.violation_id || a.name || 'Execute'})`;
          html += `<button class="action-btn" data-action='${JSON.stringify(a).replace(/'/g, "&apos;")}'>${btnLabel}</button>`;
        });
        html += `</div>`;
      }

      msgDiv.innerHTML = html;
      chatLogs.appendChild(msgDiv);
      chatLogs.scrollTop = chatLogs.scrollHeight;

      // Bind action buttons
      msgDiv.querySelectorAll('.action-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          const actionData = JSON.parse(e.currentTarget.getAttribute('data-action'));
          if (actionData.action_type === 'APPLY_ARCHETYPE' || actionData.archetype) {
            window.postMessage({
              type: 'HRMS_APPLY_ARCHETYPE',
              archetype: actionData.archetype || actionData
            }, '*');
            this.appendMessage('assistant', `⚡ **Autonomous Action Dispatched**: Auto-filling host form with **${actionData.name || actionData.badge || 'Archetype'}** and opening Requisition view...`);
          } else {
            this.executeRemediation(actionData);
          }
        });
      });
    }

    async executeRemediation(actionData) {
      try {
        const res = await fetch(`${this.apiUrl}/v1/remediate/execute`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': this.tenantId,
          },
          body: JSON.stringify({
            violation_id: actionData.violation_id,
            target_entity: 'EMPLOYEE',
            entity_id: 'EMP-AUTO',
            patch_payload: actionData.patch || {},
            approver_id: 'USR-ADMIN',
            approver_role: 'HR_ADMIN',
          }),
        });
        const result = await res.json();
        this.appendMessage('assistant', `✅ Remediation approved & applied with audit trace: ${JSON.stringify(result.audit_tracing)}`);
      } catch (err) {
        this.appendMessage('assistant', `❌ Remediation error: ${err.message}`);
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
