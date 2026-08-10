const messagesEl = document.getElementById('messages');
const form = document.getElementById('composerForm');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const newChatBtn = document.getElementById('newChatBtn');
const backendNameEl = document.getElementById('backendName');
const backendSelect = document.getElementById('backendSelect');
const statusDotEl = document.getElementById('statusDot');
const menuBtn = document.getElementById('menuBtn');
const sidebar = document.querySelector('.sidebar');
const sidebarBackdrop = document.getElementById('sidebarBackdrop');

let sessionId = null;

function setSidebarOpen(open) {
  sidebar.classList.toggle('open', open);
  sidebarBackdrop.classList.toggle('visible', open);
  menuBtn.setAttribute('aria-expanded', String(open));
}

async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    backendNameEl.textContent = data.backend;
    if (data.status !== 'ok') {
      statusDotEl.classList.add('error');
      statusDotEl.title = 'Backend not configured - check API key / config.yaml';
    }

  } catch (e) {
    backendNameEl.textContent = 'offline';
    statusDotEl.classList.add('error');
  }

}

async function loadBackends() {
  try {
    const res = await fetch('/api/backends');
    const data = await res.json();
    backendSelect.replaceChildren();
    data.backends.forEach((backend) => {
      const option = document.createElement('option');
      option.value = backend.name;
      option.textContent = backend.configured ? backend.name : `${backend.name} (not configured)`;
      option.disabled = !backend.configured;
      option.selected = backend.active;
      backendSelect.appendChild(option);
    });
    backendNameEl.textContent = backendSelect.value || '—';
  } catch (e) {
    backendNameEl.textContent = 'unavailable';
  }
}

function clearEmptyState() {
  const empty = messagesEl.querySelector('.empty-state');
  if (empty) empty.remove();
}

function addMessage(role, text) {
  clearEmptyState();
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const roleEl = document.createElement('div');
  roleEl.className = 'msg-role';
  roleEl.textContent = role === 'user' ? 'you' : 'mm';
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = text;
  wrap.appendChild(roleEl);
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function addToolChips(container, toolCalls) {
  if (!toolCalls || toolCalls.length === 0) return;
  const chipWrap = document.createElement('div');
  toolCalls.forEach(tc => {
    const chip = document.createElement('div');
    chip.className = 'tool-chip';
    chip.textContent = `${tc.name}(${JSON.stringify(tc.input)})`;
    chipWrap.appendChild(chip);
  });
  container.insertBefore(chipWrap, container.querySelector('.msg-bubble'));
}

function addThinking() {
  clearEmptyState();
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.id = 'thinkingMsg';
  wrap.innerHTML = `<div class="msg-role">mm</div><div class="thinking"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></div>`;
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeThinking() {
  const el = document.getElementById('thinkingMsg');
  if (el) el.remove();
}

async function sendMessage(text) {
  addMessage('user', text);
  addThinking();
  sendBtn.disabled = true;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: sessionId, backend: backendSelect.value }),
    });
    removeThinking();

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Request failed' }));
      addMessage('assistant', `Error: ${err.detail || res.statusText}`);
      return;
    }

    const data = await res.json();
    sessionId = data.session_id;
    const bubble = addMessage('assistant', data.reply || '(no response)');
    addToolChips(bubble, data.tool_calls);
  } catch (e) {
    removeThinking();
    addMessage('assistant', `Connection error: ${e.message}`);
  } finally {
    sendBtn.disabled = false;
  }
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = 'auto';
  sendMessage(text);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
});

backendSelect.addEventListener('change', () => {
  backendNameEl.textContent = backendSelect.value;
});

newChatBtn.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/new_session', { method: 'POST' });
    if (!res.ok) throw new Error('Unable to create a session');
    const data = await res.json();
    sessionId = data.session_id;
    messagesEl.innerHTML = `<div class="empty-state"><span class="empty-caret">&gt;</span><p>New session started.</p></div>`;
    setSidebarOpen(false);
  } catch (e) {
    addMessage('assistant', `Error: ${e.message}`);
  }
});

menuBtn.addEventListener('click', () => setSidebarOpen(!sidebar.classList.contains('open')));
sidebarBackdrop.addEventListener('click', () => setSidebarOpen(false));

checkHealth();
loadBackends();
