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

const SERVER_URL_DEFAULT = 'https://predictor.03dorixona.uz';
const RECONNECT_DELAY_MS = 5000;
const PAGE_LOAD_TIMEOUT_MS = 25000;
const SETTLE_WAIT_MS = 2000;    // extra wait after load for JS-rendered content
const ALLOWED_HOSTS = ['osonapteka.uz', 'arzonapteka.uz'];

let ws = null;
let reconnectTimer = null;
let serverUrl = '';
let password = '';
let agentName = '';
let agentId = '';
let status = 'disconnected'; // 'disconnected' | 'connecting' | 'connected' | 'error'
let lastError = '';
let tasksHandled = 0;

// ------------------------------------------------------------------ storage --

function generateUUID() {
  return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
}

async function loadConfig() {
  const data = await chrome.storage.local.get(['serverUrl', 'password', 'agentName', 'agentId']);
  serverUrl = data.serverUrl || SERVER_URL_DEFAULT;
  password = data.password || '';
  agentName = data.agentName || '';
  // Generate a stable agent_id once and persist it forever
  if (data.agentId) {
    agentId = data.agentId;
  } else {
    agentId = generateUUID();
    await chrome.storage.local.set({ agentId });
  }
}

async function saveConfig(url, pw, name, storeId) {
  serverUrl = url;
  password = pw;
  agentName = name || agentName;
  await chrome.storage.local.set({ serverUrl: url, password: pw, agentName, storeId });
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
  if (!password) {
    setStatus('disconnected', 'Password not configured.');
    return;
  }

  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }

  // Build wss:// URL — always use secure WebSocket (server has TLS)
  const base = serverUrl.replace(/\/+$/, '');
  const wsUrl = base.replace(/^http/, 'ws') + '/api/agent/ws'
    + '?password=' + encodeURIComponent(password)
    + '&agent_id=' + encodeURIComponent(agentId)
    + '&agent_name=' + encodeURIComponent(agentName || 'Агент');

  setStatus('connecting');
  const socket = new WebSocket(wsUrl);
  ws = socket;

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
    if (msg.type === 'arzon_pharmacies' && msg.id && msg.product_url) {
      await handleArzonPharmacies(msg);
    }
    if (msg.type === 'oson_pharmacies' && msg.id && msg.product_url) {
      await handleOsonPharmacies(msg);
    }
  };

  ws.onerror = () => {
    if (ws !== socket) return;
    setStatus('error', 'WebSocket error — check server URL.');
  };

  ws.onclose = (event) => {
    if (ws !== socket) return;
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
  const { id, url } = task;

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

// ------------------------------------------------------------------ arzon per-pharmacy prices --

async function handleArzonPharmacies(task) {
  const { id, product_url, region_id } = task;

  let tabId = null;
  try {
    // Step 0: clear the cart first so leftover items don't pollute the pharmacy page
    const cartTab = await chrome.tabs.create({ url: 'https://arzonapteka.uz/ru/cart', active: false });
    tabId = cartTab.id;
    await waitForTabLoad(tabId, PAGE_LOAD_TIMEOUT_MS);
    await sleep(1500);
    await clearArzonCart(tabId);
    await sleep(1500);

    // Step 1: open the product page and add to cart
    await chrome.tabs.update(tabId, { url: product_url });
    await waitForTabLoad(tabId, PAGE_LOAD_TIMEOUT_MS);
    await sleep(2500);

    // Set region in localStorage before interacting
    if (region_id) {
      await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: (rid) => { try { localStorage.setItem('arzon.region.v1', String(rid)); } catch(_) {} },
        args: [region_id],
      });
    }

    // Click "В корзину" (Add to cart) button
    const addResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: () => {
        const btn = [...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'В корзину');
        if (btn) { btn.click(); return 'clicked'; }
        return 'not found';
      },
    });
    const addStatus = addResult?.[0]?.result;
    if (addStatus !== 'clicked') {
      sendError(id, 'Add to cart button not found on ' + product_url);
      chrome.tabs.remove(tabId).catch(() => {});
      tabId = null;
      return;
    }
    await sleep(1500);

    // Step 2: navigate to checkout/pharmacies?start=best_price (sorted by price, no geo needed)
    await chrome.tabs.update(tabId, { url: 'https://arzonapteka.uz/ru/checkout/pharmacies?start=best_price' });
    await waitForTabLoad(tabId, PAGE_LOAD_TIMEOUT_MS);
    await sleep(3000);

    // Step 3: read pharmacy list from the SSR page
    const pageResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: () => ({
        html: document.documentElement.outerHTML,
        url: location.href,
        text: document.body.innerText,
      }),
    });
    const { html, url: finalUrl } = pageResult?.[0]?.result || {};

    // Step 4: clear cart again to leave it clean
    await chrome.tabs.update(tabId, { url: 'https://arzonapteka.uz/ru/cart' });
    await waitForTabLoad(tabId, 15000);
    await sleep(1500);
    await clearArzonCart(tabId);
    await sleep(500);

    tasksHandled++;
    setStatus('connected');
    ws.send(JSON.stringify({ id, html: html || '', url: finalUrl || product_url, status: 200 }));
  } catch (err) {
    sendError(id, err.message || String(err));
  } finally {
    if (tabId !== null) chrome.tabs.remove(tabId).catch(() => {});
  }
}

async function clearArzonCart(tabId) {
  // Click "Очистить корзину" and confirm the dialog that appears
  await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: () => {
      const btn = [...document.querySelectorAll('button')].find(b => /очистить корзину/i.test(b.innerText));
      if (btn) btn.click();
    },
  });
  // Wait for confirmation dialog to appear
  await sleep(800);
  // Click the "Очистить" confirm button (exact text, not "Очистить корзину")
  await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: () => {
      const btn = [...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'Очистить');
      if (btn) btn.click();
    },
  });
}

// ------------------------------------------------------------------ oson per-pharmacy prices --

async function handleOsonPharmacies(task) {
  const { id, product_url, region_ids } = task;
  let tabId = null;
  try {
    // Open product page; region is set via cookie before load
    const tab = await chrome.tabs.create({ url: 'about:blank', active: false });
    tabId = tab.id;

    // Set region cookie so SSR returns pharmacies for the right district
    if (region_ids && region_ids.length) {
      const regionValue = region_ids.join('-');
      await chrome.cookies.set({
        url: 'https://osonapteka.uz',
        name: 'regions',
        value: regionValue,
        domain: '.osonapteka.uz',
        path: '/',
      }).catch(() => {});
    }

    // Navigate to product page
    await chrome.tabs.update(tabId, { url: product_url });
    await waitForTabLoad(tabId, PAGE_LOAD_TIMEOUT_MS);
    await sleep(2000);

    // Extract per-pharmacy data from Pinia/SSR state
    const result = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: () => {
        try {
          const app = document.querySelector('#__nuxt')?.__vue_app__;
          const pinia = app?.config?.globalProperties?.$pinia;
          const posStore = pinia?.state?.value?.['PosProductStore'];
          if (posStore) return { source: 'pinia', data: JSON.stringify(posStore) };
        } catch (_) {}
        // Fallback: read SSR JSON script tag
        const scripts = [...document.querySelectorAll('script[type="application/json"]')];
        return {
          source: 'ssr',
          html: document.documentElement.outerHTML,
          url: location.href,
        };
      },
    });

    const res = result?.[0]?.result || {};
    tasksHandled++;
    setStatus('connected');
    ws.send(JSON.stringify({ id, ...res, url: product_url, status: 200 }));
  } catch (err) {
    sendError(id, err.message || String(err));
  } finally {
    if (tabId !== null) chrome.tabs.remove(tabId).catch(() => {});
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
  if (msg.type === 'loadBranches' || msg.type === 'connect') {
    configReady.then(async () => {
      const pw = msg.password || password;
      if (!pw) throw new Error('Введите пароль · Enter the agent password');
      const payload = {password: pw};
      if (msg.type === 'connect') {
        Object.assign(payload, {agent_id: agentId, store_id: msg.storeId});
      }
      const response = await fetch(SERVER_URL_DEFAULT + '/api/agent/setup', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Agent setup failed');
      if (msg.type === 'connect') {
        await saveConfig(SERVER_URL_DEFAULT, pw, msg.agentName, msg.storeId);
        disconnect();
        connect();
      }
      sendResponse({ok: true, items: result.items});
    }).catch(error => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (msg.type === 'shareLocation') {
    if (ws?.readyState !== WebSocket.OPEN) {
      sendResponse({ok: false});
      return;
    }
    ws.send(JSON.stringify({type: 'device_location', lat: msg.lat, lng: msg.lng}));
    sendResponse({ok: true});
    return;
  }
  if (msg.type === 'disconnect') {
    disconnect();
    sendResponse({ ok: true });
    return true;
  }

});

// ------------------------------------------------------------------ startup --

// Manifest V3 service workers restart frequently; reconnect on each startup.
const configReady = loadConfig();
configReady.then(() => {
  if (password) connect();
});

// Alarms keep the service worker alive in MV3
chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => {
  if (password && (!ws || ws.readyState === WebSocket.CLOSED)) {
    connect();
  }
});
