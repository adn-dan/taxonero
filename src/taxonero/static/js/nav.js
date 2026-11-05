/* ============================== navigation ============================== */
/* L'adresse porte tout l'état d'une page : #vue/argument?filtres&photo=<id de la photo ouverte>. Filtres et photo
   suivante remplacent l'entrée d'historique (replaceState) ; changer de page ou ouvrir la visionneuse en ajoute une.
   NAV.stack double l'historique du navigateur (qui ne donne pas accès aux entrées voisines) : titre et position de
   chaque page, pour le bouton Retour de la barre du haut, qui reste ainsi d'accord avec le bouton de la souris. */
const SCOPE_INT = ['taxon', 'month', 'year'], SCOPE_KEYS = [...SCOPE_INT, 'rank', 'name', 'country', 'city', 'camera', 'day'];
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
/* Modifie les paramètres de l'adresse sans créer d'étape d'historique (null ou '' : retiré). */
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
    // Visionneuse ouverte sur la même page : elle garde la position de la grille en dessous.
    NAV.stack.push({ hash, title: prev?.title || '', scroll: prev && baseOf(prev.hash) === baseOf(hash) ? prev.scroll : 0 });
    history.replaceState({ navIdx: NAV.idx }, '', hash);
  }
  saveNav();
}
/* Titre de la page courante, affiché par le bouton Retour de la page suivante. */
function setNavTitle(title) { if (NAV.stack[NAV.idx]) { NAV.stack[NAV.idx].title = title; saveNav(); } }
/* Parent logique : destination du Retour quand on est arrivé directement (lien, favori). */
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

/* Barre latérale : la page où l'on est déjà ne s'empile pas, elle remonte en haut. */
$$('.nav a').forEach(a => a.addEventListener('click', e => {
  if (a.dataset.v !== STATE.view) return;
  const { q } = parseHash(location.hash);
  if (SCOPE_KEYS.some(k => q.has(k)) || (STATE.view === 'species' && q.toString())) return;   // filtres à retirer : vraie navigation
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
/* Retour à une page : ses photos rechargées au besoin, puis la position d'avant. */
async function restoreScroll(entry, seq) {
  const y = entry.scroll || 0;
  if (!y) return;
  while (gridActive() && P.more && document.documentElement.scrollHeight < y + innerHeight && seq === routeSeq) {
    if (!await loadMorePhotos()) break;
  }
  if (seq === routeSeq) window.scrollTo(0, y);
}
/* Visionneuse d'après l'adresse : ouverte sur cette photo, ou fermée. */
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
  if (i < 0) {   // hors de la liste affichée : la photo seule
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
  if (force !== true && baseOf(hash) === NAV.base) { syncViewer(photo); drawBack(); return; }   // même page : visionneuse seule
  if (STATE.view === 'identify') clearIdentify();   // l'image déposée est oubliée dès qu'on quitte la page
  if (V.open) closeViewer(true);
  q.delete('photo');
  const entry = NAV.stack[NAV.idx], seq = ++routeSeq;
  NAV.base = baseOf(hash); NAV.parent = null; NAV.settling = true;
  // #vue/argument?filtres : #species/123, #family/Canidae (aussi #order/…, #class/…), #species?iucn=ENDANGERED&month=5…
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
  // Serveur en erreur : message et nouvel essai plutôt qu'une page vide.
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
/* L'adresse décide de l'état de la page. Sans filtre, Photos reprend le dernier filtre choisi. */
function applyQuery(view, q) {
  if (view === 'photos') {
    const scope = readScope(q), scoped = Object.keys(scope).length > 0;
    const filter = q.get('filter') || (scoped ? 'all' : P.userFilter);
    setGridState({ filter, group: q.get('group') || '', sort: q.get('sort') || P.userSort, base: +q.get('base') || 0, scope, embedded: false });
    if (!q.get('filter')) setQuery({ filter });
  }
  if (view !== 'species') return;
  const iucn = q.get('iucn');
  Object.assign(SP, { q: q.get('q') || '', group: q.get('group'), iucn: IUCN_FILTERS.some(f => f.id === iucn) ? iucn : null,
    country: q.get('country'), camera: q.get('camera'), month: +q.get('month') || null, year: +q.get('year') || null });
}
