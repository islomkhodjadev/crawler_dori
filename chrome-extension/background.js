/**
 * Dori Price Agent — background service worker
 *
 * Maintains a persistent WebSocket connection to the Dori server.
 * When the server sends a fetch task, this worker opens the target URL
 * in a temporary background tab (real Chrome, real user IP/cookies),
 * waits for the page to settle, reads the HTML, and sends it back.
 *
 * No passwords or tokens are ever sent to oson/arzon — only fetch tasks.
 */

const RECONNECT_DELAY_MS = 5000;
const PAGE_LOAD_TIMEOUT_MS = 25000;
const SETTLE_WAIT_MS = 2000;    // extra wait after load for JS-rendered content
const ALLOWED_HOSTS = ['osonapteka.uz', 'arzonapteka.uz'];

let ws = null;
let reconnectTimer = null;
let serverUrl = '';
let password = '';
let status = 'disconnected'; // 'disconnected' | 'connecting' | 'connected' | 'error'
let lastError = '';
let tasksHandled = 0;

// ------------------------------------------------------------------ storage --

async function loadConfig() {
  const data = await chrome.storage.local.get(['serverUrl', 'password']);
  serverUrl = data.serverUrl || '';
  password = data.password || '';
}

async function saveConfig(url, pw) {
  serverUrl = url;
  password = pw;
  await chrome.storage.local.set({ serverUrl: url, password: pw });
}

// ------------------------------------------------------------------ status broadcast --

function setStatus(s, error = '') {
  status = s;
  lastError = error;
  // Notify popup if open
  chrome.runtime.sendMessage({ type: 'status', status: s, error, tasksHandled })
    .catch(() => {}); // popup may not be open
}

// ------------------------------------------------------------------ WebSocket --

function connect() {
  if (!serverUrl || !password) {
    setStatus('disconnected', 'Server URL and password not configured.');
    return;
  }

  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }

  // Build wss:// URL — always use secure WebSocket (server has TLS)
  const base = serverUrl.replace(/\/+$/, '');
  const wsUrl = base.replace(/^http/, 'ws') + '/api/agent/ws?password=' + encodeURIComponent(password);

  setStatus('connecting');
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    setStatus('connected');
    clearTimeout(reconnectTimer);
    // Send a ping every 20s to keep the connection alive through idle timeouts
    keepAlive();
  };

  ws.onmessage = async (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'ping') { ws.send(JSON.stringify({ type: 'ping' })); return; }
    if (msg.type === 'fetch' && msg.id && msg.url) {
      await handleFetch(msg);
    }
  };

  ws.onerror = (err) => {
    setStatus('error', 'WebSocket error — check server URL.');
  };

  ws.onclose = (event) => {
    ws = null;
    if (status !== 'disconnected') {
      setStatus('error', `Disconnected (${event.code}). Reconnecting…`);
      scheduleReconnect();
    }
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
}

function keepAlive() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'ping' }));
  setTimeout(keepAlive, 20000);
}

function disconnect() {
  clearTimeout(reconnectTimer);
  if (ws) { ws.close(1000, 'User disconnected'); ws = null; }
  setStatus('disconnected');
}

// ------------------------------------------------------------------ fetch task --

async function handleFetch(task) {
  const { id, url, site } = task;

  // Validate the target host
  let targetHost;
  try { targetHost = new URL(url).hostname; } catch {
    sendError(id, 'Invalid URL: ' + url);
    return;
  }
  if (!ALLOWED_HOSTS.some(h => targetHost === h || targetHost.endsWith('.' + h))) {
    sendError(id, 'Host not allowed: ' + targetHost);
    return;
  }

  let tabId = null;
  try {
    // Create a background tab (not visible to the user unless they look at tab bar)
    const tab = await chrome.tabs.create({ url, active: false });
    tabId = tab.id;

    // Wait for the tab to finish loading
    await waitForTabLoad(tabId, PAGE_LOAD_TIMEOUT_MS);

    // Extra settle time for JS-rendered content (Oson uses Vue/React)
    await sleep(SETTLE_WAIT_MS);

    // Read the page HTML via content script injection
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ html: document.documentElement.outerHTML, url: location.href })
    });

    if (!results || !results[0] || !results[0].result) {
      throw new Error('executeScript returned no result');
    }

    const { html, url: finalUrl } = results[0].result;
    tasksHandled++;
    setStatus('connected');

    ws.send(JSON.stringify({ id, html, url: finalUrl, status: 200 }));
  } catch (err) {
    sendError(id, err.message || String(err));
  } finally {
    if (tabId !== null) {
      chrome.tabs.remove(tabId).catch(() => {});
    }
  }
}

function sendError(id, message) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ id, error: message }));
  }
}

function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Tab load timeout after ' + timeoutMs + 'ms'));
    }, timeoutMs);

    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ------------------------------------------------------------------ messages from popup --

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getStatus') {
    sendResponse({ status, error: lastError, tasksHandled, serverUrl });
    return true;
  }
  if (msg.type === 'connect') {
    saveConfig(msg.serverUrl, msg.password).then(() => {
      disconnect();
      connect();
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === 'disconnect') {
    disconnect();
    sendResponse({ ok: true });
    return true;
  }
});

// ------------------------------------------------------------------ startup --

// Manifest V3 service workers restart frequently; reconnect on each startup.
loadConfig().then(() => {
  if (serverUrl && password) connect();
});

// Alarms keep the service worker alive in MV3
chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => {
  if (serverUrl && password && (!ws || ws.readyState === WebSocket.CLOSED)) {
    connect();
  }
});
