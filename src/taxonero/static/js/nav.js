/* ============================== navigation ============================== */
/* The address carries the whole state of a page: #view/argument?filters&photo=<id of the open photo>. Filters and next
   photo replace the history entry (replaceState); changing page or opening the viewer adds one.
   NAV.stack mirrors the browser history (which gives no access to the neighbouring entries): title and position of
   each page, for the Back button of the top bar, which thus stays in agreement with the mouse button. */
const SCOPE_INT = ['taxon', 'month', 'year'], SCOPE_KEYS = [...SCOPE_INT, 'rank', 'name', 'country', 'city', 'camera', 'day', 'source'];
const NAV = { stack: [], idx: 0, base: null, parent: null, settling: false };
try {
  const saved = JSON.parse(sessionStorage.getItem('nav') || 'null');
  if (Array.isArray(saved?.stack)) { NAV.stack = saved.stack; NAV.idx = saved.idx; }
} catch (e) {}
const saveNav = () => { try { sessionStorage.setItem('nav', JSON.stringify({ stack: NAV.stack, idx: NAV.idx })); } catch (e) {} };
history.scrollRestoration = 'manual';

const parseHash = h => { const [path, query] = (h || '#photos').slice(1).split('?'); return { path: path || 'photos', q: new URLSearchParams(query || '') }; };
const hashOf = (path, q) => { const s = q.toString(); return `#${path}${s ? `?${s}` : ''}`; };
const baseOf = h => { const { path, q } = parseHash(h); q.delete('photo'); return hashOf(path, q); };
/* Change the address parameters without creating a history step (null or '': removed). */
function setQuery(changes) {
  const { path, q } = parseHash(location.hash);
  Object.entries(changes).forEach(([k, v]) => { if (v == null || v === '') q.delete(k); else q.set(k, v); });
  const h = hashOf(path, q);
  history.replaceState(history.state, '', h);
  NAV.stack[NAV.idx].hash = h; NAV.base = baseOf(h); saveNav();
}
function navTrack(hash) {
  const i = history.state?.navIdx;
  if (i != null && NAV.stack[i]) { NAV.idx = i; NAV.stack[i].hash = hash; }
  else {
    const prev = NAV.stack[NAV.idx];
    NAV.idx = NAV.stack.length ? NAV.idx + 1 : 0;
    NAV.stack.length = NAV.idx;
    // Viewer opened on the same page: it keeps the position of the grid below.
    NAV.stack.push({ hash, title: prev?.title || '', scroll: prev && baseOf(prev.hash) === baseOf(hash) ? prev.scroll : 0 });
    history.replaceState({ navIdx: NAV.idx }, '', hash);
  }
  saveNav();
}
/* Title of the current page, shown by the Back button of the next page. */
function setNavTitle(title) { if (NAV.stack[NAV.idx]) { NAV.stack[NAV.idx].title = title; saveNav(); } }
/* Logical parent: destination of Back when arriving directly (link, bookmark). */
function setNavParent(hash, title) { NAV.parent = { hash, title }; drawBack(); }
function drawBack() {
  const prev = NAV.idx > 0 ? NAV.stack[NAV.idx - 1] : NAV.parent, b = $('#b-back');
  b.disabled = !prev;
  $('#b-back-l').textContent = prev?.title || '';
  b.title = prev ? (prev.title ? t('nav.back', { page: prev.title }) : t('nav.back_plain')) : t('nav.back_plain');
  b.setAttribute('aria-label', b.title);
}
function goBack() {
  if (NAV.idx > 0) { history.back(); return; }
  if (!NAV.parent) return;
  history.replaceState(history.state, '', NAV.parent.hash);
  route();
}
$('#b-back').onclick = goBack;

/* Sidebar: the page you are already on is not stacked, it scrolls back to the top. */
$$('.nav a').forEach(a => a.addEventListener('click', e => {
  if (a.dataset.v !== STATE.view) return;
  const { q } = parseHash(location.hash);
  if (SCOPE_KEYS.some(k => q.has(k)) || (STATE.view === 'species' && q.toString())) return;   // filters to remove: real navigation
  e.preventDefault(); window.scrollTo(0, 0);
}));

let navTick = false;
window.addEventListener('scroll', () => {
  if (navTick) return;
  navTick = true;
  requestAnimationFrame(() => {
    navTick = false;
    if (!NAV.settling && !V.open && NAV.stack[NAV.idx]) { NAV.stack[NAV.idx].scroll = window.scrollY; saveNav(); }
  });
}, { passive: true });

const gridActive = () => ['photos', 'sp', 'fa'].includes(STATE.view);
/* Back to a page: its photos reloaded if needed, then the previous position. */
async function restoreScroll(entry, seq) {
  const y = entry.scroll || 0;
  if (!y) return;
  while (gridActive() && P.more && document.documentElement.scrollHeight < y + innerHeight && seq === routeSeq) {
    if (!await loadMorePhotos()) break;
  }
  if (seq === routeSeq) window.scrollTo(0, y);
}
/* Viewer according to the address: opened on this photo, or closed. */
async function syncViewer(photo) {
  if (!photo) { if (V.open) closeViewer(); return; }
  if (V.open && cur()?.asset_id === photo) return;
  const seq = routeSeq;
  let list = gridActive() ? P.assets : null, i = list ? list.findIndex(a => a.asset_id === photo) : -1;
  for (let tries = 0; list && i < 0 && P.more && tries < 25 && seq === routeSeq; tries++) {
    if (!await loadMorePhotos()) break;
    i = list.findIndex(a => a.asset_id === photo);
  }
  if (seq !== routeSeq) return;
  if (i < 0) {   // outside the displayed list: the photo alone
    try { list = [await api(`/api/assets/${encodeURIComponent(photo)}`)]; i = 0; }
    catch (e) { setQuery({ photo: null }); fail(e); return; }
    if (seq !== routeSeq) return;
  }
  openViewer(list, i);
}

let routeSeq = 0;
function route(force) {
  const hash = location.hash || '#photos';
  navTrack(hash);
  closeWiki();
  const { path, q } = parseHash(hash), photo = q.get('photo');
  if (force !== true && baseOf(hash) === NAV.base) { syncViewer(photo); drawBack(); return; }   // same page: viewer only
  if (STATE.view === 'identify') clearIdentify();   // the dropped image is forgotten as soon as the page is left
  if (V.open) closeViewer(true);
  q.delete('photo');
  const entry = NAV.stack[NAV.idx], seq = ++routeSeq;
  NAV.base = baseOf(hash); NAV.parent = null; NAV.settling = true;
  // #view/argument?filters: #species/123, #family/Canidae (also #order/…, #class/…), #species?iucn=ENDANGERED&month=5…
  const [v, arg] = path.split('/');
  homeGrid();
  STATE.view = v === 'species' && arg ? 'sp' : HIGHER_RANKS.includes(v) && arg ? 'fa'
    : (['photos', 'species', 'stats', 'settings', 'identify'].includes(v) ? v : 'photos');
  applyQuery(STATE.view, q);
  $$('.nav a').forEach(a => a.classList.toggle('on', a.dataset.v === (['sp', 'fa'].includes(STATE.view) ? 'species' : STATE.view)));
  $$('.view').forEach(el => el.classList.toggle('on', el.id === `v-${STATE.view}`));
  const view = $(`#v-${STATE.view}`);
  view.querySelector(':scope > .loaderr')?.remove();
  if (!['sp', 'fa'].includes(STATE.view)) setNavTitle(t(`nav.${STATE.view}`));
  drawBack();
  if (!entry.scroll) window.scrollTo(0, 0);
  // Server in error: a message and a new attempt rather than an empty page.
  (async () => {
    if (STATE.view === 'sp') await loadSpeciesPage(+arg, readScope(q));
    else if (STATE.view === 'fa') await loadFamilyPage(decodeURIComponent(arg), v, readScope(q));
    else await ({ photos: () => loadPhotos(P.base), species: loadSpecies, stats: loadStats, settings: loadSettings, identify: loadIdentify })[STATE.view]();
    if (seq !== routeSeq) return;
    await restoreScroll(entry, seq);
    await syncViewer(photo);
  })().catch(err => {
    if (seq !== routeSeq) return;
    console.warn(err);
    view.insertAdjacentHTML('afterbegin', `<div class="empty loaderr" role="alert">${t('err.load_failed', { msg: esc(err.message || err) })}<br><button class="btn">${t('common.retry')}</button></div>`);
    view.querySelector(':scope > .loaderr button').onclick = () => route(true);
  }).finally(() => { if (seq === routeSeq) NAV.settling = false; });
}
const reloadPage = () => route(true);
window.addEventListener('hashchange', route);

function readScope(q) {
  const out = {};
  SCOPE_KEYS.forEach(k => { const v = q.get(k);
    if (v == null) return;
    if (SCOPE_INT.includes(k)) { if (Number.isInteger(+v)) out[k] = +v; } else out[k] = v; });
  return out;
}
/* The address decides the state of the page. Without a filter, Photos picks up the last chosen filter. */
function applyQuery(view, q) {
  if (view === 'photos') {
    const scope = readScope(q), scoped = Object.keys(scope).length > 0;
    const filter = q.get('filter') || (scoped ? 'all' : P.userFilter);
    setGridState({ filter, kinds: q.get('kinds') || '', group: q.get('group') || '', sort: q.get('sort') || P.userSort, base: +q.get('base') || 0, scope, embedded: false });
    if (!q.get('filter')) setQuery({ filter });
  }
  if (view !== 'species') return;
  const iucn = q.get('iucn');
  Object.assign(SP, { q: q.get('q') || '', group: q.get('group'), iucn: IUCN_FILTERS.some(f => f.id === iucn) ? iucn : null,
    country: q.get('country'), camera: q.get('camera'), month: +q.get('month') || null, year: +q.get('year') || null });
}
