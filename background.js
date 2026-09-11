chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'zb-api-call') {
    const { url, cookies } = msg;
    const headers = {};
    if (cookies) headers['Cookie'] = cookies;

    fetch(url, {
      method: 'GET',
      headers,
      credentials: 'include',
    })
    .then(async (resp) => {
      const text = await resp.text();
      sendResponse({ ok: true, status: resp.status, text });
    })
    .catch((e) => {
      sendResponse({ ok: false, error: e.message });
    });
    return true;
  }

  if (msg.action === 'startBackup') {
    chrome.storage.local.set({ backupState: { running: true, status: 'starting' } });
    sendResponse({ ok: true });
  }

  if (msg.action === 'backupComplete') {
    chrome.storage.local.set({ backupState: { running: false, status: 'complete' } });
    sendResponse({ ok: true });
  }

  if (msg.action === 'backupError') {
    chrome.storage.local.set({ backupState: { running: false, status: 'error', error: msg.error } });
    sendResponse({ ok: true });
  }

  return true;
});
