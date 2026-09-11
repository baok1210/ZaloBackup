let logs = [];
let autoRefresh = null;

function addLog(msg, type = 'info') {
  const colors = { info: '#888', success: '#4caf50', warn: '#ff9800', error: '#f44336' };
  const entry = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logs.push({ text: entry, type });
  if (logs.length > 100) logs = logs.slice(-100);

  const logEl = document.getElementById('logContainer');
  if (logEl) {
    const line = document.createElement('div');
    line.className = `log-entry ${type}`;
    line.textContent = entry;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function refreshStatus() {
  chrome.runtime.sendMessage({ action: 'status' }, (response) => {
    if (chrome.runtime.lastError) return;
    if (!response) return;

    const { backupState, logs: bgLogs } = response;
    const statusEl = document.getElementById('status');
    const progressBar = document.getElementById('progressBar');
    const progressFill = document.getElementById('progressFill');

    if (backupState) {
      if (backupState.running) {
        statusEl.textContent = `Dang chay... ${backupState.progress}/${backupState.total}`;
        progressBar.style.display = 'block';
        progressFill.style.width = `${(backupState.progress / backupState.total) * 100}%`;
      } else if (backupState.status === 'complete') {
        statusEl.textContent = `Hoan thanh! Tong: ${backupState.progress} tin nhan.`;
        progressBar.style.display = 'block';
        progressFill.style.width = '100%';
      } else if (backupState.status === 'error') {
        statusEl.textContent = `Loi: ${backupState.error || 'Unknown'}`;
      }
    }

    if (bgLogs && bgLogs.length > 0) {
      const logEl = document.getElementById('logContainer');
      logEl.innerHTML = '';
      bgLogs.forEach(log => {
        const line = document.createElement('div');
        line.className = `log-entry ${log.type || 'info'}`;
        line.textContent = log.text;
        logEl.appendChild(line);
      });
      logEl.scrollTop = logEl.scrollHeight;
    }
  });
}

document.getElementById('startBtn').addEventListener('click', async () => {
  const btn = document.getElementById('startBtn');
  const status = document.getElementById('status');

  const userId = document.getElementById('userId').value.trim();
  const maxMsg = parseInt(document.getElementById('maxMsg').value) || 10000;

  btn.disabled = true;
  status.textContent = 'Dang ket noi...';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    if (!tab || !tab.url?.includes('chat.zalo.me')) {
      status.textContent = 'Hay mo chat.zalo.me truoc!';
      btn.disabled = false;
      return;
    }

    chrome.tabs.sendMessage(tab.id, {
      action: 'startBackup',
      userId: userId,
      maxMsg: maxMsg
    }, (response) => {
      if (chrome.runtime.lastError) {
        status.textContent = 'Loi: khong ket noi duoc vao page. Refresh page thu lai.';
        addLog('Cannot connect to content script: ' + chrome.runtime.lastError.message, 'error');
      } else {
        status.textContent = response?.ok ? 'Da bat dau! Xem tren page.' : 'Loi gui message.';
      }
      btn.disabled = false;
    });
  } catch (e) {
    status.textContent = 'Loi: ' + e.message;
    btn.disabled = false;
  }
});

document.getElementById('toggleLog').addEventListener('click', () => {
  const logEl = document.getElementById('logContainer');
  const btn = document.getElementById('toggleLog');
  if (logEl.style.display === 'none') {
    logEl.style.display = 'block';
    btn.textContent = 'Hide log';
    refreshStatus();
  } else {
    logEl.style.display = 'none';
    btn.textContent = 'Show log';
  }
});

document.getElementById('copyLog').addEventListener('click', () => {
  const logText = logs.map(l => l.text).join('\n');
  navigator.clipboard.writeText(logText).then(() => {
    const btn = document.getElementById('copyLog');
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
});

document.getElementById('clearLog').addEventListener('click', () => {
  logs = [];
  const logEl = document.getElementById('logContainer');
  logEl.innerHTML = '';
});

autoRefresh = setInterval(refreshStatus, 2000);
refreshStatus();
