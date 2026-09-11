(() => {
  if (window.__zaloPageScriptActive) return;
  window.__zaloPageScriptActive = true;

  const captured = [];
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function(method, url) {
    this._zbUrl = url;
    this._zbMethod = method;
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function(body) {
    const self = this;
    const entry = { url: self._zbUrl, method: self._zbMethod, time: Date.now() };
    if (body) entry.body = String(body).substring(0, 500);
    captured.push(entry);
    self.addEventListener('load', function() {
      entry.status = self.status;
      try { entry.response = self.responseText.substring(0, 2000); } catch {}
    });
    return origSend.apply(this, arguments);
  };

  const origFetch = window.fetch;
  window.fetch = function() {
    const url = typeof arguments[0] === 'string' ? arguments[0] : arguments[0]?.url || '';
    const entry = { url, method: arguments[1]?.method || 'GET', time: Date.now(), type: 'fetch' };
    captured.push(entry);
    return origFetch.apply(this, arguments).then(resp => {
      entry.status = resp.status;
      const clone = resp.clone();
      clone.text().then(t => { entry.response = t.substring(0, 2000); }).catch(() => {});
      return resp;
    });
  };

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.source !== 'zalo-backup-content') return;

    if (msg.type === 'zb-get-captured') {
      const filtered = captured.filter(c =>
        c.url && c.url.includes('params=') && !c.url.includes('qos.talk')
      );
      window.postMessage({ source: 'zalo-backup-page', type: 'zb-captured-list', calls: filtered }, '*');
    }
  });

  window.postMessage({ source: 'zalo-backup-page', type: 'zb-page-ready' }, '*');
})();
