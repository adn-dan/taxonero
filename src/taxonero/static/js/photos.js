/* ============================== page Photos ============================== */
const FILTERS = [
  { id: 'pending' }, { id: 'audit', hideEmpty: true }, { id: 'auto' }, { id: 'user' }, { id: 'rejected' }, { id: 'none' },
  { id: 'failed', hideEmpty: true }, { id: 'all' }, { id: 'identified', onlyActive: true },
];
const P = { filter: 'pending', userFilter: 'pending', userSort: 'date', scope: {}, embedded: false, key: '', onStar: null, scopeNames: {},
  assets: [], counts: {}, more: false, loading: false,
  sort: 'date', group: '', groups: [], sel: new Set(), allSel: false, excl: new Set(), selecting: false, lastSel: null,
  shown: 0, byYear: false, lastSection: null, sectionN: 0, seq: 0,
  // Séries (rafales) empilées : une tuile « ×N » par série, dans l'ordre par date. members : tuile -> photos.
  stack: true, members: new Map(), lastLead: null,
  // Navigation : P.assets commence à la photo n° P.base de la liste (saut à un mois de la frise) ; months : frise.
  base: 0, months: [] };
// Groupe (page Espèces) d'une valeur brute renvoyée par le serveur : classe, ou règne pour plantes et champignons.
const rawGroup = v => groupOf({ lineage: { class: v, phylum: v, kingdom: v } }).id;
const groupParam = () => P.groups.filter(x => rawGroup(x.group) === P.group).map(x => x.group).join(',');
// Le tri par confiance ne vaut que pour les photos à relire ; ailleurs, par date.
const sortOf = () => ['pending', 'audit'].includes(P.filter) ? P.sort : 'date';
const scopeQS = () => { const q = new URLSearchParams(); Object.entries(P.scope).forEach(([k, v]) => q.set(k, v)); const s = q.toString(); return s ? `&${s}` : ''; };
const photosURL = off => `/api/photos?filter=${P.filter}&limit=${PAGE}&offset=${P.base + off}&sort=${sortOf()}&group=${encodeURIComponent(groupParam())}${scopeQS()}`;
const PAGE = 200;
try { P.filter = P.userFilter = localStorage.getItem('photos-filter') || 'pending'; P.sort = P.userSort = localStorage.getItem('photos-sort') || 'date';
  P.stack = localStorage.getItem('photos-stack') !== '0'; } catch (e) {}
const stacking = () => P.stack && sortOf() === 'date';
// Photos d'une tuile : toute la série pour une pile, sinon la photo seule.
const idsOf = i => (P.members.get(i) || [i]).map(k => P.assets[k].asset_id);

async function loadMorePhotos() {
  if (!P.more || P.loading) return false;
  P.loading = true;
  const seq = P.seq;
  try {
    const next = await api(photosURL(P.assets.length));
    if (seq !== P.seq) return false;   // filtre changé entre-temps : page d'une autre liste
    P.assets.push(...next); P.more = next.length === PAGE;
    if (gridActive()) appendGrid();
    return next.length > 0;
  } finally { P.loading = false; }
}
async function loadPhotos(base = 0) {
  if (!FILTERS.some(f => f.id === P.filter)) { P.filter = 'pending'; if (!P.embedded) setQuery({ filter: P.filter }); }
  // Chargements qui se chevauchent (filtre changé avant la fin du précédent) : seul le dernier s'affiche.
  const seq = ++P.seq;
  P.base = base;
  const groups = await api(`/api/photos/groups?filter=${P.filter}${scopeQS()}`);
  if (seq !== P.seq) return;
  P.groups = groups;
  if (P.group && !groupParam()) P.group = '';   // groupe absent de ce filtre
  const [counts, assets, months] = await Promise.all([api(`/api/photos/counts?group=${encodeURIComponent(groupParam())}${scopeQS()}`), api(photosURL(0)),
    api(`/api/photos/months?filter=${P.filter}&group=${encodeURIComponent(groupParam())}${scopeQS()}`).catch(() => [])]);
  if (seq !== P.seq) return;
  [P.counts, P.assets, P.months] = [counts, assets, months];
  P.more = P.assets.length === PAGE;
  $('#pfilters').innerHTML = FILTERS.filter(f => P.filter === f.id || (!f.onlyActive && (!f.hideEmpty || P.counts[f.id])))
    .map(f => `<button class="chip${P.filter === f.id ? ' on' : ''}" data-f="${f.id}">${t(`filter.${f.id}`)}<span class="n">${P.counts[f.id] ?? ''}</span></button>`).join('');
  renderTools();
  renderGrid();
  renderTimeline();
}
$('#pfilters').onclick = e => {
  const b = e.target.closest('[data-f]'); if (!b) return;
  P.filter = P.userFilter = b.dataset.f; clearSel(); gridKey(); setQuery({ filter: P.filter, base: null });
  try { localStorage.setItem('photos-filter', P.filter); } catch (err) {}
  loadPhotos();
};
/* État de la grille (d'après l'adresse) ; la sélection ne survit pas à un changement de liste. */
const gridKey = () => { const k = JSON.stringify([P.filter, P.group, P.scope]); if (k !== P.key) { P.key = k; setSelecting(false); } };
function setGridState(st) { Object.assign(P, st); gridKey(); }
/* Grille déplacée dans une fiche (espèce, rang) : mêmes tuiles, sélection et actions que la page Photos. */
function embedGrid(slot, scope, onStar) {
  setGridState({ filter: 'all', group: '', sort: 'date', base: 0, scope, embedded: true, onStar: onStar || null });
  slot.append($('#pblock'));
  return loadPhotos(0);
}
function homeGrid() {
  P.embedded = false; P.onStar = null; P.more = false; P.seq++;
  $('#v-photos').append($('#pblock'));
}
/* Portée de la liste, chaque élément retirable (une fiche montre déjà son espèce ou son rang). */
function scopeLabel(k, v) {
  if (k === 'taxon') return P.scopeNames[v] ?? String(v);
  if (k === 'rank') return higherNames(P.scope.name).main;
  if (k === 'day') return fmtDate(`${v}T12:00:00`);
  if (k === 'month') return monthName(v);
  if (k === 'camera') return v || t('stats.unknown_camera');
  return String(v);
}
function scopeChips() {
  const keys = Object.keys(P.scope).filter(k => k !== 'name' && !(P.embedded && ['taxon', 'rank'].includes(k)));
  if (P.scope.taxon && !P.scopeNames[P.scope.taxon]) {   // nom de l'espèce, récupéré une fois
    const k = P.scope.taxon;
    api(`/api/taxon/${k}`).then(tx => { P.scopeNames[k] = shortName(tx); if (P.scope.taxon === k) renderTools(); }).catch(() => {});
  }
  return keys.map(k => `<button class="chip on" data-unscope="${k}" title="${esc(t('species.f_remove'))}">${esc(scopeLabel(k, P.scope[k]))} ✕</button>`).join('');
}
/* Groupes, tri, relance des analyses en échec. */
function renderTools() {
  const agg = new Map();
  P.groups.forEach(x => { const g = rawGroup(x.group); agg.set(g, (agg.get(g) || 0) + x.n); });   // crée les groupes à la volée
  const sorts = ['date', 'confident', 'uncertain'];
  const groups = agg.size > 1 || P.group
    ? `<button class="chip${P.group ? '' : ' on'}" data-g="">${t('photos.all_groups')}</button>` + groupChips(GROUPS.filter(g => agg.has(g.id)), P.group,
      g => `<button class="chip${P.group === g.id ? ' on' : ''}" data-g="${g.id}">${g.icon} ${esc(g.name)}<span class="n">${agg.get(g.id)}</span></button>`, g => agg.get(g.id))
    : '';
  // À gauche ce qui filtre, à droite ce qui change l'affichage.
  $('#ptools').innerHTML = `<div class="pgroups">${scopeChips()}${groups}</div><div class="pview">`
    + (['pending', 'audit'].includes(P.filter) ? `<select id="psort" title="${esc(t('photos.sort'))}">${sorts.map(k => `<option value="${k}"${P.sort === k ? ' selected' : ''}>${t(`sort.${k}`)}</option>`).join('')}</select>` : '')
    + (P.filter === 'failed' && P.counts.failed ? `<button class="btn" id="b-requeue">${t('photos.requeue')}</button>` : '')
    + (!P.embedded && sortOf() === 'date' && P.months.length > 1 ? `<select id="pgoto" title="${esc(t('photos.goto'))}"><option value="">${t('photos.goto')}</option>${P.months.map(x =>
        `<option value="${x.m ?? ''}">${esc(monthLabel(x.m))} (${fmtN(x.n)})</option>`).join('')}</select>` : '')
    + (sortOf() === 'date' ? `<button class="chip${P.stack ? ' on' : ''}" id="b-stack" title="${esc(t('photos.stack_sub'))}">${t('photos.stack')}</button>` : '') + '</div>';
  $$('#ptools [data-g]').forEach(b => b.onclick = () => { P.group = b.dataset.g; gridKey(); if (!P.embedded) setQuery({ group: P.group, base: null }); loadPhotos(); });
  $$('#ptools [data-unscope]').forEach(b => b.onclick = () => {
    const k = b.dataset.unscope; setQuery({ [k]: null, ...(k === 'rank' ? { name: null } : {}) }); reloadPage(); });
  $('#b-stack') && ($('#b-stack').onclick = () => {
    P.stack = !P.stack; try { localStorage.setItem('photos-stack', P.stack ? '1' : '0'); } catch (err) {}
    renderTools(); renderGrid();
  });
  $('#pgoto') && ($('#pgoto').onchange = e => { const v = e.target.value; e.target.value = ''; if (v !== '' || P.months.some(x => x.m == null)) goToMonth(v || null); });
  $('#psort') && ($('#psort').onchange = e => { P.sort = P.userSort = e.target.value; setQuery({ sort: P.sort, base: null }); try { localStorage.setItem('photos-sort', P.sort); } catch (err) {} loadPhotos(); });
  $('#b-requeue') && ($('#b-requeue').onclick = async () => {
    try { const r = await api('/api/requeue-failed', { method: 'POST' }); toast(t('photos.requeued', { n: r.requeued })); loadPhotos(); loadStatus(); }
    catch (e) { fail(e); }
  });
}
/* Sélection de photos (coche d'une tuile, appui long au doigt ; raccourcis : Ctrl/Maj + clic, glisser à la souris) et
   relecture en lot. Tant que quelque chose est sélectionné, un clic sur une photo la coche au lieu de l'ouvrir.
   « Tout sélectionner » prend toutes les photos du filtre, chargées ou non (P.allSel), sauf celles décochées (P.excl). */
const isSel = id => P.allSel ? !P.excl.has(id) : P.sel.has(id);
const selCount = () => P.allSel ? Math.max(0, (P.counts[P.filter] ?? 0) - P.excl.size) : P.sel.size;
function setSel(id, on) {
  if (P.allSel) on ? P.excl.delete(id) : P.excl.add(id); else on ? P.sel.add(id) : P.sel.delete(id);
}
function clearSel() { P.sel.clear(); P.excl.clear(); P.allSel = false; }
function refreshSel() {
  $$('#pgrid .tile').forEach(t => t.classList.toggle('sel', isSel(P.assets[+t.dataset.i]?.asset_id)));
  // Dernière photo décochée : fin de la sélection.
  if (P.selecting && !selCount() && !P.allSel) { setSelecting(false); return; }
  renderSelbar();
}
function setSelecting(on) {
  P.selecting = on; if (!on) clearSel();
  $('#pgrid').classList.toggle('selecting', on);
  if (on) renderSelbar(); else { $$('#pgrid .tile.sel').forEach(x => x.classList.remove('sel')); renderSelbar(); }
}
function toggleSel(i, range) {
  // Plage dans l'ordre affiché (la grille regroupe par mois, pas forcément dans l'ordre de la liste).
  const shown = $$('#pgrid .tile').map(t => +t.dataset.i), from = shown.indexOf(P.lastSel), to = shown.indexOf(i);
  const ids = (range && from >= 0 ? shown.slice(Math.min(from, to), Math.max(from, to) + 1) : [i]).flatMap(idsOf);
  const on = range || !isSel(P.assets[i].asset_id);
  P.selecting = true; $('#pgrid').classList.add('selecting');
  ids.forEach(id => setSel(id, on));
  P.lastSel = i;
  refreshSel();
}
function renderSelbar() {
  const n = selCount(), bar = $('#selbar');
  bar.hidden = !n && !P.selecting;
  if (bar.hidden) return;
  const total = P.counts[P.filter] ?? P.assets.length;
  const off = n ? '' : ' disabled';
  const taxonBtn = `<button class="btn" data-b="taxon"${off} title="${esc(t('bulk.taxon_hint'))}">${ic('paw')} ${t('bulk.taxon')}</button>`;
  const rejectBtn = `<button class="btn" data-b="reject"${off} title="${esc(t('bulk.reject_hint'))}">${t('action.reject')}</button>`;
  // Mêmes libellés que dans la visionneuse. Sur téléphone, Espèce et Rejeter passent dans le menu (.onlyphone).
  bar.innerHTML = `<button class="iconbtn" data-b="none" title="${esc(t('bulk.done'))}" aria-label="${esc(t('bulk.done'))}">${ic('close')}</button><b>${t('count.photos', { n })}</b>
    <button class="btn primary" data-b="confirm"${off} title="${esc(t('bulk.confirm_hint'))}">${ic('check')} ${t('action.confirm')}</button>
    <span class="notphone">${taxonBtn}${rejectBtn}</span>
    ${P.allSel && !P.excl.size ? '' : `<button class="btn notphone" data-b="all">${t('bulk.all')} (${total})</button>`}
    <details class="selmore"><summary class="btn">${t('bulk.more')}</summary><div class="menu">
      <span class="onlyphone">${taxonBtn}${rejectBtn}${P.allSel && !P.excl.size ? '' : `<button class="btn" data-b="all">${t('bulk.all')} (${total})</button>`}</span>
      <button class="btn" data-b="loc"${off} title="${esc(t('loc.bulk_hint'))}">📍 ${t('loc.bulk')}</button>
      <button class="btn" data-b="reset"${off} title="${esc(t('bulk.reset_hint'))}">${t('action.reset')}</button>
      <button class="btn" data-b="reanalyze"${off} title="${esc(t('bulk.reanalyze_hint'))}">${ic('rescan')} ${t('bulk.reanalyze')}</button>
      <button class="btn" data-b="export"${off} title="${esc(t('bulk.export_hint'))}">${t('bulk.export')}</button>
      <button class="btn" data-b="invert" title="${esc(t('bulk.invert_hint'))}">${t('bulk.invert')}</button>
      <button class="btn danger" data-b="delete"${off} title="${esc(t('action.delete_hint'))}">${t('action.delete')}</button></div></details>`;
}
/* Inverser : toutes les photos du filtre sauf celles choisies, ou l'inverse (sans tout charger). */
function invertSel() {
  if (P.allSel) { P.sel = new Set(P.excl); P.excl.clear(); P.allSel = false; }
  else { P.excl = new Set(P.sel); P.sel.clear(); P.allSel = true; }
  refreshSel();
}
/* Choix d'une espèce (une photo ou une sélection) : recherche dans GBIF, nom commun ou latin. */
function pickTaxon(title, sub = '') {
  const dlg = $('#taxpick');
  dlg.innerHTML = `<form method="dialog"><h2>${title}</h2>${sub ? `<p class="sub">${sub}</p>` : ''}
    <label class="field">${ic('search')}<input id="tp-q" autocomplete="off" placeholder="${esc(t('cand.search'))}"></label>
    <div id="tp-res" class="tpres"></div><div class="actions"><button class="btn" value="">${t('common.cancel')}</button></div></form>`;
  return new Promise(resolve => {
    let tm, chosen = null;
    const inp = $('#tp-q');
    inp.oninput = () => { clearTimeout(tm); tm = setTimeout(async () => {
      if (inp.value.length < 3) { $('#tp-res').innerHTML = ''; return; }
      const res = await api(`/api/taxa/search?q=${encodeURIComponent(inp.value)}`).catch(() => []);
      $('#tp-res').innerHTML = res.map(r => `<button type="button" data-key="${r.gbif_key}"><i>${esc(r.name)}</i> ${esc(r.common)} <small>${esc(rankName(r.rank))}</small></button>`).join('');
      $$('#tp-res button').forEach(b => b.onclick = () => { chosen = { gbif_key: +b.dataset.key, label: b.innerHTML }; dlg.close(); });
    }, 300); };
    dlg.onclose = () => resolve(chosen);
    dlg.showModal(); inp.focus();
  });
}
/* Export CSV de la sélection (« tout » : les identifiants sont demandés au serveur, page par page). */
async function exportSelection() {
  let ids = [...P.sel];
  if (P.allSel) {
    ids = [];
    for (let off = 0; ; off += 1000) {
      const page = await api(`/api/photos?filter=${P.filter}&limit=1000&offset=${off}&group=${encodeURIComponent(groupParam())}${scopeQS()}`);
      ids.push(...page.map(a => a.asset_id).filter(id => !P.excl.has(id)));
      if (page.length < 1000) break;
    }
  }
  const r = await fetch(`/api/export.csv?lang=${encodeURIComponent(LANG)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_ids: ids }) });
  if (!r.ok) throw new Error(await r.text());
  const a = document.createElement('a');
  a.href = URL.createObjectURL(await r.blob()); a.download = 'taxonero.csv';
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
$('#selbar').onclick = async e => {
  const b = e.target.closest('[data-b]'); if (!b || b.disabled) return;
  const k = b.dataset.b;
  if (k === 'all') { clearSel(); P.allSel = true; P.selecting = true; refreshSel(); return; }
  if (k === 'none') { setSelecting(false); return; }
  if (k === 'invert') { invertSel(); return; }
  if (k === 'export') { exportSelection().catch(fail); return; }
  const sel = P.allSel ? { filter: P.filter, every: true, group: groupParam(), scope: P.scope, exclude: [...P.excl] } : { filter: P.filter, asset_ids: [...P.sel] };
  if (k === 'loc') {
    const p = await pickLocation({ canClear: true }); if (!p) return;
    try {
      const r = await sendLocation(sel, p);
      if (r.previous.length) pushUndo({ location: r.previous });
      setSelecting(false); await loadPhotos(P.base);
    } catch (err) { fail(err); }
    return;
  }
  const n0 = selCount();
  const body = { ...sel, action: k };
  if (k === 'taxon') {
    const tx = await pickTaxon(t('bulk.taxon_title', { n: n0 }), t('bulk.taxon_sub')); if (!tx) return;
    body.gbif_key = tx.gbif_key;
  } else if (n0 > PAGE && !confirm(t(`bulk.ask_${k}`, { n: n0 }))) return;
  try {
    const r = await api('/api/photos/bulk', { method: 'POST', body: JSON.stringify(body) });
    const n = r.changed.length + r.created.length;
    if (n) pushUndo({ bulk: { changed: r.changed, created: r.created } });
    setSelecting(false);
    await afterBulk(); loadStatus();
    if (k === 'reanalyze') {   // même comportement qu'à l'unité : l'analyse part sans attendre
      toast(t('bulk.reanalyze_done', { n: r.requeued }));
      api('/api/sync', { method: 'POST' }).then(() => pollSync(false)).catch(() => {});
      return;
    }
    const done = { confirm: 'bulk.confirmed', reject: 'bulk.rejected', reset: 'bulk.reset_done', taxon: 'bulk.taxon_done', delete: 'bulk.deleted_done' }[k];
    toast(t(done, { n })
      + (r.skipped ? ` · ${t(k === 'taxon' ? 'bulk.taxon_skipped' : 'bulk.skipped', { n: r.skipped })}` : '')
      + (n ? ` · <button id="t-undo">${t('action.undo')}</button>` : ''));
    $('#t-undo') && ($('#t-undo').onclick = undo);
  } catch (err) { fail(err); }
};
// Après une action en lot : une fiche se recharge entière (chiffres, photo principale), Photos garde sa liste.
const afterBulk = () => P.embedded ? (reloadPage(), Promise.resolve()) : loadPhotos(P.base);
/* Intertitre d'un mois, en mode sélection : toutes ses photos affichées (re-clic : aucune). */
$('#pgrid').addEventListener('click', e => {
  const b = e.target.closest('.msel'); if (!b) return;
  e.stopPropagation();
  const tiles = [...(b.closest('.month').nextElementSibling?.querySelectorAll('.tile') || [])];
  const ids = tiles.flatMap(x => idsOf(+x.dataset.i)), on = !ids.every(isSel);
  P.selecting = true; $('#pgrid').classList.add('selecting');
  ids.forEach(id => setSel(id, on));
  refreshSel();
}, true);
function tileStatus(a) {
  const live = a.detections.filter(d => d.status !== 'rejected');
  const pending = live.filter(d => d.status === 'pending').length;
  const ok = live.filter(d => d.status === 'confirmed');
  const out = [];
  if (pending) out.push(`<span class="pending">${t('tile.pending', { n: pending })}</span>`);
  if (ok.length) out.push(`<span class="ok">✓ ${ok.some(d => d.confirmed_by === 'user') ? t('tile.confirmed') : t('tile.auto')}</span>`);
  if (!live.length && a.detections.length) out.push(`<span class="rej">${t('tile.rejected')}</span>`);
  if (!a.analyzed_at && !STATE.status?.manual) out.push(a.fail_count >= (STATE.status?.max_failures ?? 3)
    ? `<span class="rej" title="${esc(a.last_error || '')}">${t('tile.failed')}</span>` : `<span>${t('tile.queued')}</span>`);
  return out.join('');
}

/* Tuile de photo : sélection, pile de série ; nombre de sujets sur la page Photos, date et lieu dans une fiche. */
function tileHTML(a, i) {
  const ar = a.width && a.height ? Math.min(2.2, Math.max(0.6, a.width / a.height)) : 1.4;
  const live = a.detections.filter(d => d.status !== 'rejected');
  const names = [...new Set(live.map(d => shortName(d.taxon)))].join(', ');
  const date = a.taken_at ? fmtDate(a.taken_at, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
  const shade = !P.embedded ? (names && `<b>${esc(names)}</b><small>${t('count.subjects', { n: live.length })}</small>`)
    : `<b>${esc(names || date)}</b><small>${esc([date, a.city].filter(Boolean).join(' · '))}</small>`;
  return `<div class="tile${isSel(a.asset_id) ? ' sel' : ''}" data-i="${i}" role="button" tabindex="0" aria-label="${esc([names, date].filter(Boolean).join(', ') || t('nav.photos'))}" style="flex-grow:${ar};width:${Math.round(ar * 210)}px"><span class="stack" hidden></span>
    <img loading="lazy" draggable="false" src="/api/thumb/${a.asset_id}?size=thumbnail" alt="">
    <div class="st">${tileStatus(a)}</div>${a.kind === 'VIDEO' ? `<span class="vid" title="${esc(t('video.keyframe'))}">▶</span>` : ''}<i class="tick" title="${esc(t('photos.select_hint'))}"></i>
    ${shade ? `<div class="shade">${shade}</div>` : ''}</div>`;
}
const monthKey = d => d.toLocaleDateString(LANG, { month: 'long', year: 'numeric' });
// Intertitre d'une photo : mois comme la timeline Immich, ou année si les photos sont trop éparses ; triées par
// confiance, un seul bloc dans l'ordre de la liste (celui des flèches de la visionneuse).
function sectionOf(a) {
  if (sortOf() !== 'date') return t(`sort.${sortOf()}`);
  const d = a.taken_at ? new Date(a.taken_at) : null;
  return d ? (P.byYear ? String(d.getFullYear()) : monthKey(d)) : t('photos.no_date');
}
const sectionHead = (k, n) => `<div class="month">${esc(k[0].toLocaleUpperCase(LANG) + k.slice(1))}<span>${t('count.photos', { n })}</span>
  <button class="msel" title="${esc(t('bulk.section_hint'))}">${t('bulk.section')}</button></div>`;
// Mois d'une photo, comme le serveur le compte pour la frise (« AAAA-MM », null sans date).
const keyOf = a => a.taken_at ? a.taken_at.slice(0, 7) : null;
const monthLabel = m => { if (!m) return t('photos.no_date');
  const s = new Date(+m.slice(0, 4), +m.slice(5, 7) - 1, 15).toLocaleDateString(LANG, { month: 'long', year: 'numeric' });
  return s[0].toLocaleUpperCase(LANG) + s.slice(1); };
/* Ajoute les photos P.assets[P.shown..] à la grille sans toucher aux tuiles déjà affichées : avec des milliers de
   photos chargées, tout reconstruire à chaque page rendait le défilement de plus en plus lent. */
function appendGrid() {
  const grid = $('#pgrid');
  grid.querySelector('#pmore')?.remove();
  const sections = [];
  for (let i = P.shown; i < P.assets.length; i++) {
    const k = sectionOf(P.assets[i]), last = sections[sections.length - 1];
    if (last && last.k === k) last.idx.push(i); else sections.push({ k, idx: [i] });
  }
  const grown = new Set();
  for (const { k, idx } of sections) {
    // Pile : les photos suivantes de la même série s'ajoutent à la tuile de tête au lieu d'avoir la leur.
    const html = idx.map(i => {
      const sid = P.assets[i].series_id, lead = P.lastLead;
      if (stacking() && sid && lead != null && P.assets[lead].series_id === sid) { P.members.get(lead).push(i); grown.add(lead); return ''; }
      P.lastLead = i; P.members.set(i, [i]);
      return tileHTML(P.assets[i], i);
    }).join('');
    if (k === P.lastSection) {   // suite du dernier mois affiché
      P.sectionN += idx.length;
      [...grid.querySelectorAll('.month')].pop().outerHTML = sectionHead(k, P.sectionN);
      grid.lastElementChild.insertAdjacentHTML('beforeend', html);
    } else {
      grid.insertAdjacentHTML('beforeend', sectionHead(k, idx.length) + `<div class="justified">${html}</div>`);
      P.lastSection = k; P.sectionN = idx.length;
    }
  }
  P.shown = P.assets.length;
  for (const lead of grown) showStack(grid, lead);
  if (P.onStar) starTiles();
  if (P.more) {
    grid.insertAdjacentHTML('beforeend', `<div id="pmore" class="empty">${t('common.loading')}</div>`);
    const io = new IntersectionObserver(es => { if (es[0].isIntersecting) { io.disconnect(); morePhotos(); } }, { rootMargin: '600px' });
    io.observe($('#pmore'));
  }
}
/* Page suivante ; en échec, message et nouvel essai au lieu d'un « Chargement… » sans fin. */
function morePhotos() {
  loadMorePhotos().catch(err => {
    const el = $('#pmore'); if (!el) return;
    el.setAttribute('role', 'alert');
    el.innerHTML = `${t('err.load_failed', { msg: esc(err.message || err) })}<br><button class="btn">${t('common.retry')}</button>`;
    el.querySelector('button').onclick = () => { el.textContent = t('common.loading'); morePhotos(); };
  });
}
function showStack(grid, lead) {
  const n = P.members.get(lead)?.length, badge = grid.querySelector(`.tile[data-i="${lead}"] .stack`);
  if (badge && n > 1) { badge.hidden = false; badge.textContent = `×${n}`; badge.title = t('photos.stack_hint', { n }); }
}
function renderGrid() {
  $('#pgrid').classList.toggle('selecting', P.selecting);
  renderSelbar();
  P.shown = 0; P.lastSection = null; P.members = new Map(); P.lastLead = null;
  if (!P.assets.length) {
    const msg = { pending: t('photos.empty_pending'), none: t('photos.empty_none') }[P.filter] || t('photos.empty');
    $('#pgrid').innerHTML = `<div class="empty">${msg}</div>`; return;
  }
  const nbMonths = new Set(P.assets.filter(a => a.taken_at).map(a => monthKey(new Date(a.taken_at)))).size;
  P.byYear = P.assets.length / Math.max(1, nbMonths) < 3;   // décidé sur la première page, gardé ensuite
  // Après un saut dans la frise, les photos plus récentes ne sont pas chargées : bouton pour y revenir.
  $('#pgrid').innerHTML = P.base ? `<div class="pnewer"><button class="btn" id="pnewer">↑ ${t('photos.newer')}</button></div>` : '';
  appendGrid();
  if (P.base) {   // chargées seules en remontant jusqu'en haut (zone sous la barre du haut exclue), bouton en secours
    let busy = false;
    const go = () => { if (busy) return; busy = true; io.disconnect(); loadNewer(); };
    const io = new IntersectionObserver(es => { if (es[0].isIntersecting && P.armed) go(); },
      { rootMargin: `-${Math.round(($('.topbar')?.getBoundingClientRect().bottom ?? 64) + 8)}px 0px 0px 0px` });
    P.armed = false; setTimeout(() => { P.armed = true; }, 800);   // pas pendant le saut lui-même
    io.observe($('#pnewer')); $('#pnewer').onclick = go;
  }
}
/* Frise de défilement par dates, comme celle d'Immich : toute la hauteur de l'écran, chaque mois à proportion de
   ses photos (avec un minimum, pour que les petits mois restent atteignables). Survol : mois sous le pointeur ;
   clic ou glisser : la liste va à cette position, à la photo près. Clavier : flèches (mois), Page (année). */
const TL_MIN = 0.25;   // place minimale d'un mois : un quart de la place moyenne
function tlLayout() {
  const total = P.months.reduce((s, x) => s + x.n, 0), floor = TL_MIN * total / P.months.length;
  const w = P.months.map(x => Math.max(x.n, floor)), W = w.reduce((s, x) => s + x, 0);
  let off = 0, y = 0;
  P.tl = P.months.map((x, i) => { const r = { ...x, off, y0: y / W, h: w[i] / W }; off += x.n; y += w[i]; return r; });
}
const yearOf = i => P.tl[i]?.m?.slice(0, 4) ?? '?';
const tlAt = f => P.tl.find(r => f < r.y0 + r.h) || P.tl[P.tl.length - 1];   // mois à une hauteur (0 à 1)
function tlOffsetAt(f) { const r = tlAt(f); return r.off + Math.min(r.n - 1, Math.max(0, Math.floor((f - r.y0) / r.h * r.n))); }
function tlFrac(off) { const r = P.tl.find(x => off < x.off + x.n) || P.tl[P.tl.length - 1]; return r.y0 + r.h * Math.min(1, (off - r.off) / Math.max(1, r.n)); }
function renderTimeline() {
  const nav = $('#ptimeline');
  nav.hidden = P.embedded || sortOf() !== 'date' || P.months.length < 2;
  if (nav.hidden) return;
  tlLayout();
  nav.innerHTML = `<div class="tlmarks"></div><div class="tlpos"></div><div class="tlhover" hidden><span></span></div>`;
  Object.assign(nav, { tabIndex: 0 });
  nav.setAttribute('role', 'slider'); nav.setAttribute('aria-orientation', 'vertical');
  nav.setAttribute('aria-valuemin', 0); nav.setAttribute('aria-valuemax', P.tl.length - 1);
  drawTimeline();
  const frac = e => { const r = nav.getBoundingClientRect(); return Math.min(0.9999, Math.max(0, (e.clientY - r.top) / r.height)); };
  const hover = e => { const f = frac(e), m = tlAt(f), h = nav.querySelector('.tlhover');
    h.hidden = false; h.style.top = `${f * 100}%`;
    h.firstChild.textContent = `${monthLabel(m.m)} · ${t('count.photos', { n: m.n })}`; };
  let drag = false;
  nav.onpointerdown = e => { if (e.button) return; drag = true; nav.setPointerCapture(e.pointerId); hover(e); e.preventDefault(); };
  nav.onpointermove = e => { if (drag || e.pointerType === 'mouse') hover(e); };
  nav.onpointerup = e => { if (!drag) return; drag = false;
    const f = frac(e), m = tlAt(f), off = tlOffsetAt(f);
    P.tlKey = P.tl.indexOf(m); P.tlKeyAt = Date.now();
    goToOffset(off).then(() => { P.tlKeyAt = Date.now(); showTlMonth(m, off); });
    if (e.pointerType !== 'mouse') nav.querySelector('.tlhover').hidden = true; };
  nav.onpointercancel = () => { drag = false; nav.querySelector('.tlhover').hidden = true; };
  nav.onpointerleave = () => { if (!drag) nav.querySelector('.tlhover').hidden = true; };
  nav.onkeydown = e => {
    // Mois choisi au clavier gardé : en bas de liste, la page ne défile plus jusqu'à lui et la photo du haut de
    // l'écran serait encore celle d'un mois plus récent (Page bas resterait sur place).
    const cur = P.tlKey != null ? P.tlKey : P.tl.findIndex(r => r.m === nav.dataset.m), step = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -1, ArrowRight: 1 }[e.key];
    let i = null;
    if (step) i = cur + step;
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = P.tl.length - 1;
    else if (e.key === 'PageDown') {   // premier mois de l'année précédente (plus ancienne)
      i = cur + 1; while (i < P.tl.length && yearOf(i) === yearOf(cur)) i++;
    } else if (e.key === 'PageUp') {   // premier mois de l'année suivante (plus récente)
      i = cur; while (i > 0 && yearOf(i - 1) === yearOf(cur)) i--;
      i--; if (i >= 0) { const y = yearOf(i); while (i > 0 && yearOf(i - 1) === y) i--; }
    }
    if (i == null) return;
    e.preventDefault();
    i = Math.max(0, Math.min(P.tl.length - 1, i));
    P.tlKey = i; P.tlKeyAt = Date.now();
    goToOffset(P.tl[i].off).then(() => { P.tlKeyAt = Date.now(); showTlMonth(P.tl[i], P.tl[i].off); });
  };
  markTimeline();
}
/* Repères : années (si la place le permet) et un point par mois. Redessinés quand la hauteur change. */
function drawTimeline() {
  const nav = $('#ptimeline'), H = nav.clientHeight; if (!H || !P.tl) return;
  let lastLabel = -Infinity, lastDot = -Infinity, year = null, html = '';
  for (const r of P.tl) {
    const y = r.y0 * H, yr = r.m ? r.m.slice(0, 4) : '?';
    if (yr !== year) { year = yr;
      if (y - lastLabel >= 18) { html += `<span class="tlyear" style="top:${y}px">${yr === '?' ? '…' : yr}</span>`; lastLabel = y; lastDot = y; continue; } }
    if (y - lastDot >= 5 && y - lastLabel >= 12) { html += `<i class="tldot" style="top:${y + r.h * H / 2}px"></i>`; lastDot = y; }
  }
  nav.querySelector('.tlmarks').innerHTML = html;
}
window.addEventListener('resize', () => { if (STATE.view === 'photos' && !$('#ptimeline').hidden) drawTimeline(); });
/* Va à la n-ième photo de la liste (dans l'ordre par date) : défile si elle est chargée, sinon la liste repart du
   début de son mois (au plus 50 photos avant elle) : intertitre du mois en tête, et en haut de la frise on repart
   bien de la toute première photo. */
async function goToOffset(off) {
  if (off < P.base || off >= P.base + P.assets.length) {
    const m = P.tl ? P.tl.find(r => off < r.off + r.n) : null, base = Math.max(m ? m.off : off, off - 50);
    setQuery({ base: base || null });
    await loadPhotos(base);
    if (off === base) { const first = $('#pgrid .month'); first ? first.scrollIntoView({ block: 'start' }) : window.scrollTo(0, 0); return; }
  }
  const i = off - P.base, lead = [...P.members].find(([, mem]) => mem.includes(i))?.[0] ?? i;
  let el = $(`#pgrid .tile[data-i="${lead}"]`); if (!el) return;
  const block = el.parentElement;   // première photo du bloc : on montre aussi l'intertitre
  if (block.firstElementChild === el && block.previousElementSibling?.classList.contains('month')) el = block.previousElementSibling;
  el.scrollIntoView({ block: 'start' });
}
function goToMonth(m) { const r = P.months.reduce((acc, x) => acc.done ? acc : x.m === m ? { off: acc.off, done: true } : { off: acc.off + x.n }, { off: 0 }); return goToOffset(r.off); }
/* Après un saut, les photos plus récentes ne sont pas chargées : en remontant jusqu'en haut, la page précédente
   arrive, et la photo regardée reste en place. */
async function loadNewer() {
  const old = P.base, base = Math.max(0, old - PAGE);
  setQuery({ base: base || null });
  await loadPhotos(base);
  while (P.assets.length <= old - base && P.more) if (!await loadMorePhotos()) break;
  const i = old - base, lead = [...P.members].find(([, mem]) => mem.includes(i))?.[0] ?? i;
  $(`#pgrid .tile[data-i="${lead}"]`)?.scrollIntoView({ block: 'start' });
}
/* Photo en haut de l'écran : repère dans la frise. */
function markTimeline() {
  const nav = $('#ptimeline'), grid = $('#pgrid');
  if (nav.hidden || STATE.view !== 'photos' || !P.tl) return;
  const r = grid.getBoundingClientRect(), top = $('.topbar')?.getBoundingClientRect().bottom ?? 64;
  const tl = document.elementFromPoint(r.left + 30, Math.max(top, r.top) + 70)?.closest('#pgrid .tile');
  if (P.tlKey != null && Date.now() - P.tlKeyAt < 400) return;   // défilement causé par le clavier
  P.tlKey = null;   // défilement de l'utilisateur : la frise suit de nouveau la page
  const off = P.base + (tl ? +tl.dataset.i : 0);
  showTlMonth(tlAt(tlFrac(off)), off);
}
function showTlMonth(m, off) {
  const nav = $('#ptimeline');
  nav.querySelector('.tlpos').style.top = `${tlFrac(off) * 100}%`;
  nav.dataset.m = m.m ?? '';
  nav.setAttribute('aria-valuenow', P.tl.indexOf(m)); nav.setAttribute('aria-valuetext', monthLabel(m.m));
}
let tlTick = false;
window.addEventListener('scroll', () => { if (!tlTick) { tlTick = true; requestAnimationFrame(() => { tlTick = false; markTimeline(); }); } }, { passive: true });
/* Clavier dans la grille : flèches d'une photo à l'autre (haut et bas : la photo la plus proche de la rangée
   voisine). Entrée ouvre, Espace sélectionne en mode sélection (core.js). */
$('#pgrid').addEventListener('keydown', e => {
  const tl = e.target.closest?.('.tile'); if (!tl || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
  const tiles = $$('#pgrid .tile'), k = tiles.indexOf(tl);
  let next = null;
  if (e.key === 'ArrowRight') next = tiles[k + 1];
  else if (e.key === 'ArrowLeft') next = tiles[k - 1];
  else {
    const r = tl.getBoundingClientRect(), cx = r.left + r.width / 2, down = e.key === 'ArrowDown';
    const row = tiles.map(x => x.getBoundingClientRect()).filter(q => down ? q.top >= r.bottom - 2 : q.bottom <= r.top + 2);
    const edge = down ? Math.min(...row.map(q => q.top)) : Math.max(...row.map(q => q.bottom));
    let best = Infinity;
    tiles.forEach(x => { const q = x.getBoundingClientRect();
      if ((down ? q.top : q.bottom) !== edge) return;
      const d = Math.abs(q.left + q.width / 2 - cx); if (d < best) { best = d; next = x; } });
  }
  if (!next) return;
  e.preventDefault();
  next.focus();
  next.scrollIntoView({ block: 'nearest' });
});
/* Retour de la visionneuse : seules les tuiles des photos relues sont redessinées, la grille et sa position restent. */
function refreshTiles(indices) {
  for (const i of indices) {
    const el = $(`#pgrid .tile[data-i="${i}"]`);
    if (el && P.assets[i]) { el.outerHTML = tileHTML(P.assets[i], i); showStack($('#pgrid'), i); }
  }
  if (P.onStar) starTiles();
  api(`/api/photos/counts?group=${encodeURIComponent(groupParam())}${scopeQS()}`).then(c => {
    P.counts = c;
    $$('#pfilters [data-f]').forEach(b => { const n = b.querySelector('.n'); if (n) n.textContent = c[b.dataset.f] ?? ''; });
  }).catch(() => {});
}
/* Glisser à la souris sur les photos en mode sélection : chaque photo survolée prend l'état de la première. */
let dragSel = null;
let longPress = null, longFired = false;
/* Appui long au doigt : première photo sélectionnée. */
$('#pgrid').addEventListener('pointerdown', e => {
  const tl = e.target.closest('.tile');
  if (!tl || e.pointerType === 'mouse') return;
  longFired = false; clearTimeout(longPress);
  longPress = setTimeout(() => { longFired = true; toggleSel(+tl.dataset.i); }, 450);
});
['pointerup', 'pointercancel', 'pointermove'].forEach(k => $('#pgrid').addEventListener(k, e => { if (k !== 'pointermove' || e.pointerType !== 'mouse') clearTimeout(longPress); }));
$('#pgrid').addEventListener('contextmenu', e => { if (e.target.closest('.tile') && (longFired || matchMedia('(pointer: coarse)').matches)) e.preventDefault(); });
$('#pgrid').addEventListener('pointerdown', e => {
  const tl = e.target.closest('.tile');
  if (!tl || !P.selecting || e.pointerType !== 'mouse' || e.button !== 0 || e.shiftKey) return;
  const id = P.assets[+tl.dataset.i].asset_id;
  dragSel = { first: tl, on: !isSel(id), moved: false };
  e.preventDefault();   // pas de sélection de texte ni de glisser d'image
});
// pointermove + elementFromPoint : pendant l'appui, Firefox envoie les événements à la tuile de départ.
$('#pgrid').addEventListener('pointermove', e => {
  if (!dragSel || !(e.buttons & 1)) return;
  const tl = document.elementFromPoint(e.clientX, e.clientY)?.closest('#pgrid .tile');
  if (!tl || (tl === dragSel.first && !dragSel.moved)) return;
  if (!dragSel.moved) { dragSel.moved = true; idsOf(+dragSel.first.dataset.i).forEach(id => setSel(id, dragSel.on)); dragSel.first.classList.toggle('sel', dragSel.on); }
  const i = +tl.dataset.i;
  if (isSel(P.assets[i].asset_id) === dragSel.on) return;
  idsOf(i).forEach(id => setSel(id, dragSel.on)); tl.classList.toggle('sel', dragSel.on); P.lastSel = i;
  renderSelbar();
});
window.addEventListener('pointerup', () => { if (dragSel && !dragSel.moved) dragSel = null; else if (dragSel) setTimeout(() => { dragSel = null; }); });
$('#pgrid').onclick = e => {
  if (dragSel?.moved) return;   // fin d'un glisser : déjà sélectionné
  if (longFired) { longFired = false; return; }   // fin d'un appui long
  const t = e.target.closest('.tile'); if (!t) return;
  const star = e.target.closest('.star');
  if (star) { P.onStar?.(P.assets[+t.dataset.i].asset_id); return; }
  if (P.selecting || e.target.closest('.tick') || e.ctrlKey || e.metaKey || e.shiftKey) { e.preventDefault(); toggleSel(+t.dataset.i, e.shiftKey); return; }
  openViewer(P.assets, +t.dataset.i);
};
// Menus « Plus » : refermés par un clic ailleurs ou sur l'un de leurs choix.
document.addEventListener('click', e => { $$('.selmore[open]').forEach(m => { if (!m.contains(e.target) || e.target.closest('.menu button, .menu a')) m.open = false; }); });
