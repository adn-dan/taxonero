/* ============================== Wikipedia panel ============================== */
const W = { key: null, src: 'wikipedia', lang: '' };
async function openWiki(key, src) {
  if (!key) return;
  if (key !== W.key) W.lang = LANG;   // new article: interface language (the menu allows changing it)
  W.key = key; W.src = src || W.src;
  $('#wiki').classList.add('on');
  $$('#wiki .wbar .chip').forEach(b => b.classList.toggle('on', b.dataset.src === W.src));
  $('#wikibody').innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  const label = W.src === 'wikispecies' ? 'Wikispecies' : t('wiki.wikipedia');
  try {
    const a = await api(`/api/wiki/${key}?src=${W.src}&lang=${W.lang}`);
    $('#wikilang').style.display = W.src === 'wikipedia' ? '' : 'none';
    $('#wikilang').innerHTML = a.languages.map(l => `<option value="${esc(l)}" ${l === (a.lang || W.lang) ? 'selected' : ''}>${esc(langName(l))}</option>`).join('');
    $('#wikibody').innerHTML = `<h1>${esc(a.title)}</h1><a class="readon" href="${esc(a.url)}" target="_blank" rel="noopener">${t('wiki.read_on', { site: label })} ↗</a>
      ${a.thumbnail && W.src === 'wikipedia' ? `<img class="hero" src="${esc(a.thumbnail)}" alt="">` : ''}
      ${a.html || `<p><i>${t('wiki.no_text')}</i></p>`}`;
    $('#wikibody').scrollTop = 0;
  } catch (e) { $('#wikibody').innerHTML = `<div class="empty">${t('wiki.no_page', { site: label })}</div>`; }
}
function closeWiki() { $('#wiki').classList.remove('on'); }
$$('#wiki .wbar .chip').forEach(b => b.onclick = () => openWiki(W.key, b.dataset.src));
$('#wikilang').onchange = e => { W.lang = e.target.value; openWiki(W.key); };
$('#wiki-x').onclick = closeWiki;

/* ============================== range maps ============================== */
// Local data in compact format (per cell: GBIF tile, column, half-row, observations), snapped to the
// exact GBIF grid (see ranges.py) then drawn by a canvas layer, tile by tile: fast even
// with tens of thousands of cells, fixed size and colours whatever the zoom. The grids of two
// neighbouring GBIF tiles do not join: each cell is clipped at the edge of its tile, as on GBIF.
// Single-hue sequential scale, rounded logarithmic classes.
const RANGE_COLORS = ['#edf8e9', '#bae4b3', '#74c476', '#31a354', '#006d2c'];
function niceBreaks(max) {
  const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1e6];
  const out = [];
  for (let i = 1; i <= 4; i++) {
    const target = Math.pow(Math.max(max, 2), i / 5);
    const b = nice.reduce((best, v) => Math.abs(Math.log(v) - Math.log(target)) < Math.abs(Math.log(best) - Math.log(target)) ? v : best, 1);
    if (b > (out[out.length - 1] || 1)) out.push(b);
  }
  return out;
}
const classOf = (n, br) => br.filter(b => n >= b).length;
const rangeData = new Map();   // GBIF key -> promise of the data (a single request per taxon and per session)
function getRange(key) {
  if (!rangeData.has(key)) rangeData.set(key, api(`/api/range/${key}`).then(prepRange).catch(e => { rangeData.delete(key); throw e; }));
  return rangeData.get(key);
}
function prepRange(d) {
  const n = 2 ** d.z, E = d.tile, W = E * n, sx = E / (d.hex + 1), R = sx / 1.5, hy = Math.sqrt(3) * R / 2;
  const max = d.cells.reduce((m, c) => Math.max(m, c[3]), 0), br = niceBreaks(max);
  // [x, y, observations, colour class, tile] in normalised Web Mercator, grouped by tile and sorted by x.
  d.cells = d.cells.map(([t, i, h, nb]) => [(Math.floor(t / n) * E + i * sx) / W, ((t % n) * E + d.tiles[t] + h * hy) / W, nb, classOf(nb, br), t]);
  d.cells.sort((a, b) => a[0] - b[0]);
  d.groups = new Map();
  d.cells.forEach(c => { if (!d.groups.has(c[4])) d.groups.set(c[4], []); d.groups.get(c[4]).push(c); });
  d.n = n; d.r = R / W;
  return Object.assign(d, { max, breaks: br });
}
// Rectangle of GBIF tile t, in normalised Web Mercator.
const tileRect = (d, t) => { const tx = Math.floor(t / d.n), ty = t % d.n; return [tx / d.n, ty / d.n, (tx + 1) / d.n, (ty + 1) / d.n]; };
function lowerBound(cells, x) { let lo = 0, hi = cells.length; while (lo < hi) { const m = (lo + hi) >> 1; if (cells[m][0] < x) lo = m + 1; else hi = m; } return lo; }
// Size on screen: the local grid as long as its cells are smaller than ZOOM_HEX_PX; beyond, the GBIF tile of the
// displayed zoom (cells of ~11 px at every zoom), asked from the server on the fly. Colours: observations in
// the displayed cell, same classes as the legend (relative to the area, a single observation in a
// small cell turned to the darkest). No border, at every zoom.
const ZOOM_HEX_PX = 14;
const hexPath = (ctx, px, py, R) => { ctx.beginPath();
  for (let k = 0; k < 6; k++) { const X = px + R * Math.cos(k * Math.PI / 3), Y = py + R * Math.sin(k * Math.PI / 3); k ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); }
  ctx.closePath(); };
const zoomTiles = new Map();   // "key/z/x/y" -> promise of the cells of the tile (one request per tile and per session)
function getZoomTile(key, z, x, y) {
  const id = `${key}/${z}/${x}/${y}`;
  if (!zoomTiles.has(id)) zoomTiles.set(id, api(`/api/range/${id}`).catch(e => { zoomTiles.delete(id); throw e; }));
  return zoomTiles.get(id);
}
const HexLayer = L.GridLayer.extend({
  initialize(data, key, opts) { this.d = data; this.key = key; this.zt = new Map(); L.setOptions(this, { opacity: 0.8, zIndex: 350, ...opts }); },
  createTile(coords, done) {
    const size = this.getTileSize(), dpr = window.devicePixelRatio || 1, d = this.d;
    const tile = document.createElement('canvas'); tile.width = size.x * dpr; tile.height = size.y * dpr;
    const ctx = tile.getContext('2d'); ctx.scale(dpr, dpr); ctx.lineWidth = 0.8; ctx.lineJoin = 'round';
    const n = 2 ** coords.z, x = ((coords.x % n) + n) % n;
    if (!this.key || 2 * d.r * size.x * n <= ZOOM_HEX_PX) { this.drawLocal(ctx, coords, size); setTimeout(() => done(null, tile)); return tile; }
    getZoomTile(this.key, coords.z, x, coords.y).then(t => {
      this.zt.set(`${coords.z}/${x}/${coords.y}`, t);
      const sx = 4096 / (t.hex + 1), hy = Math.sqrt(3) * sx / 3, k = size.x / 4096, R = sx / 1.5 * k;
      for (const [i, h, nb] of t.cells) {
        hexPath(ctx, i * sx * k, (t.y0 + h * hy) * k, R);
        ctx.fillStyle = ctx.strokeStyle = RANGE_COLORS[classOf(nb, d.breaks)]; ctx.fill(); ctx.stroke();   // no border, like the local grid
      }
    }).catch(() => this.drawLocal(ctx, coords, size))   // GBIF injoignable : grille locale
      .finally(() => done(null, tile));
    return tile;
  },
  drawLocal(ctx, coords, size) {
    const d = this.d, n = 2 ** coords.z, world = size.x * n, tx = ((coords.x % n) + n) % n;
    const x0 = tx / n, y0 = coords.y / n, span = 1 / n, pad = d.r * 1.2, R = d.r * world;
    for (const [t, cells] of d.groups) {
      const [ax, ay, bx, by] = tileRect(d, t);
      if (bx < x0 || ax > x0 + span || by < y0 || ay > y0 + span) continue;
      ctx.save();
      ctx.beginPath(); ctx.rect((ax - x0) * world, (ay - y0) * world, (bx - ax) * world, (by - ay) * world); ctx.clip();
      for (let i = lowerBound(cells, x0 - pad); i < cells.length && cells[i][0] <= x0 + span + pad; i++) {
        const [cx, cy, , k] = cells[i];
        if (cy < y0 - pad || cy > y0 + span + pad) continue;
        hexPath(ctx, (cx - x0) * world, (cy - y0) * world, R);
        ctx.fillStyle = ctx.strokeStyle = RANGE_COLORS[k]; ctx.fill(); ctx.stroke();   // outline = fill: no seam
      }
      ctx.restore();
    }
  },
  // Observations of the cell under a point (tooltip): tile of the displayed zoom if loaded, otherwise the local grid.
  countAt(latlng) {
    const z = this._tileZoom, n = 2 ** z, p = L.CRS.EPSG3857.latLngToPoint(latlng, 0);
    const wx = ((((p.x / 256) % 1) + 1) % 1) * n, wy = p.y / 256 * n, t = this.zt.get(`${z}/${Math.floor(wx)}/${Math.floor(wy)}`);
    if (t) {
      const sx = 4096 / (t.hex + 1), hy = Math.sqrt(3) * sx / 3, ux = (wx % 1) * 4096, uy = (wy % 1) * 4096;
      let best = null, bd = (sx / 1.5) ** 2;
      for (const [i, h, nb] of t.cells) { const dd = (i * sx - ux) ** 2 + (t.y0 + h * hy - uy) ** 2; if (dd < bd) { bd = dd; best = nb; } }
      return best;
    }
    return this.cellAt(latlng)?.[2] ?? null;
  },
  // Cell under a point (for the tooltip): search by sorted x then distance to the centre.
  cellAt(latlng) {
    const p = L.CRS.EPSG3857.latLngToPoint(latlng, 0), d = this.d;
    const x = (((p.x / 256) % 1) + 1) % 1, y = p.y / 256;
    let best = null, bd = d.r * d.r;
    for (let i = lowerBound(d.cells, x - d.r); i < d.cells.length && d.cells[i][0] <= x + d.r; i++) {
      const c = d.cells[i], dd = (c[0] - x) ** 2 + (c[1] - y) ** 2, [ax, ay, bx, by] = tileRect(d, c[4]);
      if (dd < bd && x >= ax && x < bx && y >= ay && y < by) { bd = dd; best = c; }   // cell of the tile of the point
    }
    return best;
  },
});
async function rangeLayer(key, m, { tooltips = true } = {}) {
  const d = await getRange(key), layer = new HexLayer(d, key);
  layer.breaks = d.breaks; layer.max = d.max; layer.cells = d.cells.length;
  if (tooltips) {
    const tip = L.tooltip({ sticky: true, direction: 'top', offset: [0, -6] });
    const move = e => { const nb = layer.countAt(e.latlng);
      if (nb) tip.setLatLng(e.latlng).setContent(t('count.observations', { n: nb })).addTo(m); else tip.remove(); };
    layer.on('add', () => { m.on('mousemove', move); m.on('mouseout', () => tip.remove()); });
    layer.on('remove', () => { m.off('mousemove', move); tip.remove(); });
  }
  return layer;
}
function rangeLegend(br, max) {
  const lo = [1, ...br], hi = [...br.map(b => b - 1), max];
  return `<div class="classes">${lo.map((l, i) => `<span><i style="background:${RANGE_COLORS[i]}"></i>${l === hi[i] ? fmtN(l) : `${fmtN(l)}–${fmtN(hi[i])}`}</span>`).join('')}</div>`;
}
// OpenStreetMap background washed out almost to white (basemap class, app.css): forests, parks and relief are no longer
// green or dark, green stays reserved for the hexagons of the range map.
// referrerPolicy: without a Referer header (same-origin policy of the dashboard), OSM answers with an "Access blocked" tile.
const baseLayer = () => L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '© OpenStreetMap', className: 'basemap', referrerPolicy: 'strict-origin-when-cross-origin' });
// Touch screen: one finger scrolls the page instead of moving the map; pinching zooms and moves.
const MAP_TOUCH = L.Browser.mobile ? { dragging: false, tap: false } : {};
const photoIcon = on => ({ radius: on ? 8 : 5, stroke: false, fillColor: on ? '#dc2626' : '#4250af', fillOpacity: on ? 1 : .85 });

/* Mini map of the viewer: created only once; only the pin and the range shown change.
   Range layers already built are kept: going back over a candidate is instant. */
let mini, miniPin, miniShown = null, miniAsset = null, miniReq = 0;
const miniLayers = new Map();
function ensureMini() {
  if (!mini) {
    mini = L.map('minimap', { ...MAP_TOUCH, zoomControl: false, attributionControl: false, scrollWheelZoom: true, worldCopyJump: true }).addLayer(baseLayer());
    mini.setView([30, 10], 1);
  }
  setTimeout(() => mini.invalidateSize(), 0);
  return mini;
}
function drawMini(key, a) {
  ensureMini();
  if (miniAsset !== a.asset_id) {   // new photo: pin and framing
    miniAsset = a.asset_id; miniPin?.remove(); miniPin = null;
    if (a.lat != null) { miniPin = L.circleMarker([a.lat, a.lon], photoIcon(true)).addTo(mini); mini.setView([a.lat, a.lon], Math.max(3, Math.min(mini.getZoom(), 6))); }
  }
  setMiniRange(key);
}
async function setMiniRange(key) {
  if (!mini || key === miniShown) return;
  const req = ++miniReq;
  let layer = miniLayers.get(key);
  if (!layer && key) { try { layer = await rangeLayer(key, mini, { tooltips: false }); miniLayers.set(key, layer); } catch (e) { layer = null; } }
  if (req !== miniReq) return;   // another hover arrived in the meantime
  miniLayers.forEach(l => mini.hasLayer(l) && l !== layer && mini.removeLayer(l));
  if (layer && !mini.hasLayer(layer)) layer.addTo(mini);
  miniPin?.bringToFront(); miniShown = key;
}
