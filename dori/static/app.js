// Admin panel: a five-step workflow (products → connect → collect → review → results) in RU / UZ / EN.
let sourceState = {}, activeRun = false, viewerSocket = null, viewerSite = null;
let products = [], selected = new Set(), currentRun = null, currentReport = null, aiConfigured = false;
let runs = [], stats = null, expanded = new Set(), openRejected = new Set(), sites = new Set(['arzon', 'oson']);
let lastReportJson = '', lastStatsJson = '', tab = 'products', editingPrice = null;
const SITE_NAMES = {arzon: 'ArzonApteka', oson: 'OsonApteka'};
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const safeUrl = u => { try { const x = new URL(u); return x.protocol === 'https:' && ['arzonapteka.uz', 'osonapteka.uz'].includes(x.hostname) ? x.href : '#'; } catch { return '#'; } };
// Browser storage can be unavailable (private mode); the panel works without it.
const saved = {get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} }};

let lang = I18N[saved.get('dori-lang')] ? saved.get('dori-lang') : 'ru';
const t = (key, vars = {}) => String(I18N[lang][key] ?? I18N.en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
const locale = () => lang === 'en' ? 'en-US' : 'ru-RU';
const money = n => n == null ? '—' : new Intl.NumberFormat(locale(), {maximumFractionDigits: 0}).format(n) + ' ' + t('currency');
const when = iso => iso ? new Date(iso).toLocaleString(locale(), {dateStyle: 'short', timeStyle: 'short'}) : '';
const stateLabel = s => I18N.en['state.' + s] ? t('state.' + s) : String(s || '').replaceAll('_', ' ');
const stateClass = s => ({connected: 'ok', ok: 'ok', needs_attention: 'warn', closed: '', unchecked: '', no_results: ''})[s] ?? 'bad';
const siteName = s => SITE_NAMES[s] || s;
const duration = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

let toastTimer;
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 7000); }
async function api(url, options = {}) {
  const r = await fetch(url, options);
  let data;
  try { data = await r.json(); } catch { throw Error('Server response could not be read'); }
  if (r.status === 401) { location.assign('/login'); throw Error('Sign in required'); }
  if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

/* ---------- language and tabs ---------- */
function applyLanguage() {
  document.documentElement.lang = lang;
  $$('[data-i18n]').forEach(el => el.textContent = t(el.dataset.i18n));
  $$('[data-i18n-ph]').forEach(el => el.placeholder = t(el.dataset.i18nPh));
  $$('[data-lang]').forEach(b => b.classList.toggle('active', b.dataset.lang === lang));
  const filter = $('#report-filter').value || 'all';
  $('#report-filter').innerHTML = ['all', 'matched', 'needs_review', 'rejected', 'no_candidates', 'source_error']
    .map(v => `<option value="${v}">${esc(t('filter.' + v))}</option>`).join('');
  $('#report-filter').value = filter;
  $('#ai-availability').textContent = t(aiConfigured ? 'connect.aiOn' : 'connect.aiOff');
  renderAll();
}
function showTab(name) {
  tab = name;
  saved.set('dori-tab', name);
  $$('[data-panel]').forEach(p => p.hidden = p.dataset.panel !== name);
  $$('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  renderNext();
}
function renderAll() {
  renderProducts(); renderBrowsers(); renderRuns(); renderCollect(); renderStats();
  if (currentReport) renderReport(currentReport);
  renderNext();
}

/* ---------- what to do next ---------- */
function reviewItems() {
  return (currentReport?.products || []).flatMap(entry => entry.offers.filter(o => o.match_status === 'review').map(o => ({p: entry.product, o})));
}
function nextStep() {
  if (!products.length) return ['products', t('next.import')];
  const waiting = runs.find(r => r.status === 'waiting_for_human');
  if (waiting) return ['connect', t('next.verify', {site: siteName((waiting.error || '').split(':')[0])})];
  const live = runs.find(r => ['queued', 'running'].includes(r.status));
  if (live) return ['collect', t('next.running', {done: live.completed, total: live.total})];
  const review = reviewItems().length;
  if (review) return ['review', t('next.review', {n: review})];
  if (!runs.length) {
    if (!selected.size) return ['products', t('next.select')];
    if (!connectionsReady()) return ['connect', t('next.connect')];
    return ['collect', t('next.start')];
  }
  return ['results', t('next.done')];
}
function renderNext() {
  const [target, text] = nextStep();
  $('#next-text').textContent = text;
  $('#next-go').hidden = target === tab;
  $('#next-go').dataset.goto = target;
  const review = reviewItems().length, connected = ['arzon', 'oson'].filter(s => sourceState[s]?.status === 'connected').length;
  const live = runs.find(r => ['queued', 'running', 'waiting_for_human'].includes(r.status));
  $('#badge-products').textContent = products.length || '';
  $('#badge-connect').textContent = `${connected}/2`;
  $('#badge-connect').className = connected === 2 ? 'ok' : '';
  $('#badge-collect').textContent = live && stats ? `${stats.progress_percent}%` : '';
  $('#badge-review').textContent = review || '';
  $('#badge-review').className = review ? 'hot' : '';
  $('#badge-results').textContent = (currentReport?.products || []).filter(x => x.estimate.value != null).length || '';
}

/* ---------- 1. products ---------- */
function renderProducts() {
  const q = $('#search').value.toLowerCase();
  const shown = products.filter(p => (p.name + ' ' + p.manufacturer).toLowerCase().includes(q));
  $('#products').innerHTML = shown.map(p => `<tr class="${selected.has(p.id) ? 'picked' : ''}">
    <td><input type="checkbox" data-select="${p.id}" aria-label="${esc(p.name)}" ${selected.has(p.id) ? 'checked' : ''}></td>
    <td><strong>${esc(p.name)}</strong><small>${esc(p.manufacturer || t('products.noManufacturer'))}${p.barcode ? ' · ' + esc(p.barcode) : ''}</small></td>
    <td class="num">${money(p.invoice_price)}</td>
    <td class="num">${aiConfigured ? `<button class="ghost small" data-ai="${p.id}">AI</button>` : ''}</td></tr>`).join('');
  $('#products-empty').hidden = products.length > 0;
  $('#selected-label').textContent = t('products.selected', {n: selected.size});
  $('#all-products').checked = products.length > 0 && selected.size === products.length;
  renderCollect();
}
async function loadProducts() {
  products = await api('/api/products');
  const ids = new Set(products.map(p => p.id));
  selected = new Set([...selected].filter(id => ids.has(id)));
  renderProducts();
  const previous = $('#test-product').value;
  $('#test-product').innerHTML = products.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('') || `<option value="">—</option>`;
  if (products.some(p => String(p.id) === previous)) $('#test-product').value = previous;
  renderNext();
}

/* ---------- 2. connect ---------- */
function chosenSites() { return [...sites]; }
function connectionsReady() { const list = chosenSites(); return list.length > 0 && list.every(s => sourceState[s]?.status === 'connected'); }
function renderBrowsers() {
  $('#browser-cards').innerHTML = ['arzon', 'oson'].map(site => {
    const s = sourceState[site] || {status: 'closed', message: '', opened: false};
    const note = s.status === 'connected' && s.checked_at ? t('connect.checkedAt', {t: when(s.checked_at)}) : s.message;
    return `<article class="card site ${stateClass(s.status)}">
      <div class="site-head"><h3>${siteName(site)}</h3><span class="pill ${stateClass(s.status)}">${esc(stateLabel(s.status))}</span></div>
      <p class="muted">${esc(note || '')}</p>
      <div class="row-actions">
        <button data-browser-open="${site}" class="${s.status === 'connected' ? 'ghost' : ''}">${t('connect.open')}</button>
        <button data-browser-check="${site}" class="${s.opened && s.status !== 'connected' ? '' : 'ghost'}" ${!s.opened || s.busy || !products.length ? 'disabled' : ''}>${t('connect.check')}</button>
      </div></article>`;
  }).join('');
  renderCollect();
}
async function refreshBrowsers() { sourceState = await api('/api/browsers'); renderBrowsers(); renderNext(); }
async function checkConnection(site) {
  const product_id = Number($('#test-product').value);
  if (!product_id) { toast(t('connect.needProduct')); return; }
  toast(t('connect.checking'));
  const result = await post(`/api/browsers/${site}/check`, {product_id, use_ai: $('#use-ai').checked});
  await refreshBrowsers();
  toast(result.ok ? t('connect.verified', {n: result.listings}) + (result.warning ? ' · ' + result.warning : '') : result.error);
}

/* ---------- 3. collect ---------- */
function collectBlocker() {
  if (activeRun) return t('collect.busy');
  if (!selected.size) return t('collect.noSelection');
  if (!sites.size) return t('collect.noSites');
  if (!connectionsReady()) return t('collect.notReady');
  return '';
}
function renderCollect() {
  $('#collect-selected').textContent = selected.size;
  $('#collect-sites').innerHTML = ['arzon', 'oson'].map(site => {
    const status = sourceState[site]?.status || 'closed';
    return `<label class="check site-pick"><input type="checkbox" data-site-toggle="${site}" ${sites.has(site) ? 'checked' : ''}> ${siteName(site)} <span class="pill ${stateClass(status)}">${esc(stateLabel(status))}</span></label>`;
  }).join('');
  const blocker = collectBlocker();
  $('#collect-blocker').textContent = blocker;
  $('#collect-blocker').hidden = !blocker || activeRun;
  $('#crawl').disabled = Boolean(blocker);
}
function renderStats() {
  if (!stats || !stats.run) {
    $('#progress-label').textContent = t('collect.idle');
    $('#run-progress').value = 0; $('#elapsed-label').textContent = ''; $('#active-work').textContent = '';
    $('#waiting-alert').hidden = true;
    return;
  }
  const p = stats.products, l = stats.listings, r = stats.run;
  $('#progress-label').textContent = t('collect.progress', {status: t('run.' + r.status), done: p.completed, total: p.selected});
  $('#run-progress').value = stats.progress_percent;
  $('#elapsed-label').textContent = t('collect.elapsed', {t: duration(stats.elapsed_seconds)});
  $('#active-work').textContent = stats.active ? t('collect.now', {name: stats.active.name, site: siteName(stats.active.site)}) : (r.status === 'failed' && r.error ? r.error : '');
  const waitingSite = r.status === 'waiting_for_human' ? (r.error || '').split(':')[0] : '';
  $('#waiting-alert').hidden = !waitingSite;
  if (waitingSite) {
    $('#waiting-text').textContent = t('collect.waiting', {site: siteName(waitingSite)});
    $('#waiting-open').textContent = t('collect.openSite', {site: siteName(waitingSite)});
    $('#waiting-open').dataset.browserOpen = waitingSite;
  }
  const errors = stats.sources.reduce((n, s) => n + s.errors, 0);
  $('#m-checked').textContent = `${p.completed}/${p.selected}`;
  $('#m-matched').textContent = p.matched;
  $('#m-review').textContent = l.review;
  $('#m-errors').textContent = errors;
  $('#m-errors').parentElement.classList.toggle('bad', errors > 0);
  $('#source-statistics').innerHTML = stats.sources.map(s => `<tr><td><strong>${siteName(s.site)}</strong></td><td class="num">${s.checks_completed}</td><td class="num">${s.successful}</td><td class="num">${s.no_results}</td><td class="num">${s.errors}</td><td class="num">${s.candidates}</td></tr>`).join('');
  $('#activity-log').innerHTML = stats.events.length ? stats.events.map(e => `<div class="event">
      <div><strong>${siteName(e.site)} · ${esc(e.name)}</strong> <span class="pill ${stateClass(e.status)}">${esc(stateLabel(e.status))}</span></div>
      <small>${t('collect.counts', {a: e.counts.accepted, r: e.counts.review, x: e.counts.rejected})}${e.message ? ' · ' + esc(e.message) : ''}</small></div>`).join('')
    : `<p class="muted">${t('collect.noEvents')}</p>`;
}
async function loadStatistics() {
  const data = await api('/api/statistics' + (currentRun ? '?run_id=' + currentRun : ''));
  const json = JSON.stringify(data);
  if (json === lastStatsJson) return;
  lastStatsJson = json; stats = data;
  renderStats(); renderNext();
}

/* ---------- runs ---------- */
function renderRuns() {
  $('#run-select').innerHTML = runs.length
    ? runs.map(r => `<option value="${r.id}" ${r.id === currentRun ? 'selected' : ''}>№${r.id} · ${esc(t('run.' + r.status))} · ${esc(when(r.started_at))}</option>`).join('')
    : `<option value="">${esc(t('run.none'))}</option>`;
  const r = runs.find(x => x.id === currentRun);
  $('#cancel-run').hidden = !r || !['queued', 'running', 'waiting_for_human'].includes(r.status);
  const query = currentRun ? '?run_id=' + currentRun : '';
  $('#export').href = '/api/export' + query;
  $('#export-candidates').href = '/api/export/candidates' + query;
}
async function refreshRuns() {
  runs = await api('/api/runs');
  if (!runs.some(x => x.id === currentRun)) currentRun = runs[0]?.id ?? null;
  activeRun = runs.some(x => ['queued', 'running', 'waiting_for_human'].includes(x.status));
  renderRuns(); renderCollect();
  await loadReport();
  await loadStatistics();
  renderNext();
}

/* ---------- prices and decisions ---------- */
function diffInfo(ours, theirs) {
  if (!ours || theirs == null) return {html: '<span class="diff">—</span>', pct: null};
  const pct = Math.round((theirs / ours - 1) * 1000) / 10;
  const cls = pct < 0 ? 'cheaper' : pct > 0 ? 'pricier' : 'same';
  const text = pct < 0 ? t('diff.cheaper') : pct > 0 ? t('diff.pricier') : t('diff.same');
  const warn = Math.abs(pct) > 60 ? `<span class="check-price">${t('diff.check')}</span>` : '';
  return {html: `<span class="diff ${cls}">${pct > 0 ? '+' : ''}${pct}%</span> <span class="muted">${text}</span>${warn}`, pct};
}
// Site price with an inline editor; a manual price replaces the read one in the estimate.
function priceBlock(o, big = false) {
  if (editingPrice === o.id) {
    return `<form class="price-form" data-price-form="${o.id}"><input name="price" type="number" min="1" step="1" value="${Math.round(o.price)}" aria-label="${esc(t('price.edit'))}">
      <button>${t('price.save')}</button><button type="button" class="ghost" data-price-cancel="1">${t('price.cancel')}</button>
      ${o.manual_price != null ? `<button type="button" class="ghost" data-price-reset="${o.id}">${t('price.reset')}</button>` : ''}</form>`;
  }
  return `<span class="price-wrap"><b class="${big ? 'price' : ''}">${money(o.price)}</b>
    <button class="link small" data-price-edit="${o.id}">✎ ${t('price.edit')}</button>
    ${o.manual_price != null ? `<small class="edited">${t('price.edited', {p: money(o.observed_price)})}</small>` : ''}</span>`;
}
function decisionButtons(o) {
  // A hard conflict (different strength, form, pack or manufacturer) cannot be approved.
  const canApprove = o.match_status !== 'accepted' && !o.reasons.some(r => r.includes('differs'));
  return `<div class="row-actions">${canApprove ? `<button class="approve" data-review="${o.id}" data-status="accepted">${t('review.approve')}</button>` : ''}${o.match_status !== 'rejected' ? `<button class="reject" data-review="${o.id}" data-status="rejected">${t('review.reject')}</button>` : ''}</div>`;
}
function renderReview() {
  const items = reviewItems();
  $('#review-queue').innerHTML = items.length ? items.map(({p, o}) => `<article class="card rv">
      <div class="rv-compare">
        <div class="rv-side"><span class="label">${t('review.ours')}</span><strong>${esc(p.name)}</strong><small>${esc(p.manufacturer)}</small><b class="price">${money(p.invoice_price)}</b></div>
        <div class="rv-vs">↔</div>
        <div class="rv-side"><span class="label">${t('review.theirs')} · <span class="site-tag ${o.site}">${siteName(o.site)}</span></span>
          <a href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener"><strong>${esc(o.name)}</strong></a><small>${esc(o.manufacturer)}</small>${priceBlock(o, true)}</div>
      </div>
      <div class="rv-diff">${diffInfo(p.invoice_price, o.price).html}</div>
      <p class="comment">${esc(o.comment)}</p>
      <div class="rv-foot"><a class="link" href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener">${t('review.open')}</a>${decisionButtons(o)}</div>
    </article>`).join('') : `<div class="card empty">${t('review.empty')}</div>`;
}
function offerLine(p, o) {
  return `<div class="offer ${o.match_status}">
    <div class="offer-main"><span class="site-tag ${o.site}">${siteName(o.site)}</span>
      <a href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener">${esc(o.name)} ↗</a>
      ${priceBlock(o)} <span class="pill ${o.match_status === 'accepted' ? 'ok' : o.match_status === 'review' ? 'warn' : 'bad'}">${t('match.' + o.match_status)}</span>
      ${diffInfo(p.invoice_price, o.price).html}</div>
    <p class="comment">${esc(o.comment)}</p>${decisionButtons(o)}</div>`;
}
function renderResults(entries) {
  const filter = $('#report-filter').value || 'all';
  const shown = entries.filter(e => filter === 'all' || e.matching?.status === filter);
  $('#results-empty').hidden = shown.length > 0;
  $('#results-empty').textContent = entries.length ? t('results.noneMatch') : t('results.empty');
  $('#reports').innerHTML = shown.map(entry => {
    const {product: p, estimate: e, sources, offers} = entry;
    const waiting = offers.some(o => o.match_status === 'review');
    const status = e.value != null ? ['ok', t('est.' + e.status)] : waiting ? ['warn', t('est.toReview')] : ['bad', t('est.unavailable')];
    const siteCell = site => e.per_site[site] != null ? money(e.per_site[site])
      : sources[site] ? `<span class="muted">${esc(sources[site].status === 'ok' ? '—' : stateLabel(sources[site].status))}</span>` : '—';
    const delta = e.invoice_delta_percent == null ? null : Math.round(e.invoice_delta_percent * 10) / 10;
    const open = expanded.has(p.id);
    const rejected = offers.filter(o => o.match_status === 'rejected');
    const visible = offers.filter(o => o.match_status !== 'rejected').sort((a, b) => (a.match_status === 'review' ? 0 : 1) - (b.match_status === 'review' ? 0 : 1));
    return `<tr class="row ${open ? 'open' : ''}" data-expand="${p.id}">
        <td><span class="caret">${open ? '▾' : '▸'}</span><strong>${esc(p.name)}</strong><small>${esc(p.manufacturer)} · ${t('results.listings', {n: offers.length})}</small></td>
        <td class="num">${money(p.invoice_price)}</td><td class="num">${siteCell('arzon')}</td><td class="num">${siteCell('oson')}</td>
        <td class="num"><strong>${money(e.value)}</strong></td>
        <td class="num">${optimalCell(entry)}</td>
        <td class="num">${delta == null ? '—' : `<span class="diff ${delta < 0 ? 'cheaper' : delta > 0 ? 'pricier' : 'same'}">${delta > 0 ? '+' : ''}${delta}%</span>`}</td>
        <td><span class="pill ${status[0]}">${esc(status[1])}</span></td></tr>
      ${open ? `<tr class="detail"><td colspan="8">${entry.advice ? `<div class="advice"><b>${t('advice.label')}: ${money(entry.advice.price)}</b>
          <span class="muted">${t('advice.range', {a: money(entry.advice.low), b: money(entry.advice.high)})}</span><p>${esc(entry.advice.reason)}</p></div>` : ''}
        ${visible.map(o => offerLine(p, o)).join('') || `<p class="muted">${t('results.noListings')}</p>`}
        ${rejected.length ? `<details data-rejected="${p.id}" ${openRejected.has(p.id) ? 'open' : ''}><summary>${t('results.showRejected', {n: rejected.length})}</summary>${rejected.map(o => offerLine(p, o)).join('')}</details>` : ''}
      </td></tr>` : ''}`;
  }).join('');
}
// AI price advice needs confirmed competitor prices; our Excel price is optional.
const canAdvise = entry => entry.offers.some(o => o.match_status === 'accepted' && o.price > 0);
function optimalCell(entry) {
  if (entry.advice) return `<strong class="optimal">${money(entry.advice.price)}</strong><button class="link small" data-advice="${entry.product.id}">${t('advice.recalc')}</button>`;
  if (!canAdvise(entry)) return '<span class="muted">—</span>';
  return aiConfigured ? `<button class="ghost small" data-advice="${entry.product.id}">${t('advice.calc')}</button>` : '<span class="muted">—</span>';
}
async function requestAdvice(productId) {
  return post(`/api/products/${productId}/optimal-price` + (currentRun ? '?run_id=' + currentRun : ''), {});
}
function renderReport(report) { currentReport = report; renderReview(); renderResults(report.products); renderNext(); }
async function loadReport(force = false) {
  const report = await api('/api/report' + (currentRun ? '?run_id=' + currentRun : ''));
  const json = JSON.stringify(report);
  if (!force && (json === lastReportJson || editingPrice !== null)) return;
  lastReportJson = json;
  renderReport(report);
}

/* ---------- browser viewer ---------- */
function closeViewer() { if (viewerSocket) { viewerSocket.close(); viewerSocket = null; } viewerSite = null; if ($('#browser-dialog').open) $('#browser-dialog').close(); }
function sendBrowser(command) { if (viewerSocket?.readyState === WebSocket.OPEN) viewerSocket.send(JSON.stringify(command)); }
async function openBrowser(site) {
  toast(t('connect.opening'));
  await post(`/api/browsers/${site}/open`, {});
  await refreshBrowsers();
  closeViewer();
  viewerSite = site;
  $('#browser-title').textContent = t('viewer.title', {site: siteName(site)});
  $('#remote-screen').removeAttribute('src');
  $('#browser-dialog').showModal();
  $('#browser-notice').textContent = t('viewer.loading');
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  viewerSocket = new WebSocket(`${scheme}//${location.host}/api/browsers/${site}/stream`);
  viewerSocket.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.type === 'frame') {
      $('#remote-screen').src = 'data:image/jpeg;base64,' + data.image;
      $('#browser-url').textContent = data.url;
      $('#browser-notice').textContent = t('viewer.hint');
    } else $('#browser-notice').textContent = data.message;
  };
  viewerSocket.onclose = () => { $('#browser-notice').textContent = t('viewer.disconnected'); };
}

/* ---------- events ---------- */
document.addEventListener('click', async e => {
  const el = e.target.closest('[data-tab],[data-goto],[data-lang],[data-review],[data-advice],[data-browser-open],[data-browser-check],[data-ai],[data-price-edit],[data-price-cancel],[data-price-reset],[data-expand]');
  if (!el) return;
  if (el.dataset.tab) return showTab(el.dataset.tab);
  if (el.dataset.goto) return showTab(el.dataset.goto);
  if (el.dataset.lang) { lang = el.dataset.lang; saved.set('dori-lang', lang); return applyLanguage(); }
  if (el.dataset.priceEdit || el.dataset.priceCancel) {
    editingPrice = el.dataset.priceEdit ? Number(el.dataset.priceEdit) : null;
    renderReport(currentReport);
    document.querySelector('.price-form input')?.select();
    return;
  }
  if (el.dataset.expand) {
    if (e.target.closest('a,button,details')) return;
    const id = Number(el.dataset.expand);
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    return renderResults(currentReport.products);
  }
  el.disabled = true;
  try {
    if (el.dataset.review) {
      await post(`/api/offers/${el.dataset.review}/review`, {status: el.dataset.status});
      await loadReport(true); lastStatsJson = ''; await loadStatistics();
    } else if (el.dataset.advice) {
      await requestAdvice(el.dataset.advice);
      await loadReport(true);
    } else if (el.dataset.priceReset) {
      await post(`/api/offers/${el.dataset.priceReset}/price`, {price: null});
      editingPrice = null; await loadReport(true); toast(t('price.saved'));
    } else if (el.dataset.browserOpen) await openBrowser(el.dataset.browserOpen);
    else if (el.dataset.browserCheck) await checkConnection(el.dataset.browserCheck);
    else if (el.dataset.ai) {
      if (!confirm(t('ai.confirm'))) return;
      const r = await post(`/api/products/${el.dataset.ai}/suggest-attributes`, {});
      $('#ai-result').textContent = JSON.stringify(r.suggestion, null, 2);
      $('#ai-dialog').showModal();
    }
  } catch (x) { toast(x.message); } finally { el.disabled = false; }
});
document.addEventListener('submit', async e => {
  const form = e.target.closest('[data-price-form]');
  if (!form) return;
  e.preventDefault();
  const price = Number(form.elements.price.value);
  if (!(price > 0)) { toast(t('price.invalid')); return; }
  try {
    await post(`/api/offers/${form.dataset.priceForm}/price`, {price});
    editingPrice = null; await loadReport(true); toast(t('price.saved'));
  } catch (x) { toast(x.message); }
});
// Keep "rejected" lists open across the 4-second refresh.
document.addEventListener('toggle', e => {
  const id = Number(e.target.dataset?.rejected);
  if (id) e.target.open ? openRejected.add(id) : openRejected.delete(id);
}, true);
document.addEventListener('change', e => {
  if (e.target.dataset.select) {
    const id = Number(e.target.dataset.select);
    e.target.checked ? selected.add(id) : selected.delete(id);
    renderProducts(); renderNext();
  } else if (e.target.dataset.siteToggle) {
    e.target.checked ? sites.add(e.target.dataset.siteToggle) : sites.delete(e.target.dataset.siteToggle);
    renderCollect(); renderNext();
  }
});
$('#all-products').onchange = e => { selected = e.target.checked ? new Set(products.map(p => p.id)) : new Set(); renderProducts(); renderNext(); };
$('#search').oninput = renderProducts;
$('#to-collect').onclick = () => showTab('collect');
$('#report-filter').onchange = () => currentReport && renderResults(currentReport.products);
$('#run-select').onchange = async e => { currentRun = Number(e.target.value) || null; expanded.clear(); try { await refreshRuns(); } catch (x) { toast(x.message); } };
$('#upload-form').onsubmit = async e => {
  e.preventDefault();
  const b = e.submitter; b.disabled = true;
  try {
    const f = new FormData(); f.append('file', $('#file').files[0]);
    const r = await api('/api/import', {method: 'POST', body: f});
    selected = new Set(r.product_ids.slice(0, 5));
    await loadProducts();
    toast(t('products.imported', {n: r.count, k: selected.size}));
  } catch (x) { toast(x.message); } finally { b.disabled = false; }
};
$('#manual-form').onsubmit = async e => {
  e.preventDefault();
  try {
    const d = Object.fromEntries(new FormData(e.target));
    d.invoice_price = d.invoice_price === '' ? null : Number(d.invoice_price);
    const p = await post('/api/products', d);
    selected.add(p.id); await loadProducts(); e.target.reset(); toast(t('products.added'));
  } catch (x) { toast(x.message); }
};
$('#crawl').onclick = async () => {
  try {
    $('#crawl').disabled = true;
    const r = await post('/api/crawl', {product_ids: [...selected], sites: chosenSites(), use_ai: $('#use-ai').checked});
    currentRun = r.run_id; await refreshRuns(); toast(t('collect.started'));
  } catch (x) { toast(x.message); renderCollect(); }
};
$('#advice-all').onclick = async () => {
  if (!aiConfigured) { toast(t('connect.aiOff')); return; }
  const todo = (currentReport?.products || []).filter(e => canAdvise(e) && !e.advice);
  if (!todo.length) { toast(t('advice.none')); return; }
  $('#advice-all').disabled = true;
  let done = 0;
  try {
    for (const entry of todo) {
      toast(t('advice.progress', {i: done + 1, n: todo.length}));
      await requestAdvice(entry.product.id);
      done++;
    }
    toast(t('advice.done', {n: done}));
  } catch (x) { toast(x.message); } finally { $('#advice-all').disabled = false; await loadReport(true); }
};
$('#cancel-run').onclick = async () => { try { await post(`/api/runs/${currentRun}/cancel`, {}); await refreshRuns(); toast(t('collect.cancelled')); } catch (x) { toast(x.message); } };
$('#logout').onclick = async () => { await post('/api/logout', {}); location.assign('/login'); };
$('#close-dialog').onclick = () => $('#ai-dialog').close();
$('#browser-close').onclick = closeViewer;
$('#browser-dialog').addEventListener('cancel', closeViewer);
$('#browser-home').onclick = () => sendBrowser({type: 'home'});
$('#browser-reload').onclick = () => sendBrowser({type: 'reload'});
$('#browser-enter').onclick = () => sendBrowser({type: 'key', key: 'Enter'});
$('#browser-tab').onclick = () => sendBrowser({type: 'key', key: 'Tab'});
$('#browser-check').onclick = async e => { e.target.disabled = true; try { await checkConnection(viewerSite); } catch (x) { toast(x.message); } finally { e.target.disabled = false; } };
$('#browser-text-form').onsubmit = e => { e.preventDefault(); sendBrowser({type: 'text', text: $('#browser-text').value}); $('#browser-text').value = ''; };
$('#remote-screen').onclick = e => {
  const rect = e.target.getBoundingClientRect();
  sendBrowser({type: 'click', x: (e.clientX - rect.left) * 1280 / rect.width, y: (e.clientY - rect.top) * 800 / rect.height});
  $('#remote-control').focus();
};
$('#remote-control').addEventListener('wheel', e => { e.preventDefault(); sendBrowser({type: 'scroll', delta: e.deltaY}); }, {passive: false});
$('#remote-control').onkeydown = e => {
  if (e.key === 'Escape') return;
  let key = e.key;
  if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === 'a') key = 'Control+A';
  else if (e.shiftKey && key === 'Tab') key = 'Shift+Tab';
  else if (key === ' ') key = 'Space';
  const keys = ['Enter', 'Tab', 'Backspace', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'Space', 'Control+A', 'Shift+Tab'];
  if (keys.includes(key)) { e.preventDefault(); sendBrowser({type: 'key', key}); }
  else if (key.length === 1 && !e.ctrlKey && !e.metaKey) { e.preventDefault(); sendBrowser({type: 'text', text: key}); }
};
$('#remote-control').onpaste = e => { e.preventDefault(); sendBrowser({type: 'text', text: e.clipboardData.getData('text').slice(0, 2000)}); };

/* ---------- start ---------- */
(async () => {
  applyLanguage();
  showTab(saved.get('dori-tab') || 'products');
  try {
    const h = await api('/api/health');
    aiConfigured = h.ai_configured;
    $('#use-ai').disabled = !aiConfigured;
    $('#use-ai').checked = Boolean(h.ai_default_enabled);
    $('#logout').hidden = !h.admin_password_set;
    applyLanguage();
    await loadProducts(); await refreshBrowsers(); await refreshRuns();
  } catch (x) { toast(x.message); }
  setInterval(async () => { try { await refreshBrowsers(); await refreshRuns(); } catch {} }, 4000);
})();
