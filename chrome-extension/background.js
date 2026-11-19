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
let geoLat = null;
let geoLon = null;

// ------------------------------------------------------------------ storage --

async function loadConfig() {
  const data = await chrome.storage.local.get(['serverUrl', 'password', 'geoLat', 'geoLon']);
  serverUrl = data.serverUrl || '';
  password = data.password || '';
  geoLat = data.geoLat ?? null;
  geoLon = data.geoLon ?? null;
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
    if (msg.type === 'arzon_pharmacies' && msg.id && msg.product_url) {
      await handleArzonPharmacies(msg);
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

    // If geolocation is configured, inject an override immediately so the page
    // receives our coordinates when it calls navigator.geolocation.getCurrentPosition.
    if (geoLat != null && geoLon != null && site === 'arzon') {
      await injectGeoOverride(tabId, parseFloat(geoLat), parseFloat(geoLon));
    }

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

// Inject a geolocation override into a tab so the site gets our coordinates.
// Uses executeScript with world:'MAIN' to override navigator.geolocation directly.
async function injectGeoOverride(tabId, lat, lon) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: (latitude, longitude) => {
        const position = {
          coords: { latitude, longitude, accuracy: 100, altitude: null, altitudeAccuracy: null, heading: null, speed: null },
          timestamp: Date.now(),
        };
        navigator.geolocation.getCurrentPosition = (success) => success(position);
        navigator.geolocation.watchPosition = (success) => { success(position); return 1; };
      },
      args: [lat, lon],
    });
  } catch (_) {
    // Best-effort; tab may not be ready yet
  }
}

// ------------------------------------------------------------------ arzon per-pharmacy prices --

async function handleArzonPharmacies(task) {
  const { id, product_url, region_id } = task;

  let tabId = null;
  try {
    // Step 1: open the product page and add to cart
    const tab = await chrome.tabs.create({ url: product_url, active: false });
    tabId = tab.id;
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
    const { html, url: finalUrl, text } = pageResult?.[0]?.result || {};

    // Step 4: remove the product from cart (clear cart)
    await chrome.tabs.update(tabId, { url: 'https://arzonapteka.uz/ru/cart' });
    await waitForTabLoad(tabId, 15000);
    await sleep(1500);
    // Click "Удалить все" or individual delete button
    await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: () => {
        // Try "clear all" button first
        const clearAll = [...document.querySelectorAll('button')].find(
          b => /удалить все|очистить|clear all/i.test(b.innerText)
        );
        if (clearAll) { clearAll.click(); return; }
        // Fall back to individual delete buttons
        const delBtns = [...document.querySelectorAll('button[aria-label*="удалить"], button[aria-label*="Удалить"], button[aria-label*="delete"], button[aria-label*="Remove"]')];
        delBtns.forEach(b => b.click());
      },
    });
    await sleep(1000);

    tasksHandled++;
    setStatus('connected');
    ws.send(JSON.stringify({ id, html: html || '', url: finalUrl || product_url, status: 200 }));
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
  if (msg.type === 'setGeo') {
    geoLat = msg.lat;
    geoLon = msg.lon;
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
