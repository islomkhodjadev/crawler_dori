const dot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const statusDetail = document.getElementById('status-detail');
const tasksLabel = document.getElementById('tasks-label');
const urlInput = document.getElementById('url');
const pwInput = document.getElementById('pw');
const btnConnect = document.getElementById('btn-connect');
const btnDisconnect = document.getElementById('btn-disconnect');
const latInput = document.getElementById('lat');
const lonInput = document.getElementById('lon');
const btnDetectGeo = document.getElementById('btn-detect-geo');
const btnClearGeo = document.getElementById('btn-clear-geo');
const geoStatus = document.getElementById('geo-status');

const LABELS = {
  connected:    'Подключён · Connected',
  connecting:   'Подключение… · Connecting…',
  disconnected: 'Не подключён · Disconnected',
  error:        'Ошибка · Error',
};

function applyStatus({ status, error, tasksHandled, serverUrl }) {
  dot.className = 'dot ' + (status || 'disconnected');
  statusText.textContent = LABELS[status] || status;
  statusDetail.textContent = error || (status === 'connected' ? (serverUrl || '') : '');
  tasksLabel.textContent = tasksHandled > 0
    ? `Запросов выполнено · Tasks handled: ${tasksHandled}`
    : '';
}

// Load saved URL and geolocation into fields
chrome.storage.local.get(['serverUrl', 'geoLat', 'geoLon']).then(d => {
  urlInput.value = d.serverUrl || 'https://predictor.03dorixona.uz';
  if (d.geoLat != null) latInput.value = d.geoLat;
  if (d.geoLon != null) lonInput.value = d.geoLon;
  if (d.geoLat != null && d.geoLon != null) {
    geoStatus.textContent = `📍 ${parseFloat(d.geoLat).toFixed(5)}, ${parseFloat(d.geoLon).toFixed(5)}`;
  }
});

// Ask background for current status
chrome.runtime.sendMessage({ type: 'getStatus' }, resp => {
  if (resp) applyStatus(resp);
});

// Listen for status updates pushed from background
chrome.runtime.onMessage.addListener(msg => {
  if (msg.type === 'status') applyStatus(msg);
});

btnConnect.addEventListener('click', () => {
  const url = urlInput.value.trim();
  const pw = pwInput.value;
  if (!url) { statusDetail.textContent = 'Введите адрес сервера.'; return; }
  if (!pw)  { statusDetail.textContent = 'Введите пароль.'; return; }
  chrome.runtime.sendMessage({ type: 'connect', serverUrl: url, password: pw });
});

btnDisconnect.addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'disconnect' });
});

// Geolocation controls
btnDetectGeo.addEventListener('click', () => {
  geoStatus.textContent = 'Определяем…';
  navigator.geolocation.getCurrentPosition(pos => {
    const lat = pos.coords.latitude.toFixed(6);
    const lon = pos.coords.longitude.toFixed(6);
    latInput.value = lat;
    lonInput.value = lon;
    chrome.storage.local.set({ geoLat: lat, geoLon: lon });
    geoStatus.textContent = `📍 ${lat}, ${lon}`;
    chrome.runtime.sendMessage({ type: 'setGeo', lat, lon });
  }, err => {
    geoStatus.textContent = 'Ошибка: ' + err.message;
  });
});

btnClearGeo.addEventListener('click', () => {
  latInput.value = '';
  lonInput.value = '';
  chrome.storage.local.remove(['geoLat', 'geoLon']);
  geoStatus.textContent = 'Геолокация сброшена · Cleared';
  chrome.runtime.sendMessage({ type: 'setGeo', lat: null, lon: null });
});

// Save lat/lon on manual input
[latInput, lonInput].forEach(inp => inp.addEventListener('change', () => {
  const lat = latInput.value.trim();
  const lon = lonInput.value.trim();
  if (lat && lon) {
    chrome.storage.local.set({ geoLat: lat, geoLon: lon });
    geoStatus.textContent = `📍 ${parseFloat(lat).toFixed(5)}, ${parseFloat(lon).toFixed(5)}`;
    chrome.runtime.sendMessage({ type: 'setGeo', lat, lon });
  }
}));
