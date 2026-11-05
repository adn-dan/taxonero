/* ============================== visionneuse ============================== */
const V = { open: false, list: [], i: 0, sel: 0, info: true, touched: new Set() };
const cur = () => V.list[V.i];
const curDet = () => cur()?.detections[V.sel];
const firstPending = a => Math.max(0, a?.detections.findIndex(d => d.status === 'pending') ?? 0);

function openViewer(list, i) {
  Object.assign(V, { open: true, list, i, sel: firstPending(list[i]), touched: new Set(), opener: V.open ? V.opener : document.activeElement });
  $('#viewer').classList.add('on'); document.body.style.overflow = 'hidden';
  $('#viewer').setAttribute('aria-label', t('nav.photos'));
  $('#viewer').focus();
  photoInHash(true);
  drawViewer();
  fitPhoto();   // la fenêtre a pu changer de taille depuis la dernière ouverture
  // Visionneuse cachée jusqu'ici : Annotorious ne relit la taille de l'image qu'un peu après (sinon NaN, Infinity) ;
  // les rectangles attendent.
  V.opening = true;
  setTimeout(() => { V.opening = false; if (V.open) drawBoxes(); }, 150);
}
/* Photo ouverte dans l'adresse : à l'ouverture une étape d'historique (Retour ferme la visionneuse), ensuite non. */
function photoInHash(push) {
  const a = cur(), { path, q } = parseHash(location.hash);
  if (!a || q.get('photo') === a.asset_id) return;
  if (!push) { setQuery({ photo: a.asset_id }); return; }
  q.set('photo', a.asset_id);
  location.hash = hashOf(path, q);
}
/* Fermeture demandée (bouton, Échap) : comme le Retour de la barre du haut, sans quitter la page d'en dessous. */
function leaveViewer() {
  const prev = NAV.stack[NAV.idx - 1];
  if (prev && baseOf(prev.hash) === baseOf(location.hash) && !parseHash(prev.hash).q.get('photo')) { history.back(); return; }
  if (parseHash(location.hash).q.get('photo')) setQuery({ photo: null });
  closeViewer();
}
function closeViewer(silent) {
  // Rectangles retirés avant de cacher l'image : Annotorious les recalculerait à la taille 0 (NaN, Infinity).
  anno.clearAnnotations();
  V.open = false; $('#viewer').classList.remove('on'); document.body.style.overflow = '';
  $('#vdone').hidden = true;
  V.opener?.focus?.({ preventScroll: true });
  resetZoom();
  if (silent) return;
  if (V.list === P.assets && gridActive()) {   // la grille montre la dernière photo vue
    const lead = [...P.members].find(([, mem]) => mem.includes(V.i))?.[0] ?? V.i;
    $(`#pgrid .tile[data-i="${lead}"]`)?.scrollIntoView({ block: 'nearest' });
  }
  if (!V.touched.size) return;
  // Photos : statuts à jour sans recharger la grille ; fiche : ses chiffres et ses photos ont pu changer.
  if (STATE.view === 'photos' && V.list === P.assets) refreshTiles(V.touched);
  else if (gridActive()) reloadPage();
}
async function go(delta) {
  const n = V.i + delta;
  if (n >= V.list.length && V.list === P.assets && P.more) await loadMorePhotos();
  if (n < 0 || n >= V.list.length) return;
  V.i = n; V.sel = firstPending(cur()); photoInHash(false); drawViewer();
}

function drawViewer() {
  const a = cur(); if (!a) { leaveViewer(); return; }
  $('#vdone').hidden = true;
  setNavTitle(curDet()?.taxon ? shortName(curDet().taxon) : a.file_name || t('nav.photos'));
  $('#v-title').textContent = `${V.i + 1} / ${V.list.length} · ${a.kind === 'VIDEO' ? `▶ ${t('video.keyframe')} · ` : ''}${a.file_name || ''}`;
  $('#v-immich').hidden = !a.immich_link; if (a.immich_link) $('#v-immich').href = a.immich_link;
  $('#v-prev').style.visibility = V.i > 0 ? '' : 'hidden';
  $('#v-next').style.visibility = V.i < V.list.length - 1 || (V.list === P.assets && P.more) ? '' : 'hidden';
  showPhoto(a);
  drawBoxes(); drawInfo();
}

/* Affichage de la photo, comme la visionneuse d'Immich : la vignette (déjà en cache depuis la grille) tout de
   suite, à la taille de l'écran, puis l'aperçu dès qu'il est chargé ; les photos voisines sont préchargées.
   Jamais l'image précédente sous le titre et les rectangles de la nouvelle. */
function showPhoto(a) {
  const img = $('#photo'), f = $('#frame'), id = a.asset_id;
  if (img.dataset.id === id) return;
  img.dataset.id = id; delete img.dataset.full;
  resetZoom();
  f.classList.add('swap');
  const small = new Image(), full = new Image();
  const put = el => {
    if (img.dataset.id !== id || !el.naturalWidth || img.dataset.full === id) return;   // l'original du zoom est déjà là
    V.ar = el.naturalWidth / el.naturalHeight;
    img.onload = () => { if (img.dataset.id === id) f.classList.remove('swap'); };
    img.src = el.src; fitPhoto();
  };
  small.onload = () => { if (!full.naturalWidth) put(small); };
  full.onload = () => { put(full); preloadAround(); };
  full.onerror = () => { if (img.dataset.id === id && !small.naturalWidth) f.classList.remove('swap'); };
  small.src = `/api/thumb/${id}?size=thumbnail`;
  full.src = `/api/thumb/${id}`;
}
/* Taille de l'image : la plus grande qui tient dans la zone (max-width / max-height du CSS), même pour une
   vignette ou une petite photo, pour que les rectangles ne sautent pas au passage vignette -> aperçu. */
function fitPhoto() {
  const img = $('#photo'); if (!V.ar) return;
  img.style.width = img.style.height = '';
  const cs = getComputedStyle(img), mw = parseFloat(cs.maxWidth), mh = parseFloat(cs.maxHeight);
  if (!(mw > 0 && mh > 0)) return;
  const w = Math.min(mw, mh * V.ar);
  img.style.width = `${Math.round(w)}px`; img.style.height = `${Math.round(w / V.ar)}px`;
}
window.addEventListener('resize', () => { if (V.open) { fitPhoto(); resetZoom(); } });
const PRELOADED = [];
function preloadAround() {
  PRELOADED.length = 0;
  [V.i + 1, V.i - 1, V.i + 2].forEach(k => { const a = V.list[k]; if (a) { const im = new Image(); im.src = `/api/thumb/${a.asset_id}`; PRELOADED.push(im); } });
}

/* Série (rafale) : la même espèce sur plusieurs photos, ou proposition corrigée par la série (series.py). */
function seriesLine(d) {
  if (!d.series_n || d.series_n < 2) return '';
  if (d.model_taxon) return `<div class="locline">🎞 ${tr('series.smoothed', { n: d.series_n, model: esc(shortName(d.model_taxon)) })}</div>`;
  return `<div class="locline">🎞 ${tr('series.support', { n: d.series_n })}</div>`;
}
function statusBadge(d) {
  if (d.status === 'pending' && d.audit) return `<span class="badge2 pending" title="${esc(t('det.audit_hint'))}">${t('det.audit')}</span>`;
  if (d.status === 'confirmed') return `<span class="badge2 ok">${ic('check')} ${t(d.confirmed_by === 'auto' ? 'det.confirmed_auto' : 'det.confirmed_user')}</span>`;
  if (d.status === 'rejected') return `<span class="badge2">${t('det.rejected')}</span>`;
  return `<span class="badge2 pending">${t('det.pending')}</span>`;
}

/* Date, lieu et appareil : chacun mène aux photos qui les partagent. */
function metaLine(a) {
  const link = (q, label) => `<a href="#photos?filter=all&${q}">${esc(label)}</a>`, cam = [a.make, a.model].filter(Boolean).join(' ');
  const items = [
    a.taken_at && link(`day=${encodeURIComponent(a.taken_at.slice(0, 10))}`, fmtDate(a.taken_at)),
    a.city && link(`city=${encodeURIComponent(a.city)}`, a.city),
    a.country && link(`country=${encodeURIComponent(a.country)}`, a.country),
    cam && link(`camera=${encodeURIComponent(cam)}`, cam),
  ].filter(Boolean);
  return items.length ? `<div class="metaline">${items.join(' · ')}</div>` : '';
}
function drawInfo() {
  const a = cur(), d = curDet();
  $('#vi-mapsec').hidden = !d;
  // Photo principale : seulement une photo validée pour ce taxon.
  $('#v-cover').hidden = !(d?.status === 'confirmed' && d.taxon_key);
  $('#v-del').hidden = !d;
  if (!d) {
    $('#vi-body').innerHTML = `<section>${metaLine(a)}<div class="empty" style="padding:32px 0">${tr('det.none')}<br>${tr('det.none_hint')}</div>${locLine(a)}
      <div class="actions"><button class="btn" id="b-taxon">${ic('paw')} ${tr('bulk.taxon')}</button></div></section>`;
    $('#b-loc') && ($('#b-loc').onclick = editLocation);
    $('#b-taxon').onclick = chooseTaxon;
    return;
  }
  const t = d.taxon, n = a.detections.length;
  // Un seul accès par fonction : la fiche complète (page de l'espèce) ou l'article Wikipédia en lecture rapide.
  let h = `<section class="taxon">
    <div class="sectit">${esc(rankName(d.taxon_rank))}${n > 1 ? ` · ${tr('det.subject_of', { i: V.sel + 1, n })} <span style="text-transform:none;letter-spacing:0">(${tr('det.subject_hint')})</span>` : ''}</div>
    <h2>${subjectIcon(d)} ${esc(shortName(t))}</h2>
    ${t ? `<div><i>${esc(t.scientific_name)}</i> <span class="auth">${esc(t.authority || '')}</span></div>` : ''}
    ${otherNames(t)}
    <div class="badges">${statusBadge(d)}${t?.iucn && THREATENED.concat('NEAR_THREATENED').includes(t.iucn)
      ? `<a class="badge2" href="#species?iucn=${t.iucn}"><span class="iucn-dot" style="background:${IUCN_COLOR[t.iucn]}"></span>${tr('iucn.short', { cat: esc(iucnName(t.iucn)) })}</a>` : ''}</div>
    ${metaLine(a)}
    ${seriesLine(d)}
    ${locLine(a)}
    ${t ? `<div class="links"><button class="btn" id="l-sp">${ic('paw')} ${tr('det.species_page')}</button>${hasWiki(t) ? `<button class="btn" id="l-wp">${ic('book')} ${tr('wiki.wikipedia')}</button>` : ''}</div>` : ''}
  </section>`;
  // Actions selon l'état : jamais deux boutons pour la même chose. Supprimer et Ré-analyser : barre du haut.
  const acts = { pending: ['ok', 'no'], confirmed: ['no', 'reset'], rejected: ['ok', 'reset'] }[d.status] || ['ok', 'no'];
  const label = { ok: `${ic('check')} ${tr('action.confirm')}`, no: tr('action.reject'), reset: tr('action.reset') };
  h += `<section><div class="actions">${acts.map(k => `<button class="btn${k === 'ok' ? ' primary' : ''}" id="b-${k}">${label[k]}</button>`).join('')}
    <button class="btn" id="b-taxon">${ic('paw')} ${tr('bulk.taxon')}</button></div>
    ${d.why_pending?.length && !d.audit ? `<div class="why">${tr('why.title', { reasons: d.why_pending.map(whyText).join(' · ') })}</div>` : ''}
    <div id="series"></div>
    <div class="sub" style="font-size:12px;margin-top:8px">${tr('det.edit_hint')}</div></section>`;
  if (d.candidates.length) {
    const maxp = Math.max(...d.candidates.map(c => c.p), 0.01);
    // Les candidats à moins de 1 % ne sont que du bruit ; on en garde au moins 3 pour comparer.
    const shownC = V.shownC = d.candidates.filter((c, k) => k < 3 || c.p >= 0.01).length;
    h += `<section><div class="sectit">${tr('cand.title')} <span style="text-transform:none;letter-spacing:0">· ${tr('cand.hint')}</span></div>
      ${d.candidates.slice(0, shownC).map((c, k) => `<div class="cand${c.gbif_key === d.taxon_key ? ' top' : ''}" data-k="${k}" role="button" tabindex="0" title="${esc(tr('cmp.hint'))}">
      <span class="k">${k + 1}</span><span><span class="cn" data-ck="${c.gbif_key || ''}">${candName(c)}</span><small>${c.lineage?.family ? `<a href="#family/${encodeURIComponent(c.lineage.family)}">${esc(c.lineage.family)}</a>` : ''} · <span class="dot ${c.plausibility}"></span>${tr(`plaus.${c.plausibility || 'unknown'}`)}</small>
      <div class="bar"><i style="width:${Math.round(c.p / maxp * 100)}%"></i></div></span><span class="p${c.p < 0.005 ? ' low' : ''}">${fmtProb(c.p)}</span></div>`).join('')}</section>`;
  }
  $('#vi-body').innerHTML = h;
  $('#vi-mapcap').innerHTML = tr('map.caption', { pin: '<span style="color:#dc2626">●</span>', gps: a.lat == null ? ` (${tr('plaus.unknown')})` : '' });
  // Clic sur un candidat : comparaison en images ; survol : son aire sur la mini-carte (aucune validation).
  const shownKey = () => d.taxon_key || d.candidates[0]?.gbif_key;
  $$('#vinfo .cand').forEach(el => {
    const c = d.candidates[+el.dataset.k];
    el.onclick = e => { if (!e.target.closest('a')) compareCurrent(c?.gbif_key); };
    el.onmouseenter = () => setMiniRange(c?.gbif_key);
    el.onmouseleave = () => setMiniRange(shownKey());
  });
  drawMini(shownKey(), a);
  loadCandNames(d);
  if (d.status === 'confirmed' && d.taxon_key) loadSeries(d);
  $('#l-sp') && ($('#l-sp').onclick = () => { location.hash = `species/${d.taxon_key}`; });   // route() ferme la visionneuse
  $('#l-wp') && ($('#l-wp').onclick = () => openWiki(d.taxon_key, 'wikipedia'));
  $('#b-ok') && ($('#b-ok').onclick = () => act('confirm'));
  $('#b-no') && ($('#b-no').onclick = () => act('reject'));
  $('#b-reset') && ($('#b-reset').onclick = () => act('reset'));
  $('#b-taxon').onclick = chooseTaxon;
  $('#b-loc') && ($('#b-loc').onclick = editLocation);
}
/* Noms communs des candidats (cache local ; le serveur récupère en arrière-plan ceux qui manquent). */
const NAMES = new Map();
function candName(c) {
  const t = NAMES.get(c.gbif_key), com = t && (t.vernacular?.[LANG] || t.vernacular?.en || t.vernacular?.fr);
  return com ? `<b class="cname">${esc(com)}</b> <i>${esc(c.name)}</i>` : `<i>${esc(c.name)}</i>`;
}
async function loadCandNames(d, tries = 0) {
  const refs = d.candidates.filter(c => c.gbif_key && !NAMES.has(c.gbif_key)).map(c => ({ key: c.gbif_key, name: c.name }));
  if (!refs.length) return;
  let r;
  try { r = await api('/api/names', { method: 'POST', body: JSON.stringify(refs) }); } catch (e) { return; }
  Object.entries(r.names).forEach(([k, v]) => NAMES.set(+k, v));
  if (curDet() !== d) return;
  $$('#vinfo .cn').forEach(el => { const c = d.candidates.find(x => String(x.gbif_key) === el.dataset.ck); if (c) el.innerHTML = candName(c); });
  if (r.pending && tries < 6) setTimeout(() => loadCandNames(d, tries + 1), 2500);
}
/* Comparaison en images (compare.js) : le sujet sélectionné et les candidats affichés. */
function compareCurrent(focus) {
  const d = curDet(); if (!d) return;
  const a = cur(), img = $('#photo');
  openCompare({ img, box: d, candidates: d.candidates.slice(0, V.shownC ?? 3), focus, asset: a?.asset_id,
                full: a && a.kind !== 'VIDEO' && img.dataset.full !== a.asset_id ? `/api/full/${a.asset_id}` : null,
                place: a && { lat: a.lat, lon: a.lon, user: !!a.geo_user },
                onLocate: async () => {   // candidats reclassés avec le nouveau lieu : comparaison rouverte à jour
                  if (await editLocation() && CMP.open) { if (curDet()) compareCurrent(focus); else closeCompare(); }
                },
                onPick: c => act('confirm', { gbif_key: c.gbif_key, name: c.name }) });
}
function toggleInfo() {
  V.info = !V.info; $('#viewer').classList.toggle('noinfo', !V.info); $('#v-info').classList.toggle('on', V.info); fitPhoto(); resetZoom();
  if (V.info) drawInfo();
}

/* Une espèce pour le sujet affiché ; une photo sans sujet reçoit l'espèce sur l'image entière (comme en lot). */
async function chooseTaxon() {
  const tx = await pickTaxon(t('bulk.taxon')); if (!tx) return;
  if (curDet()) { act('confirm', { gbif_key: tx.gbif_key }); return; }
  const id = cur().asset_id;
  try {
    const r = await api('/api/photos/bulk', { method: 'POST', body: JSON.stringify({ asset_ids: [id], action: 'taxon', gbif_key: tx.gbif_key }) });
    pushUndo({ asset: id, bulk: { changed: r.changed, created: r.created } });
    await refreshAsset();
  } catch (e) { fail(e); }
}
async function refreshAsset() {
  const a = await api(`/api/assets/${cur().asset_id}`);
  V.list[V.i] = { ...cur(), ...a }; V.touched.add(V.i);
  drawBoxes(); drawInfo(); loadStatus();
}
const snapshot = d => ({ status: d.status, confirmed_by: d.confirmed_by, taxon_key: d.taxon_key, taxon_rank: d.taxon_rank,
  reviewed_at: d.reviewed_at, x: d.x, y: d.y, w: d.w, h: d.h, score: d.score, margin: d.margin, plausibility: d.plausibility,
  candidates: d.candidates, candidates_out: d.candidates_out });
// Relecture de la file « À relire » : la photo terminée, on passe toute seule à la suivante.
const autoNext = () => V.list === P.assets && ['pending', 'audit'].includes(P.filter);
/* Fin de la file à relire : ce qu'il reste, plutôt qu'une photo qui ne bouge plus. */
function queueEnd() {
  const left = V.list.findIndex(a => a.detections.some(d => d.status === 'pending')), n = V.list.filter(a => a.detections.some(d => d.status === 'pending')).length;
  const box = $('#vdone');
  box.innerHTML = `<b>${left < 0 ? t('queue.done') : t('queue.left', { n })}</b><div class="actions">
    ${left < 0 ? '' : `<button class="btn primary" id="q-first">${t('queue.first')}</button>`}<button class="btn" id="q-back">${t('queue.back')}</button></div>`;
  box.hidden = false;
  $('#q-back').onclick = leaveViewer;
  $('#q-first') && ($('#q-first').onclick = () => { V.i = left; V.sel = firstPending(cur()); photoInHash(false); drawViewer(); });
}
function nextInQueue(from) {
  if (!autoNext() || !V.open || V.i !== from) return;
  if (V.i < V.list.length - 1 || P.more) go(1); else queueEnd();
}
async function act(kind, body) {
  const d = curDet(); if (!d) return;
  const entry = { asset: cur().asset_id, det: d.id, prev: snapshot(d) }, from = V.i;
  try { await api(`/api/detections/${d.id}/${kind}`, { method: 'POST', body: body ? JSON.stringify(body) : undefined }); }
  catch (e) { fail(e); return; }
  pushUndo(entry);
  await refreshAsset();
  if (kind === 'reset') return;
  // reste sur la photo et sélectionne le prochain sujet à relire s'il y en a un
  const next = cur().detections.findIndex(x => x.status === 'pending');
  if (next >= 0) { V.sel = next; drawBoxes(); drawInfo(); return; }
  const done = cur().detections.find(x => x.id === d.id);
  // Série à valider d'un coup : on reste pour proposer le bouton ; sinon photo suivante.
  const inSeries = kind === 'confirm' && done?.taxon_key ? await loadSeries(done) : 0;
  if (!inSeries) setTimeout(() => nextInQueue(from), 250);
}
async function deleteDet() {
  const d = curDet(); if (!d) return;
  const entry = { asset: cur().asset_id, det: d.id, prev: snapshot(d) };
  try { await api(`/api/detections/${d.id}`, { method: 'DELETE' }); } catch (e) { fail(e); return; }
  pushUndo(entry);
  V.sel = 0; await refreshAsset();
  toast(t('toast.deleted', { key: '<kbd style="color:inherit">U</kbd>' }));
}
// Touches 1 à 8 : seulement les candidats affichés (les autres sont sous 1 %).
function pick(k) { const c = k < (V.shownC ?? 8) ? curDet()?.candidates[k] : null; if (c) act('confirm', { gbif_key: c.gbif_key, name: c.name }); }
/* Série (rafale) : après validation, propose le même taxon pour les autres photos de la série où il est proposé.
   Renvoie le nombre de photos concernées. */
const SERIES = new Map();
async function loadSeries(d) {
  const a = cur(), k = `${a.asset_id}:${d.id}:${d.taxon_key}`;
  if (!SERIES.has(k)) SERIES.set(k, api(`/api/assets/${a.asset_id}/series?key=${d.taxon_key}`).catch(() => null));
  const r = await SERIES.get(k);
  if (!r || r.assets.length < 2) return 0;
  const n = r.matches.length;
  if (curDet()?.id !== d.id || !$('#series')) return n;
  $('#series').innerHTML = `<div class="why">${t('series.info', { n: r.assets.length })}${n ? '' : ` ${t('series.none')}`}</div>`
    + (n ? `<button class="btn" id="b-series">${ic('check')} ${t('series.confirm', { n })}</button>` : '');
  $('#b-series') && ($('#b-series').onclick = async () => {
    try {
      const res = await api(`/api/assets/${a.asset_id}/series/confirm`, { method: 'POST', body: JSON.stringify({ gbif_key: d.taxon_key }) });
      SERIES.clear();
      pushUndo({ asset: a.asset_id, batch: res.changed });
      await refreshList(new Set(res.changed.map(x => x.asset_id)));
      toast(t('series.done', { n: res.changed.length, key: '<kbd style="color:inherit">U</kbd>' }));
      drawInfo(); loadStatus();
      const from = V.i; setTimeout(() => nextInQueue(from), 600);
    } catch (e) { fail(e); }
  });
  return n;
}
async function refreshList(ids) {
  await Promise.all(V.list.map(async (x, j) => { if (ids.has(x.asset_id)) { V.list[j] = { ...x, ...(await api(`/api/assets/${x.asset_id}`)) }; V.touched.add(j); } }));
}
/* Annulation : un seul historique pour toute la session, à l'unité comme en lot, visionneuse ouverte ou non. */
const UNDO = [];
function drawUndo() { $('#b-undo').hidden = !UNDO.length; $('#v-undo').disabled = !UNDO.length; }
function pushUndo(entry) { UNDO.push(entry); drawUndo(); }
async function undo() {
  const u = UNDO.pop(); if (!u) return;
  drawUndo();
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
  try {
    if (u.location) await post('/api/assets/location-undo', u.location);
    else if (u.bulk) await post('/api/photos/bulk-undo', u.bulk);
    else if (u.batch) await post('/api/detections/restore-many', u.batch);
    else if (u.created) await api(`/api/detections/${u.det}?purge=true`, { method: 'DELETE' });
    else await post(`/api/detections/${u.det}/restore`, u.prev);
  } catch (e) { fail(e); return; }
  SERIES.clear(); loadStatus(); toast(t('toast.undone'));
  if (!V.open) { reloadPage(); return; }
  const ids = new Set([u.asset, ...(u.batch || u.bulk?.changed || []).map(x => x.asset_id), ...(u.location || []).map(x => x.asset_id)]);
  await refreshList(ids);
  const i = V.list.findIndex(x => x.asset_id === u.asset);
  if (i >= 0) { V.i = i; photoInHash(false); }
  if (u.det) V.sel = Math.max(0, cur().detections.findIndex(x => x.id === u.det)); else V.sel = 0;
  drawViewer();
}
$('#v-close').onclick = leaveViewer;
$('#b-undo').onclick = undo;
$('#v-del').onclick = () => deleteDet();
$('#v-cover').onclick = () => { const d = curDet(); if (d) setCover(d.taxon_key, cur().asset_id); };
$('#v-prev').onclick = () => go(-1);
$('#v-next').onclick = () => go(1);
$('#v-info').onclick = toggleInfo;
$('#v-draw').onclick = () => enableDraw();
async function rescan() {
  const a = cur(), btn = $('#v-rescan');
  if (!a || btn.disabled) return;
  btn.disabled = true; toast(t('rescan.running'));
  try {
    await api(`/api/assets/${a.asset_id}/reanalyze`, { method: 'POST' });
    // Les propositions à relire ont été remplacées : leur annulation n'a plus d'objet.
    for (let k = UNDO.length - 1; k >= 0; k--) if (UNDO[k].asset === a.asset_id) UNDO.splice(k, 1);
    drawUndo();
    V.sel = 0; await refreshAsset();
    const n = cur().detections.length;
    toast(n ? t('rescan.done', { n }) : t('rescan.done_none'));
  } catch (e) { toast(t('rescan.failed', { msg: esc(e.message) })); }
  finally { btn.disabled = false; }
}
$('#v-rescan').onclick = rescan;
drawUndo();
