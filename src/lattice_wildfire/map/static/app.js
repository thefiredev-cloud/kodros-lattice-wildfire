/* Lattice wildfire picture: read-only Leaflet front end.
 *
 * Data arrives as GeoJSON from /api/picture and tasks from /api/tasks. Nothing here publishes,
 * edits or writes anything. Every piece of text that comes from Lattice is inserted as a text
 * node, including analyst brief markdown: no HTML string is ever parsed.
 */
/* global L */
(() => {
  'use strict';

  const REFRESH_MS = 30000;
  const FETCH_TIMEOUT_MS = 20000;
  const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  const TILE_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  // Used for tiles that fail to load (offline box) so the plain background shows through.
  const EMPTY_TILE =
    'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  const DEFAULT_VIEW = [34.2, -118.3];
  const DEFAULT_ZOOM = 9;

  // Offline geographic context: California county outlines, vendored (US Census, public domain).
  const COUNTY_URL = '/vendor/census/ca-counties-5m.geojson';
  const LA_COUNTY_GEOID = '06037';
  const COUNTY_STYLE = { color: '#7c8ba1', weight: 1, opacity: 0.55, fill: false, interactive: false };
  const LA_COUNTY_STYLE = { color: '#cbd5e1', weight: 2.2, opacity: 0.9, fill: false, interactive: false };

  const LAYER_INFO = {
    hotspot: { label: 'VIIRS hotspots', shape: 'dot' },
    incident: { label: 'Incidents', shape: 'triangle' },
    perimeter: { label: 'Perimeters', shape: 'polygon' },
    'red-flag': { label: 'Red flag warnings', shape: 'dashed' },
    station: { label: 'Fire stations', shape: 'square' },
    candidate: { label: 'New start candidates', shape: 'diamond' },
    analyst: { label: 'AI analysts', shape: 'ring' },
    other: { label: 'Other entities', shape: 'dot' },
  };

  const COLORS = {
    hotspot: { fill: '#f59e0b', stroke: '#111827' },
    incident: { fill: '#dc2626', stroke: '#ffffff' },
    station: { fill: '#2563eb', stroke: '#ffffff' },
    candidate: { fill: '#c026d3', stroke: '#ffffff' },
    analyst: { fill: '#059669', stroke: '#ffffff' },
    other: { fill: '#64748b', stroke: '#ffffff' },
    perimeter: { color: '#f97316', fill: '#f97316' },
    'red-flag': { color: '#dc2626', fill: '#dc2626' },
  };

  const STATUS_LABEL = {
    executing: 'running',
    done_ok: 'done · ok',
    done_not_ok: 'done · failed',
    replaced: 'replaced',
    version_rejected: 'version rejected',
    pending: 'pending',
    scheduled: 'scheduled',
    cancelled: 'cancelled',
    dispatched: 'dispatched',
  };

  const $ = (id) => document.getElementById(id);
  const statusEl = $('status');
  const bannerEl = $('banner');
  const refreshButton = $('refresh');
  const candidatesEl = $('candidates');
  const candidatesHint = $('candidates-hint');
  const tasksEl = $('tasks');
  const tasksHint = $('tasks-hint');
  const briefEl = $('brief');
  const briefMeta = $('brief-meta');

  const state = {
    tasks: [],
    picture: null,
    aoi: null,
    selectedEntityId: null,
    selectedTaskId: null,
    fitted: false,
  };

  // ---------------------------------------------------------------- helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  function clock(date) {
    const d = date instanceof Date ? date : new Date(date || Date.now());
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function shortTime(value) {
    if (!value) return 'unknown time';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function number(value, digits) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(digits) : null;
  }

  function statusLabel(status) {
    if (!status) return 'unknown status';
    const key = String(status).replace(/^STATUS_/, '').toLowerCase();
    return STATUS_LABEL[key] || key.replace(/_/g, ' ');
  }

  function statusKind(status) {
    const key = String(status || '').replace(/^STATUS_/, '').toLowerCase();
    if (key === 'done_ok') return 'ok';
    if (key === 'done_not_ok' || key === 'version_rejected') return 'bad';
    if (key === 'executing' || key === 'dispatched') return 'active';
    return 'idle';
  }

  async function requestText(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        signal: controller.signal,
        headers: { Accept: 'text/markdown, text/plain;q=0.9, */*;q=0.5' },
        cache: 'no-store',
      });
      const body = await response.text();
      if (!response.ok) {
        let message = `${response.status} ${response.statusText}`;
        try {
          const parsed = JSON.parse(body);
          if (parsed && parsed.error) message = parsed.error;
        } catch (ignored) {
          /* not JSON; keep the HTTP status text */
        }
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return body;
    } finally {
      clearTimeout(timer);
    }
  }

  async function requestJson(url) {
    const body = await requestText(url);
    try {
      return body ? JSON.parse(body) : null;
    } catch (ignored) {
      throw new Error(`${url} did not return JSON`);
    }
  }

  function showBanner(message) {
    bannerEl.textContent = message;
    bannerEl.hidden = false;
  }

  function hideBanner() {
    bannerEl.hidden = true;
    bannerEl.textContent = '';
  }

  // ---------------------------------------------------------------- map

  const map = L.map('map', {
    center: DEFAULT_VIEW,
    zoom: DEFAULT_ZOOM,
    minZoom: 3,
    maxZoom: 18,
    worldCopyJump: true,
    zoomControl: true,
  });

  // Draw order: tiles (200) < county outline (300) < stations (350) < every other layer (400+).
  map.createPane('counties').style.zIndex = '300';
  map.createPane('counties').style.pointerEvents = 'none';
  map.createPane('stations').style.zIndex = '350';

  const tiles = L.tileLayer(TILE_URL, {
    maxZoom: 19,
    attribution: TILE_ATTRIBUTION,
    errorTileUrl: EMPTY_TILE,
    className: 'basemap-tiles',
  });
  tiles.addTo(map);

  const countyLayer = L.geoJSON(null, {
    pane: 'counties',
    interactive: false,
    style: (feature) =>
      feature && feature.properties && feature.properties.geoid === LA_COUNTY_GEOID
        ? LA_COUNTY_STYLE
        : COUNTY_STYLE,
  });
  countyLayer.addTo(map);
  let countyCount = 0;
  fetch(COUNTY_URL)
    .then((response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return response.json();
    })
    .then((document) => {
      countyLayer.addData(document);
      countyCount = ((document && document.features) || []).length;
      updateLegendCounts();
    })
    .catch(() => {
      /* the outline is optional; the picture still renders without it */
    });

  const groups = {};
  for (const key of Object.keys(LAYER_INFO)) {
    groups[key] = L.layerGroup().addTo(map);
  }
  const candidateLayers = new Map();

  L.control.scale({ position: 'bottomright', imperial: false, maxWidth: 160 }).addTo(map);

  // One control: legend, toggles and counts together. No separate Leaflet layers box.
  const legendCounts = {};
  const legend = L.control({ position: 'bottomleft' });
  legend.onAdd = () => {
    const box = L.DomUtil.create('div', 'legend');
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', 'Map layers and counts');
    box.appendChild(el('p', 'legend-title', 'Layers'));

    const addRow = (config) => {
      const row = el('label', 'legend-row');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'legend-check';
      input.checked = config.checked !== false;
      input.dataset.layer = config.key;
      const swatch = el('span', `swatch swatch-${config.shape} swatch-${config.key}`);
      swatch.setAttribute('aria-hidden', 'true');
      const count = el('span', 'legend-count', config.count === undefined ? '' : String(config.count));
      row.append(input, swatch, el('span', 'legend-label', config.label), count);
      input.addEventListener('change', () => config.onToggle(input.checked));
      box.appendChild(row);
      legendCounts[config.key] = count;
      return row;
    };

    addRow({
      key: 'basemap',
      shape: 'basemap',
      label: 'OpenStreetMap tiles',
      count: '',
      onToggle: (on) => (on ? tiles.addTo(map) : map.removeLayer(tiles)),
    });
    addRow({
      key: 'counties',
      shape: 'county',
      label: 'CA county lines',
      count: countyCount,
      onToggle: (on) => (on ? countyLayer.addTo(map) : map.removeLayer(countyLayer)),
    });
    for (const [key, info] of Object.entries(LAYER_INFO)) {
      addRow({
        key,
        shape: info.shape,
        label: info.label,
        count: 0,
        onToggle: (on) => (on ? groups[key].addTo(map) : map.removeLayer(groups[key])),
      });
    }
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    return box;
  };
  legend.addTo(map);

  function updateLegendCounts(counts) {
    for (const [key, node] of Object.entries(legendCounts)) {
      if (key === 'basemap') continue;
      if (key === 'counties') {
        node.textContent = countyCount ? String(countyCount) : '';
      } else if (counts) {
        node.textContent = String(counts[key] || 0);
      }
    }
  }

  function relayout() {
    map.invalidateSize({ animate: false });
  }

  window.addEventListener('resize', relayout);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(relayout).observe($('map'));
  }
  requestAnimationFrame(relayout);

  function pointStyle(layer, props) {
    const palette = COLORS[layer] || COLORS.other;
    if (layer === 'hotspot') {
      const frp = Number(props.frp_mw);
      const radius = Number.isFinite(frp) && frp > 0 ? Math.min(14, 3.5 + 10 * Math.min(1, Math.log10(1 + frp) / 2)) : 3.5;
      return {
        radius,
        color: palette.stroke,
        weight: 1.5,
        opacity: 0.95,
        fillColor: palette.fill,
        fillOpacity: 0.72,
      };
    }
    if (layer === 'candidate') {
      return CANDIDATE_STYLE.base;
    }
    if (layer === 'incident') {
      return { radius: 7, color: palette.stroke, weight: 2, opacity: 1, fillColor: palette.fill, fillOpacity: 0.85 };
    }
    return { radius: 5, color: palette.stroke, weight: 1.5, opacity: 0.9, fillColor: palette.fill, fillOpacity: 0.8 };
  }

  function polygonStyle(layer) {
    if (layer === 'red-flag') {
      return {
        color: COLORS['red-flag'].color,
        weight: 2,
        opacity: 0.95,
        dashArray: '6 4',
        fillColor: COLORS['red-flag'].fill,
        fillOpacity: 0.1,
        lineJoin: 'round',
      };
    }
    return {
      color: COLORS.perimeter.color,
      weight: 2,
      opacity: 0.9,
      fillColor: COLORS.perimeter.fill,
      fillOpacity: 0.14,
      lineJoin: 'round',
    };
  }

  function factLine(props) {
    const parts = [];
    parts.push(props.data_type);
    const score = number(props.score, 2);
    if (score !== null && props.layer === 'candidate') parts.push(`score ${score}`);
    const frp = number(props.frp_mw, 1);
    if (frp !== null) parts.push(`FRP ${frp} MW`);
    if (props.confidence) parts.push(`confidence ${props.confidence}`);
    if (props.station) parts.push(`station ${props.station}`);
    if (props.city) parts.push(String(props.city));
    if (props.expires) parts.push(`expires ${shortTime(props.expires)}`);
    return parts.join(' · ');
  }

  function tooltipNode(props) {
    const box = el('div', 'tip');
    box.appendChild(el('p', 'tip-name', props.name || props.entity_id));
    box.appendChild(el('p', 'tip-facts', factLine(props)));
    return box;
  }

  function popupNode(props) {
    const box = el('div', 'popup');
    box.appendChild(el('p', 'popup-name', props.name || props.entity_id));
    box.appendChild(el('p', 'popup-facts', factLine(props)));
    if (props.description) {
      const pre = el('pre', 'popup-description');
      pre.textContent = props.description;
      box.appendChild(pre);
    }
    box.appendChild(el('p', 'popup-id', props.entity_id));
    return box;
  }

  function buildLayer(feature) {
    const props = feature.properties || {};
    const geometry = feature.geometry || {};
    const layerKey = groups[props.layer] ? props.layer : 'other';
    let shape = null;
    if (geometry.type === 'Polygon' && Array.isArray(geometry.coordinates)) {
      const latlngs = geometry.coordinates.map((ring) => ring.map(([lon, lat]) => [lat, lon]));
      shape = L.polygon(latlngs, polygonStyle(layerKey));
    } else if (geometry.type === 'Point' && Array.isArray(geometry.coordinates)) {
      const [lon, lat] = geometry.coordinates;
      if (layerKey === 'station') {
        // Square marker in a pane below the hotspot pane, so stations read as ground context
        // while hotspots and candidates stay on top of them.
        const square = document.createElement('span');
        square.className = 'station-square';
        shape = L.marker([lat, lon], {
          pane: 'stations',
          keyboard: false,
          riseOnHover: false,
          title: props.name || props.entity_id,
          icon: L.divIcon({
            className: 'station-icon',
            html: square,
            iconSize: [9, 9],
            iconAnchor: [4.5, 4.5],
          }),
        });
      } else {
        shape = L.circleMarker([lat, lon], pointStyle(layerKey, props));
      }
    }
    if (!shape) return null;
    shape.bindTooltip(tooltipNode(props), { direction: 'top', offset: [0, -4], opacity: 0.96 });
    if (layerKey === 'candidate') {
      shape.on('click', () => selectCandidate(props.entity_id));
    } else {
      shape.bindPopup(popupNode(props), { maxWidth: 340, autoPan: true });
    }
    shape.addTo(groups[layerKey]);
    if (layerKey === 'candidate') candidateLayers.set(props.entity_id, shape);
    return shape;
  }

  function countsByLayer(features) {
    const counts = {};
    for (const key of Object.keys(groups)) counts[key] = 0;
    for (const feature of features) {
      const key = feature.properties && feature.properties.layer;
      if (key && key in counts) counts[key] += 1;
    }
    return counts;
  }

  function featureBounds(features) {
    const bounds = L.latLngBounds([]);
    for (const feature of features) {
      const geometry = feature.geometry || {};
      if (geometry.type === 'Point') {
        const [lon, lat] = geometry.coordinates;
        bounds.extend([lat, lon]);
      } else if (geometry.type === 'Polygon') {
        for (const ring of geometry.coordinates) {
          for (const [lon, lat] of ring) bounds.extend([lat, lon]);
        }
      }
    }
    return bounds.isValid() ? bounds : null;
  }

  function aoiBounds(aoi) {
    if (!aoi || !Number.isFinite(aoi.min_lat) || !Number.isFinite(aoi.max_lat)) return null;
    if (!Number.isFinite(aoi.min_lon) || !Number.isFinite(aoi.max_lon)) return null;
    if (aoi.min_lat >= aoi.max_lat || aoi.min_lon >= aoi.max_lon) return null;
    return L.latLngBounds([aoi.min_lat, aoi.min_lon], [aoi.max_lat, aoi.max_lon]);
  }

  // First view = the configured AOI from /api/health (the server's LW_AOI_BBOX), else feature bounds.
  function fitInitial() {
    const features = (state.picture && state.picture.features) || [];
    const bounds = aoiBounds(state.aoi) || featureBounds(features);
    if (!bounds) return;
    map.invalidateSize({ animate: false });
    map.fitBounds(bounds.pad(0.02), { maxZoom: 13 });
    state.fitted = true;
  }

  function applyPicture(picture) {
    state.picture = picture;
    const features = (picture && picture.features) || [];
    for (const group of Object.values(groups)) group.clearLayers();
    candidateLayers.clear();
    for (const feature of features) buildLayer(feature);
    updateLegendCounts(countsByLayer(features));
    if (!state.fitted) fitInitial();
    document.title = `Lattice wildfire picture · ${features.length} features`;
  }

  // ---------------------------------------------------------------- side panel

  function candidateRows() {
    const features = (state.picture && state.picture.features) || [];
    const rows = features
      .filter((feature) => feature.properties && feature.properties.layer === 'candidate')
      .map((feature) => feature.properties);
    rows.sort((a, b) => {
      const diff = (Number(b.score) || 0) - (Number(a.score) || 0);
      return diff !== 0 ? diff : String(a.entity_id).localeCompare(String(b.entity_id));
    });
    return rows;
  }

  function latestTaskFor(entityId) {
    return state.tasks.find((task) => task.objective_entity_id === entityId) || null;
  }

  function renderCandidates() {
    const rows = candidateRows();
    candidatesEl.replaceChildren();
    candidatesHint.hidden = rows.length > 0;
    if (!rows.length) {
      candidatesHint.textContent = 'No candidates in the picture right now.';
      return;
    }
    for (const props of rows) {
      const item = el('li');
      const button = el('button', 'row', null);
      button.type = 'button';
      button.dataset.entityId = props.entity_id;
      if (props.entity_id === state.selectedEntityId) button.classList.add('selected');
      button.appendChild(el('span', 'row-name', props.name || props.entity_id));
      const score = number(props.score, 2);
      const facts = [`score ${score === null ? 'n/a' : score}`];
      if (props.sensor_hits) facts.push(`${props.sensor_hits} detection${props.sensor_hits === 1 ? '' : 's'}`);
      const task = latestTaskFor(props.entity_id);
      facts.push(task ? `task ${statusLabel(task.status)}` : 'no task yet');
      button.appendChild(el('span', 'row-meta', facts.join(' · ')));
      button.addEventListener('click', () => selectCandidate(props.entity_id));
      item.appendChild(button);
      candidatesEl.appendChild(item);
    }
  }

  function renderTasks() {
    tasksEl.replaceChildren();
    tasksHint.hidden = state.tasks.length > 0;
    if (!state.tasks.length) {
      tasksHint.textContent = 'No tasks in this environment.';
      return;
    }
    for (const task of state.tasks) {
      const item = el('li');
      const button = el('button', 'row', null);
      button.type = 'button';
      button.dataset.taskId = task.task_id;
      if (task.task_id === state.selectedTaskId) button.classList.add('selected');
      button.appendChild(el('span', 'row-name', task.display_name || task.task_id));
      const chip = el('span', `chip chip-${statusKind(task.status)}`, statusLabel(task.status));
      const meta = el('span', 'row-meta');
      meta.appendChild(chip);
      meta.appendChild(document.createTextNode(` · ${shortTime(task.created)}`));
      if (task.objective_entity_id) meta.appendChild(document.createTextNode(` · ${task.objective_entity_id}`));
      button.appendChild(meta);
      button.addEventListener('click', () => selectTask(task));
      item.appendChild(button);
      tasksEl.appendChild(item);
    }
  }

  function markSelected() {
    for (const button of candidatesEl.querySelectorAll('button.row')) {
      button.classList.toggle('selected', button.dataset.entityId === state.selectedEntityId);
    }
    for (const button of tasksEl.querySelectorAll('button.row')) {
      button.classList.toggle('selected', button.dataset.taskId === state.selectedTaskId);
    }
    highlightCandidates();
  }

  const CANDIDATE_STYLE = {
    base: { radius: 8, color: '#ffffff', weight: 2.5, opacity: 1, fillColor: '#c026d3', fillOpacity: 0.92 },
    selected: { radius: 12, color: '#f0abfc', weight: 3.5, opacity: 1, fillColor: '#c026d3', fillOpacity: 1 },
  };

  function highlightCandidates() {
    for (const [entityId, layer] of candidateLayers) {
      const selected = entityId === state.selectedEntityId;
      layer.setStyle(selected ? CANDIDATE_STYLE.selected : CANDIDATE_STYLE.base);
      if (selected) layer.bringToFront();
    }
  }

  function focusCandidate(entityId) {
    const layer = entityId ? candidateLayers.get(entityId) : null;
    if (!layer || typeof layer.getLatLng !== 'function') return;
    map.flyTo(layer.getLatLng(), 11, { duration: 0.6 });
  }

  function revealRow(container, datasetKey, value) {
    if (!value) return;
    for (const button of container.querySelectorAll('button.row')) {
      if (button.dataset[datasetKey] === value) {
        button.scrollIntoView({ block: 'nearest' });
        return;
      }
    }
  }

  function setBriefMeta(text) {
    briefMeta.textContent = text;
  }

  function setBriefState(kind, text) {
    briefEl.setAttribute('aria-busy', kind === 'loading' ? 'true' : 'false');
    briefEl.replaceChildren(el('p', kind === 'loading' ? 'brief-note is-loading' : 'brief-note', text));
  }

  const RUNNING_STATUSES = new Set(['created', 'pending', 'scheduled', 'dispatched', 'executing']);

  function briefMissingText(task) {
    const key = String(task.status || '').replace(/^STATUS_/, '').toLowerCase();
    if (!key || RUNNING_STATUSES.has(key)) {
      return `No brief yet: task is ${statusLabel(task.status)}. The analyst writes it when it finishes.`;
    }
    return `No brief yet: task ended ${statusLabel(task.status)} without an uploaded brief.`;
  }

  // Markdown -> DOM. Every string goes in via textContent; no HTML is ever parsed.
  function inlineInto(target, text) {
    const parts = String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
    for (const part of parts) {
      if (!part) continue;
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
        const strong = document.createElement('strong');
        strong.textContent = part.slice(2, -2);
        target.appendChild(strong);
      } else if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        const code = document.createElement('code');
        code.textContent = part.slice(1, -1);
        target.appendChild(code);
      } else {
        target.appendChild(document.createTextNode(part));
      }
    }
  }

  function renderMarkdown(markdown) {
    briefEl.replaceChildren();
    let list = null;
    let listTag = null;
    for (const raw of String(markdown).split(/\r?\n/)) {
      const line = raw.replace(/\s+$/, '');
      if (!line.trim()) {
        list = null;
        listTag = null;
        continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        list = null;
        listTag = null;
        const level = Math.min(6, heading[1].length + 2);
        const node = document.createElement(`h${level}`);
        inlineInto(node, heading[2]);
        briefEl.appendChild(node);
        continue;
      }
      const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || ordered) {
        const tag = bullet ? 'ul' : 'ol';
        if (!list || listTag !== tag) {
          list = document.createElement(tag);
          listTag = tag;
          briefEl.appendChild(list);
        }
        const item = document.createElement('li');
        inlineInto(item, (bullet || ordered)[1]);
        list.appendChild(item);
        continue;
      }
      const paragraph = el('p');
      inlineInto(paragraph, line);
      briefEl.appendChild(paragraph);
      list = null;
      listTag = null;
    }
    if (!briefEl.childNodes.length) setBriefState('note', 'This brief is empty.');
  }

  async function openBrief(task, heading) {
    state.selectedTaskId = task.task_id;
    setBriefMeta(`${heading || task.display_name || task.task_id} · ${statusLabel(task.status)} · task ${task.task_id}`);
    setBriefState('loading', 'Loading brief…');
    try {
      const markdown = await requestText(`/api/brief/${encodeURIComponent(task.task_id)}`);
      briefEl.setAttribute('aria-busy', 'false');
      renderMarkdown(markdown);
    } catch (error) {
      if (error.status === 404) {
        setBriefState('note', briefMissingText(task));
      } else {
        setBriefState('note', `Could not load the brief: ${error.message}`);
      }
    }
  }

  function selectCandidate(entityId) {
    if (!entityId) return;
    state.selectedEntityId = entityId;
    state.selectedTaskId = null;
    markSelected();
    revealRow(candidatesEl, 'entityId', entityId);
    focusCandidate(entityId);
    const props = candidateRows().find((row) => row.entity_id === entityId);
    const task = latestTaskFor(entityId);
    if (!task) {
      setBriefMeta(`${(props && props.name) || entityId} · no task yet`);
      setBriefState('note', 'No brief yet: no Investigate task targets this candidate.');
      return;
    }
    openBrief(task, (props && props.name) || entityId);
  }

  function selectTask(task) {
    state.selectedEntityId = task.objective_entity_id || null;
    openBrief(task);
    markSelected();
    revealRow(tasksEl, 'taskId', task.task_id);
    if (state.selectedEntityId) {
      revealRow(candidatesEl, 'entityId', state.selectedEntityId);
      focusCandidate(state.selectedEntityId);
    }
  }

  // ---------------------------------------------------------------- refresh

  function applyTasks(payload) {
    state.tasks = (payload && payload.tasks) || [];
  }

  async function refresh() {
    statusEl.textContent = 'refreshing…';
    refreshButton.disabled = true;
    try {
      const [health, picture, taskPayload] = await Promise.all([
        requestJson('/api/health').catch(() => null),
        requestJson('/api/picture'),
        requestJson('/api/tasks'),
      ]);
      if (!picture || picture.type !== 'FeatureCollection') {
        throw new Error('/api/picture did not return a FeatureCollection');
      }
      if (health && health.aoi) state.aoi = health.aoi;
      applyPicture(picture);
      map.invalidateSize({ animate: false });
      applyTasks(taskPayload);
      renderCandidates();
      renderTasks();
      markSelected();
      const meta = picture.meta || {};
      statusEl.textContent = `updated ${clock()} · ${meta.count || 0} features · ${state.tasks.length} tasks`;
      hideBanner();
    } catch (error) {
      showBanner(`Picture unavailable: ${error.message}`);
      statusEl.textContent = `stale · ${clock()}`;
    } finally {
      refreshButton.disabled = false;
    }
  }

  refreshButton.addEventListener('click', refresh);
  window.addEventListener('online', refresh);
  window.addEventListener('focus', () => {
    if (!state.picture) refresh();
  });

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
