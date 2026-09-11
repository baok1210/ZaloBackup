(() => {
  if (window.__zaloBackupLoaded) return;
  window.__zaloBackupLoaded = true;

  let logs = [];
  let allMessages = new Map();

  function addLog(msg, type = 'info') {
    const colors = { info: '#888', success: '#4caf50', warn: '#ff9800', error: '#f44336', data: '#00bcd4' };
    const entry = '[' + new Date().toLocaleTimeString() + '] ' + msg;
    logs.push({ text: entry, type });
    if (logs.length > 2000) logs = logs.slice(-2000);
    const logEl = document.getElementById('zb-log');
    if (logEl) {
      const line = document.createElement('div');
      line.style.color = colors[type] || '#888';
      line.style.whiteSpace = 'pre-wrap';
      line.style.wordBreak = 'break-all';
      line.textContent = entry;
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    }
    console.log('[ZaloBackup] ' + entry);
  }

  // ===== DEBUG: FIND ALL SCROLLABLE ELEMENTS =====
  function debugScrollables() {
    addLog('=== Debug: All Scrollable Elements ===', 'info');
    const scrollables = [];
    for (const d of document.querySelectorAll('div')) {
      const style = getComputedStyle(d);
      if (style.overflow === 'auto' || style.overflow === 'scroll' ||
          style.overflowY === 'auto' || style.overflowY === 'scroll') {
        if (d.scrollHeight > d.clientHeight + 50) {
          const msgCount = d.querySelectorAll('[class*="message-frame"]').length;
          scrollables.push({
            el: d,
            id: d.id,
            className: (d.className || '').substring(0, 80),
            scrollHeight: d.scrollHeight,
            clientHeight: d.clientHeight,
            scrollTop: d.scrollTop,
            msgCount,
            children: d.children.length,
          });
          addLog('Scrollable: id=' + d.id + ' class=' + (d.className || '').substring(0, 60) +
                 ' scrollH=' + d.scrollHeight + ' clientH=' + d.clientHeight +
                 ' msgs=' + msgCount + ' children=' + d.children.length, 'data');
        }
      }
    }

    // Also check message-frame parents
    addLog('\n=== Message Frame Parents ===', 'info');
    const firstFrame = document.querySelector('[class*="message-frame"]');
    if (firstFrame) {
      let el = firstFrame;
      for (let i = 0; i < 15 && el; i++) {
        const style = getComputedStyle(el);
        addLog('Parent ' + i + ': tag=' + el.tagName + ' id=' + el.id +
               ' class=' + (el.className || '').substring(0, 60) +
               ' scroll=' + style.overflow + '/' + style.overflowY +
               ' scrollH=' + el.scrollHeight + ' clientH=' + el.clientHeight, 'data');
        el = el.parentElement;
      }
    }

    return scrollables;
  }

  // ===== TRY ALL SCROLL METHODS =====
  async function tryAllScrollMethods() {
    addLog('\n=== Testing All Scroll Methods ===', 'info');

    // Find scrollable containers
    const scrollables = [];
    for (const d of document.querySelectorAll('div')) {
      const style = getComputedStyle(d);
      if ((style.overflow === 'auto' || style.overflow === 'scroll' ||
           style.overflowY === 'auto' || style.overflowY === 'scroll') &&
          d.scrollHeight > d.clientHeight + 100) {
        scrollables.push(d);
      }
    }

    if (scrollables.length === 0) {
      addLog('No scrollable containers found!', 'error');
      return;
    }

    addLog('Found ' + scrollables.length + ' scrollable containers', 'info');

    for (let i = 0; i < scrollables.length; i++) {
      const container = scrollables[i];
      const before = container.querySelectorAll('[class*="message-frame"]').length;
      const beforeScroll = container.scrollTop;

      addLog('\n--- Container ' + i + ': class=' + (container.className || '').substring(0, 40) + ' ---', 'info');
      addLog('Before: scrollTop=' + beforeScroll + ' msgs=' + before, 'data');

      // Method 1: scrollTop
      container.scrollTop = 0;
      await new Promise(r => setTimeout(r, 1000));
      let after = container.querySelectorAll('[class*="message-frame"]').length;
      addLog('After scrollTop=0: scrollTop=' + container.scrollTop + ' msgs=' + after + (after > before ? ' NEW!' : ''), after > before ? 'success' : 'data');

      // Reset
      container.scrollTop = beforeScroll;
      await new Promise(r => setTimeout(r, 500));

      // Method 2: wheel event
      container.dispatchEvent(new WheelEvent('wheel', { deltaY: -500, bubbles: true }));
      await new Promise(r => setTimeout(r, 1000));
      after = container.querySelectorAll('[class*="message-frame"]').length;
      addLog('After wheel(-500): scrollTop=' + container.scrollTop + ' msgs=' + after + (after > before ? ' NEW!' : ''), after > before ? 'success' : 'data');

      // Reset
      container.scrollTop = beforeScroll;
      await new Promise(r => setTimeout(r, 500));

      // Method 3: scroll event
      container.dispatchEvent(new Event('scroll', { bubbles: true }));
      await new Promise(r => setTimeout(r, 1000));
      after = container.querySelectorAll('[class*="message-frame"]').length;
      addLog('After scroll event: msgs=' + after + (after > before ? ' NEW!' : ''), after > before ? 'success' : 'data');

      // Reset
      container.scrollTop = beforeScroll;
      await new Promise(r => setTimeout(r, 500));

      // Method 4: keyboard PageUp
      container.focus();
      container.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', keyCode: 33, bubbles: true }));
      await new Promise(r => setTimeout(r, 1000));
      after = container.querySelectorAll('[class*="message-frame"]').length;
      addLog('After PageUp key: scrollTop=' + container.scrollTop + ' msgs=' + after + (after > before ? ' NEW!' : ''), after > before ? 'success' : 'data');

      // Reset
      container.scrollTop = beforeScroll;
      await new Promise(r => setTimeout(r, 500));
    }
  }

  // ===== CAPTURE =====
  function captureViewport() {
    const frames = document.querySelectorAll('[class*="message-frame"]');
    let added = 0;
    for (const frame of frames) {
      const isOwn = / me /.test(' ' + frame.className + ' ');
      const rawText = (frame.innerText || '').trim();
      if (!rawText) continue;
      let text = rawText
        .replace(/\s+\/-strong\s+\/-heart\s+:>\s+:o\s+:-\(\(\s+:-h\s*$/g, '')
        .replace(/\s+\d{2}:\d{2}\s*$/, '')
        .trim();
      if (!text) continue;
      const key = (isOwn ? 'ME:' : 'THEM:') + text;
      if (!allMessages.has(key)) {
        allMessages.set(key, { side: isOwn ? 'ME' : 'THEM', text });
        added++;
      }
    }
    const el = document.getElementById('zb-count');
    if (el) el.textContent = 'Messages: ' + allMessages.size;
    return added;
  }

  // ===== AUTO SCROLL WITH BEST METHOD =====
  async function autoScroll() {
    addLog('\n=== Auto Scroll (finding best method) ===', 'info');

    // Find the container with most message-frames
    let bestContainer = null;
    let bestCount = 0;
    for (const d of document.querySelectorAll('div')) {
      const style = getComputedStyle(d);
      if ((style.overflow === 'auto' || style.overflow === 'scroll' ||
           style.overflowY === 'auto' || style.overflowY === 'scroll') &&
          d.scrollHeight > d.clientHeight + 100) {
        const count = d.querySelectorAll('[class*="message-frame"]').length;
        if (count > bestCount) {
          bestCount = count;
          bestContainer = d;
        }
      }
    }

    if (!bestContainer) {
      addLog('No container with messages found!', 'error');
      return;
    }

    addLog('Best container: msgs=' + bestCount + ' class=' + (bestContainer.className || '').substring(0, 60), 'success');

    const initialScrollTop = bestContainer.scrollTop;
    let lastCount = 0;
    let stuck = 0;

    for (let round = 0; round < 500; round++) {
      // Try multiple scroll methods
      bestContainer.scrollTop = 0;
      bestContainer.dispatchEvent(new WheelEvent('wheel', { deltaY: -500, bubbles: true }));
      bestContainer.dispatchEvent(new Event('scroll', { bubbles: true }));

      await new Promise(r => setTimeout(r, 800));

      captureViewport();

      if (round % 10 === 0) {
        addLog('Round ' + round + ': ' + allMessages.size + ' msgs | scrollTop=' + bestContainer.scrollTop + ' scrollH=' + bestContainer.scrollHeight, 'info');
      }

      if (allMessages.size === lastCount) {
        stuck++;
        if (stuck >= 10) {
          addLog('Stuck after ' + round + ' rounds. Trying keyboard...', 'warn');

          // Try keyboard
          bestContainer.focus();
          for (let k = 0; k < 20; k++) {
            bestContainer.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', keyCode: 38, bubbles: true }));
            await new Promise(r => setTimeout(r, 300));
          }
          captureViewport();

          if (allMessages.size === lastCount) {
            addLog('Keyboard also stuck. This conversation may be fully loaded.', 'warn');
            break;
          }
          stuck = 0;
        }
      } else {
        stuck = 0;
        lastCount = allMessages.size;
      }

      if (bestContainer.scrollTop <= 0 && stuck >= 3) {
        addLog('Reached top.', 'success');
        break;
      }
    }

    addLog('Final: ' + allMessages.size + ' unique messages', 'success');
  }

  // ===== SAVE =====
  function saveMessages() {
    if (allMessages.size === 0) { addLog('No messages', 'warn'); return; }
    const output = {
      backupDate: new Date().toISOString(),
      platform: 'zalo-web',
      totalMessages: allMessages.size,
      messages: Array.from(allMessages.values()),
    };
    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'zalo_backup_' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(url);
    addLog('Saved ' + allMessages.size + ' messages', 'success');
  }

  // ===== UI =====
  function createUI() {
    if (document.getElementById('zb-btn')) return;
    const btn = document.createElement('div');
    btn.id = 'zb-btn';
    btn.textContent = '\u{1F4E6}';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:999998;width:48px;height:48px;background:#0068ff;color:white;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:24px;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
    btn.onclick = () => {
      const existing = document.getElementById('zb-panel');
      if (existing) { existing.remove(); return; }
      const panel = document.createElement('div');
      panel.id = 'zb-panel';
      panel.style.cssText = 'position:fixed;bottom:80px;right:20px;z-index:999999;background:#1a1a2e;color:white;border-radius:12px;padding:16px;width:500px;font-family:sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.3);font-size:13px;max-height:90vh;overflow-y:auto;';
      panel.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
          <b style="font-size:14px;">Zalo Backup v20.0</b>
          <span id="zb-close" style="cursor:pointer;font-size:18px;">&times;</span>
        </div>
        <div style="color:#aaa;font-size:11px;margin-bottom:8px;">
          Debug + Auto-scroll with multiple methods
        </div>
        <div id="zb-status" style="color:#aaa;margin-bottom:8px;">Ready</div>
        <div id="zb-count" style="color:#00bcd4;font-size:13px;margin-bottom:8px;">Messages: 0</div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <button id="zb-debug" style="flex:1;padding:8px;border:none;border-radius:6px;background:#ff9800;color:white;font-weight:600;cursor:pointer;font-size:12px;">Debug Scroll</button>
          <button id="zb-auto" style="flex:1;padding:8px;border:none;border-radius:6px;background:#0068ff;color:white;font-weight:600;cursor:pointer;font-size:12px;">Auto Scroll</button>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <button id="zb-stop" style="flex:1;padding:8px;border:none;border-radius:6px;background:#f44336;color:white;font-weight:600;cursor:pointer;font-size:12px;" disabled>Stop</button>
          <button id="zb-save" style="flex:1;padding:8px;border:none;border-radius:6px;background:#4caf50;color:white;font-weight:600;cursor:pointer;font-size:12px;" disabled>Save</button>
        </div>
        <div id="zb-log" style="max-height:350px;overflow-y:auto;background:#111;border-radius:6px;padding:8px;font-size:11px;font-family:monospace;color:#888;min-height:80px;"></div>
        <div style="margin-top:8px;display:flex;justify-content:flex-end;">
          <button id="zb-copy-log" style="background:#333;border:none;color:#aaa;cursor:pointer;font-size:11px;padding:2px 8px;border-radius:4px;">Copy Log</button>
        </div>
      `;
      document.body.appendChild(panel);

      panel.querySelector('#zb-close').onclick = () => { panel.remove(); };
      panel.querySelector('#zb-debug').onclick = () => {
        debugScrollables();
        tryAllScrollMethods().then(() => {
          captureViewport();
          addLog('\nCaptured after debug: ' + allMessages.size, 'success');
        });
      };
      panel.querySelector('#zb-auto').onclick = () => {
        panel.querySelector('#zb-auto').disabled = true;
        panel.querySelector('#zb-debug').disabled = true;
        panel.querySelector('#zb-stop').disabled = false;
        panel.querySelector('#zb-save').disabled = false;
        captureViewport();
        autoScroll().then(() => {
          panel.querySelector('#zb-auto').disabled = false;
          panel.querySelector('#zb-debug').disabled = false;
          panel.querySelector('#zb-stop').disabled = true;
        });
      };
      panel.querySelector('#zb-stop').onclick = () => { addLog('Stopped', 'warn'); };
      panel.querySelector('#zb-save').onclick = saveMessages;
      panel.querySelector('#zb-copy-log').onclick = () => {
        navigator.clipboard.writeText(logs.map(l => l.text).join('\n')).then(() => {
          panel.querySelector('#zb-copy-log').textContent = 'Copied!';
          setTimeout(() => { panel.querySelector('#zb-copy-log').textContent = 'Copy Log'; }, 1500);
        });
      };
    };
    document.body.appendChild(btn);
  }

  function init() {
    if (document.body) createUI();
    else { const o = new MutationObserver(() => { if (document.body) { o.disconnect(); createUI(); } }); o.observe(document.documentElement, { childList: true }); }
  }

  if (!window.__zaloBackupListenerAdded) {
    window.__zaloBackupListenerAdded = true;
    chrome.runtime?.onMessage?.addListener((msg, sender, sendResponse) => {
      if (msg.action === 'toggleUI') { const e = document.getElementById('zb-panel'); if (e) e.remove(); else createUI(); sendResponse({ ok: true }); }
      return true;
    });
  }
  init();
})();
