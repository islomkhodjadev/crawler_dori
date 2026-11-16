const dot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const statusDetail = document.getElementById('status-detail');
const tasksLabel = document.getElementById('tasks-label');
const urlInput = document.getElementById('url');
const pwInput = document.getElementById('pw');
const btnConnect = document.getElementById('btn-connect');
const btnDisconnect = document.getElementById('btn-disconnect');

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

// Load saved URL into field, default to production server
chrome.storage.local.get(['serverUrl']).then(d => {
  urlInput.value = d.serverUrl || 'https://predictor.03dorixona.uz';
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
