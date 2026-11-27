(() => {
  const dialog = $('#agents-map-dialog');
  const canvas = $('#agents-map');
  const status = $('#agents-map-status');
  const unmapped = $('#agents-map-unmapped');
  let leaflet;
  let map;
  let request = 0;

  function loadAsset(tag, url, integrity) {
    return new Promise((resolve, reject) => {
      const asset = document.createElement(tag);
      asset.integrity = integrity;
      asset.crossOrigin = 'anonymous';
      if (tag === 'script') asset.src = url;
      else {
        asset.rel = 'stylesheet';
        asset.href = url;
      }
      asset.onload = resolve;
      asset.onerror = () => { asset.remove(); reject(new Error('Leaflet failed to load')); };
      document.head.append(asset);
    });
  }

  function loadLeaflet() {
    const base = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet';
    leaflet ??= Promise.all([
      loadAsset('link', base + '.css', 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY='),
      loadAsset('script', base + '.js', 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo='),
    ]).catch(error => { leaflet = null; throw error; });
    return leaflet;
  }

  function coordinates(point) {
    if (point?.lat == null || point?.lng == null) return null;
    const lat = Number(point.lat), lng = Number(point.lng);
    return Math.abs(lat) <= 90 && Math.abs(lng) <= 180 && (lat !== 0 || lng !== 0) ? [lat, lng] : null;
  }

  function mapEntries(branches, agents) {
    const branchesById = new Map(branches.map(branch => [branch.store_id, branch]));
    const locations = new Map();
    const missing = [];
    function add(point, label, detail, type) {
      if (!point) {
        missing.push(label + (detail ? ' · ' + detail : ''));
        return;
      }
      const key = point.join(',');
      if (!locations.has(key)) locations.set(key, {point, branch: [], agent: []});
      locations.get(key)[type].push({label, detail});
    }

    for (const branch of branches) {
      add(coordinates(branch), branch.store_name, branch.store_address, 'branch');
    }
    for (const agent of agents) {
      const branch = branchesById.get(agent.assignment?.store_id);
      const shared = coordinates(agent.device_location);
      const assigned = branch?.store_name || t('area.unassigned');
      const accuracy = agent.device_location?.accuracy;
      const detail = shared ? (accuracy == null ? assigned : assigned + ' · ' + t('agentMap.accuracy', {m: Math.round(accuracy)}))
        : assigned + ' · ' + t('agentMap.notShared');
      add(shared, agent.name, detail, 'agent');
    }
    return {locations: [...locations.values()], missing};
  }

  function markerPopup(location) {
    const content = document.createElement('div');
    for (const type of ['branch', 'agent']) {
      if (!location[type].length) continue;
      const heading = document.createElement('strong');
      heading.textContent = t('agentMap.' + type);
      const list = document.createElement('ul');
      for (const {label, detail} of location[type]) {
        const item = document.createElement('li');
        item.textContent = detail ? label + ' · ' + detail : label;
        list.append(item);
      }
      content.append(heading, list);
    }
    return content;
  }

  function draw(locations) {
    canvas.hidden = false;
    const L = window.L;
    map = L.map(canvas, {maxZoom: 19});
    const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, referrerPolicy: 'strict-origin-when-cross-origin',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    });
    tiles.once('tileerror', () => { status.textContent = t('agentMap.tilesFailed'); });
    tiles.addTo(map);
    for (const location of locations) {
      const count = location.agent.length;
      const type = location.branch.length ? (count ? 'both' : 'branch') : 'agent';
      const marker = L.divIcon({className: 'agent-map-marker ' + type, html: count || '•', iconSize: [29, 29]});
      const title = [...location.branch, ...location.agent].map(item => item.label).join(', ');
      L.marker(location.point, {icon: marker, title, alt: title}).addTo(map).bindPopup(markerPopup(location));
    }
    map.fitBounds(locations.map(location => location.point), {padding: [28, 28], maxZoom: 15});
  }

  $('#agents-map-open').addEventListener('click', async () => {
    const opening = ++request;
    canvas.hidden = unmapped.hidden = true;
    status.textContent = t('agentMap.loading');
    dialog.showModal();
    try {
      const [stores, agents] = await Promise.all([api('/api/dorixona/stores'), api('/api/agents')]);
      if (opening !== request) return;
      const {locations, missing} = mapEntries(stores.items, agents);
      if (missing.length) {
        const list = unmapped.querySelector('ul');
        list.replaceChildren(...missing.map(label => {
          const item = document.createElement('li');
          item.textContent = label;
          return item;
        }));
        unmapped.hidden = false;
      }
      if (!locations.length) {
        status.textContent = t('agentMap.empty');
        return;
      }
      await loadLeaflet();
      if (opening !== request) return;
      status.textContent = '';
      draw(locations);
    } catch (_) {
      if (opening === request) status.textContent = t('agentMap.failed');
    }
  });

  dialog.addEventListener('close', () => {
    request++;
    if (map) map.remove();
    map = null;
  });
})();
