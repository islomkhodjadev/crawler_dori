const dot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const statusDetail = document.getElementById('status-detail');
const tasksLabel = document.getElementById('tasks-label');
const pwInput = document.getElementById('pw');
const branchInput = document.getElementById('branch');
const loadBranchesButton = document.getElementById('load-branches');
const setupMessage = document.getElementById('setup-message');
const agentNameInput = document.getElementById('agent-name');
const btnConnect = document.getElementById('btn-connect');
const btnDisconnect = document.getElementById('btn-disconnect');
const shareLocation = document.getElementById('share-location');
const locationMessage = document.getElementById('location-message');
let sharing = false;
let connected = false;
const LABELS = {
  connected:    'Подключён · Connected',
  connecting:   'Подключение… · Connecting…',
  disconnected: 'Не подключён · Disconnected',
  error:        'Ошибка · Error',
};

function applyStatus({ status, error, tasksHandled, serverUrl }) {
  connected = status === 'connected';
  shareLocation.disabled = !connected || sharing;
  if (!connected && !sharing) locationMessage.textContent = '';
  dot.className = 'dot ' + (status || 'disconnected');
  statusText.textContent = LABELS[status] || status;
  statusDetail.textContent = error || (status === 'connected' ? (serverUrl || '') : '');
  tasksLabel.textContent = tasksHandled > 0
    ? `Запросов выполнено · Tasks handled: ${tasksHandled}`
    : '';
}

let storedBranch = '';
let branches = new Map();
let branchesPassword = null;

async function loadBranches() {
  const requestedPassword = pwInput.value;
  const chosen = branchInput.value || storedBranch;
  branchInput.disabled = btnConnect.disabled = loadBranchesButton.disabled = true;
  setupMessage.textContent = '';
  try {
    const reply = await chrome.runtime.sendMessage({type: 'loadBranches', password: requestedPassword});
    if (requestedPassword !== pwInput.value) return;
    if (!reply?.ok) throw new Error(reply?.error || 'Не удалось загрузить филиалы · Could not load branches');
    branches = new Map(reply.items.map(branch => [String(branch.store_id), branch]));
    const empty = new Option('Выберите филиал · Select a branch', '');
    branchInput.replaceChildren(empty, ...[...branches].map(([id, branch]) => {
      const address = branch.store_address ? ' · ' + branch.store_address : '';
      const missing = branch.location_available === false ? ' · Нет координат / No location' : '';
      const option = new Option(branch.store_name + address + missing, id);
      option.disabled = branch.location_available === false;
      return option;
    }));
    if (branches.has(chosen) && branches.get(chosen).location_available !== false) branchInput.value = chosen;
    branchesPassword = requestedPassword;
    if (!branches.size) setupMessage.textContent = 'Нет синхронизированных филиалов · No synced branches';
    branchInput.disabled = !branches.size;
    btnConnect.disabled = !branchInput.value;
  } catch (error) {
    setupMessage.textContent = error.message;
  } finally {
    loadBranchesButton.disabled = false;
  }
}

chrome.storage.local.get(['agentName', 'storeId', 'password']).then(data => {
  storedBranch = String(data.storeId ?? '');
  agentNameInput.value = data.agentName || '';
  if (data.password && !pwInput.value) loadBranches();
});

chrome.runtime.sendMessage({type: 'getStatus'}, response => {
  if (response) applyStatus(response);
});
chrome.runtime.onMessage.addListener(message => {
  if (message.type === 'status') applyStatus(message);
});

pwInput.addEventListener('input', () => {
  branchInput.disabled = true;
  btnConnect.disabled = true;
  branchesPassword = null;
});
pwInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !loadBranchesButton.disabled) loadBranches();
});
loadBranchesButton.addEventListener('click', loadBranches);
branchInput.addEventListener('change', () => {
  const branch = branches.get(branchInput.value);
  if (branch) agentNameInput.value = branch.store_name;
  btnConnect.disabled = !branch;
});

btnConnect.addEventListener('click', async () => {
  const branch = branches.get(branchInput.value);
  if (!branch || branchesPassword !== pwInput.value) return;
  setupMessage.textContent = '';
  btnConnect.disabled = true;
  try {
    const reply = await chrome.runtime.sendMessage({
      type: 'connect', password: pwInput.value, storeId: branch.store_id,
      agentName: agentNameInput.value.trim() || branch.store_name,
    });
    if (!reply?.ok) throw new Error(reply?.error || 'Connection failed');
    storedBranch = branchInput.value;
  } catch (error) {
    setupMessage.textContent = error.message;
  } finally {
    btnConnect.disabled = branchesPassword !== pwInput.value || !branchInput.value;
  }
});

btnDisconnect.addEventListener('click', () => chrome.runtime.sendMessage({type: 'disconnect'}));


shareLocation.addEventListener('click', async () => {
  sharing = true;
  shareLocation.disabled = true;
  locationMessage.textContent = 'Определяем местоположение… · Finding location…';
  try {
    const position = await new Promise((resolve, reject) =>
      navigator.geolocation.getCurrentPosition(resolve, reject, {maximumAge: 0, timeout: 12000}));
    const reply = await chrome.runtime.sendMessage({
      type: 'shareLocation', lat: position.coords.latitude, lng: position.coords.longitude,
    });
    if (!reply?.ok) throw new Error('Подключите агент снова · Reconnect the agent');
    locationMessage.textContent = 'Местоположение отправлено · Location sent';
  } catch (error) {
    locationMessage.textContent = error.message || 'Местоположение недоступно · Location unavailable';
  } finally {
    sharing = false;
    shareLocation.disabled = !connected;
  }
});
