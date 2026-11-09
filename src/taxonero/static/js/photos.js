/* ============================== Photos page ============================== */
const FILTERS = [
  { id: 'pending' }, { id: 'audit', hideEmpty: true }, { id: 'auto' }, { id: 'user' }, { id: 'rejected' }, { id: 'none' },
  { id: 'failed', hideEmpty: true }, { id: 'all' }, { id: 'identified', onlyActive: true },
];
const P = { filter: 'pending', userFilter: 'pending', userSort: 'date', scope: {}, embedded: false, key: '', onStar: null, scopeNames: {},
  assets: [], counts: {}, more: false, loading: false,
  sort: 'date', group: '', groups: [], kinds: '', kindCounts: {}, menu: null, sel: new Set(), allSel: false, excl: new Set(), selecting: false, lastSel: null,
  shown: 0, byYear: false, lastSection: null, sectionN: 0, seq: 0,
  // Stacked series (bursts): one "×N" tile per series, in date order. members: tile -> photos.
  stack: true, members: new Map(), lastLead: null,
  // Navigation: P.assets starts at photo no. P.base of the list (jump to a month of the timeline); months: timeline.
  base: 0, months: [] };
// Group (Species page) of a raw value returned by the server: class, or kingdom for plants and fungi.
const rawGroup = v => groupOf({ lineage: { class: v, phylum: v, kingdom: v } }).id;
/* Groups menu: what was detected (animal, plant, fungus, microbe), each with its groups (birds, mammals…).
   Kinds (P.kinds) and groups (P.group, ids of GROUPS) can be chosen together: a photo matches if it shows any of them.
   A chosen kind stands for all its groups. */
const KINDS = [{ id: 'animal', icon: '🐾' }, { id: 'plant', icon: '🌿' }, { id: 'fungus', icon: '🍄' }, { id: 'microbe', icon: '🦠' }];
const kindList = () => P.kinds ? P.kinds.split(',') : [];
const groupList = () => P.group ? P.group.split(',') : [];
// Both travel in the group parameter (server: split_group), so every list, count and bulk action gets them.
const groupParam = () => {
  const ks = kindList(), gs = groupList();
  const raws = P.groups.filter(x => gs.includes(rawGroup(x.group)) && !ks.includes(x.kind)).map(x => x.group);
  return [...ks.map(k => `kind:${k}`), ...new Set(raws)].join(',');
};
// Kinds present (or chosen), each with its groups, most photos first. kids: [{ g (GROUPS item), n }].
function groupTree() {
  const by = new Map();
  P.groups.forEach(x => {
    const g = groupOf({ lineage: { class: x.group, phylum: x.group, kingdom: x.group } }), k = x.kind || 'animal';
    if (!by.has(k)) by.set(k, new Map());
    const m = by.get(k), e = m.get(g.id) || { g, n: 0 };
    e.n += x.n; m.set(g.id, e);
  });
  return KINDS.filter(k => by.has(k.id) || P.kindCounts[k.id] || kindList().includes(k.id))
    .map(k => ({ ...k, n: P.kindCounts[k.id] ?? 0, kids: [...(by.get(k.id)?.values() || [])].sort((a, b) => b.n - a.n) }));
}
function setChoice(kinds, groups) {
  P.kinds = KINDS.map(k => k.id).filter(k => kinds.includes(k)).join(',');
  P.group = [...new Set(groups)].join(',');
  gridKey(); if (!P.embedded) setQuery({ kinds: P.kinds || null, group: P.group || null, base: null }); loadPhotos();
}
function toggleKind(id) {
  const kind = groupTree().find(k => k.id === id), kids = (kind?.kids || []).map(x => x.g.id);
  const ks = kindList(), gs = groupList();   // half chosen (some of its groups): the whole kind, like a tri-state box
  setChoice(ks.includes(id) ? ks.filter(k => k !== id) : [...ks, id], gs.filter(g => !kids.includes(g)));
}
function toggleGroup(id, kindId) {
  const kids = (groupTree().find(k => k.id === kindId)?.kids || []).map(x => x.g.id);
  let ks = kindList(), gs = groupList();
  if (ks.includes(kindId)) { ks = ks.filter(k => k !== kindId); gs = [...gs, ...kids.filter(g => g !== id)]; }   // all but this one
  else gs = gs.includes(id) ? gs.filter(g => g !== id) : [...gs, id];
  if (kids.length > 1 && kids.every(g => gs.includes(g))) { ks = [...ks, kindId]; gs = gs.filter(g => !kids.includes(g)); }   // all of them: the kind
  setChoice(ks, gs);
}
// Sorting by confidence only applies to photos awaiting review; elsewhere, by date.
const sortOf = () => ['pending', 'audit'].includes(P.filter) ? P.sort : 'date';
const scopeQS = () => { const q = new URLSearchParams(); Object.entries(P.scope).forEach(([k, v]) => q.set(k, v)); const s = q.toString(); return s ? `&${s}` : ''; };
const photosURL = off => `/api/photos?filter=${P.filter}&limit=${PAGE}&offset=${P.base + off}&sort=${sortOf()}&group=${encodeURIComponent(groupParam())}${scopeQS()}`;
const PAGE = 200;
try { P.filter = P.userFilter = localStorage.getItem('photos-filter') || 'pending'; P.sort = P.userSort = localStorage.getItem('photos-sort') || 'date';
  P.stack = localStorage.getItem('photos-stack') !== '0'; } catch (e) {}
const stacking = () => P.stack && sortOf() === 'date';
// Photos of a tile: the whole series for a stack, otherwise the photo alone.
const idsOf = i => (P.members.get(i) || [i]).map(k => P.assets[k].asset_id);

async function loadMorePhotos() {
  if (!P.more || P.loading) return false;
  P.loading = true;
  const seq = P.seq;
  try {
    const next = await api(photosURL(P.assets.length));
    if (seq !== P.seq) return false;   // filter changed in the meantime: page of another list
    P.assets.push(...next); P.more = next.length === PAGE;
    if (gridActive()) appendGrid();
    return next.length > 0;
  } finally { P.loading = false; }
}
async function loadPhotos(base = 0) {
  if (!FILTERS.some(f => f.id === P.filter)) { P.filter = 'pending'; if (!P.embedded) setQuery({ filter: P.filter }); }
  // Overlapping loads (filter changed before the end of the previous one): only the last one is shown.
  const seq = ++P.seq;
  P.base = base;
  const [groups, kindCounts] = await Promise.all([api(`/api/photos/groups?filter=${P.filter}${scopeQS()}`),
    api(`/api/photos/kinds?filter=${P.filter}${scopeQS()}`).catch(() => ({}))]);
  if (seq !== P.seq) return;
  [P.groups, P.kindCounts] = [groups, kindCounts];
  P.group = groupList().filter(g => P.groups.some(x => rawGroup(x.group) === g)).join(',');   // groups absent from this filter
  const [counts, assets, months] = await Promise.all([api(`/api/photos/counts?group=${encodeURIComponent(groupParam())}${scopeQS()}`), api(photosURL(0)),
    api(`/api/photos/months?filter=${P.filter}&group=${encodeURIComponent(groupParam())}${scopeQS()}`).catch(() => [])]);
  if (seq !== P.seq) return;
  [P.counts, P.assets, P.months] = [counts, assets, months];
  P.more = P.assets.length === PAGE;
  $('#pfilters').innerHTML = FILTERS.filter(f => P.filter === f.id || (!f.onlyActive && (!f.hideEmpty || P.counts[f.id])))
    .map(f => `<button role="tab" class="${P.filter === f.id ? 'on' : ''}" aria-selected="${P.filter === f.id}" data-f="${f.id}">${t(`filter.${f.id}`)}<span class="n">${fmtN(P.counts[f.id] ?? 0)}</span></button>`).join('');
  renderTools();
  renderGrid();
  renderTimeline();
  P.done = seq;   // this list is fully shown (the tests wait for it)
}
$('#pfilters').onclick = e => {
  const b = e.target.closest('[data-f]'); if (!b) return;
  P.filter = P.userFilter = b.dataset.f; clearSel(); gridKey(); setQuery({ filter: P.filter, base: null });
  try { localStorage.setItem('photos-filter', P.filter); } catch (err) {}
  loadPhotos();
};
/* State of the grid (from the address); the selection does not survive a change of list. */
const gridKey = () => { const k = JSON.stringify([P.filter, P.kinds, P.group, P.scope]); if (k !== P.key) { P.key = k; setSelecting(false); } };
function setGridState(st) { Object.assign(P, st); gridKey(); }
/* Grid moved into a record (species, rank): same tiles, selection and actions as the Photos page. */
function embedGrid(slot, scope, onStar) {
  setGridState({ filter: 'all', kinds: '', group: '', sort: 'date', base: 0, scope, embedded: true, onStar: onStar || null });
  slot.append($('#pblock'));
  return loadPhotos(0);
}
function homeGrid() {
  P.embedded = false; P.onStar = null; P.more = false; P.seq++;
  $('#v-photos').append($('#pblock'));
}
/* Scope of the list, each item removable (a record already shows its species or its rank). */
function scopeLabel(k, v) {
  if (k === 'taxon') return P.scopeNames[v] ?? String(v);
  if (k === 'rank') return higherNames(P.scope.name).main;
  if (k === 'day') return fmtDate(`${v}T12:00:00`);
  if (k === 'month') return monthName(v);
  if (k === 'camera') return v || t('stats.unknown_camera');
  if (k === 'source') return (STATE.status?.sources || []).find(x => x.id === v)?.label ?? v;
  return String(v);
}
function scopeChips(menus) {
  const keys = Object.keys(P.scope).filter(k => k !== 'name' && !(P.embedded && ['taxon', 'rank'].includes(k)) && !menus.includes(k));
  if (P.scope.taxon && !P.scopeNames[P.scope.taxon]) {   // name of the species, fetched once
    const k = P.scope.taxon;
    api(`/api/taxon/${k}`).then(tx => { P.scopeNames[k] = shortName(tx); if (P.scope.taxon === k) renderTools(); }).catch(() => {});
  }
  return keys.map(k => `<button class="chip on" data-unscope="${k}" title="${esc(t('species.f_remove'))}">${esc(scopeLabel(k, P.scope[k]))} ✕</button>`).join('');
}
/* Drop-down menu of the tool bar: a button that says what is chosen, and its list (open: P.menu). */
const dropdown = (id, label, active, body) => `<div class="pdrop"><button class="chip ddbtn${active ? ' on' : ''}" data-menu="${id}" aria-haspopup="true"
  aria-expanded="${P.menu === id}">${label}<svg class="ic dd" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5z"/></svg></button><div class="pmenu" data-for="${id}"${P.menu === id ? '' : ' hidden'}>${body}</div></div>`;
function closeMenus() {
  P.menu = null;
  $$('#ptools .pmenu').forEach(m => { m.hidden = true; });
  $$('#ptools [data-menu]').forEach(b => b.setAttribute('aria-expanded', 'false'));
}
document.addEventListener('click', e => { if (P.menu && !e.target.closest?.('#ptools .pdrop')) closeMenus(); });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !P.menu) return;
  const b = $(`#ptools [data-menu="${P.menu}"]`); closeMenus(); b?.focus(); e.stopPropagation();
}, true);
function groupsMenu() {
  const tree = groupTree(), ks = kindList(), gs = groupList();
  if (!tree.length || (tree.length === 1 && tree[0].kids.length < 2 && !ks.length && !gs.length)) return '';
  const names = [...tree.filter(k => ks.includes(k.id)).map(k => t(`kind.${k.id}`)),
    ...tree.flatMap(k => k.kids).filter(x => gs.includes(x.g.id)).map(x => x.g.name)];
  const label = !names.length ? t('photos.groups') : names.length > 2 ? `${esc(names[0])}, ${esc(names[1])} +${names.length - 2}` : esc(names.join(', '));
  const item = (attrs, on, mixed, icon, name, n, cls = '') => `<label class="mi${cls}"><input type="checkbox" ${attrs}${on ? ' checked' : ''}${mixed ? ' data-mixed' : ''}>
    <span class="mic">${icon}</span><span class="mname">${esc(name)}</span><span class="n">${fmtN(n)}</span></label>`;
  const body = tree.map(k => {
    const kids = k.kids.length > 1 || k.kids.some(x => gs.includes(x.g.id)) ? k.kids : [];
    const on = ks.includes(k.id), mixed = !on && kids.some(x => gs.includes(x.g.id));
    return item(`data-kind="${k.id}"`, on, mixed, k.icon, t(`kind.${k.id}`), k.n, ' mkind')
      + kids.map(x => item(`data-g="${x.g.id}" data-k="${k.id}"`, on || gs.includes(x.g.id), false, x.g.icon, x.g.name, x.n, ' mkid')).join('');
  }).join('');
  return dropdown('groups', label, names.length, `<p class="mhint">${t('photos.kinds_hint')}</p>${body}`
    + (names.length ? `<button class="mclear" data-clear>${t('photos.clear')}</button>` : ''));
}
function sourceMenu() {
  const srcs = STATE.status?.sources || [];
  if (srcs.length < 2 || P.embedded) return '';
  const cur = P.scope.source || '';
  const item = (id, name) => `<label class="mi"><input type="radio" name="psource" data-source="${esc(id)}"${cur === id ? ' checked' : ''}><span class="mname">${esc(name)}</span></label>`;
  return dropdown('source', cur ? esc(scopeLabel('source', cur)) : t('photos.source'), cur,
    item('', t('photos.all_sources')) + srcs.map(x => item(x.id, x.label)).join(''));
}
/* Groups, sorting, retry of the failed analyses. */
function renderTools() {
  const sorts = ['date', 'confident', 'uncertain'];
  const sources = sourceMenu(), groups = groupsMenu();
  // On the left what filters, on the right what changes the display.
  $('#ptools').innerHTML = `<div class="pgroups">${groups}${sources}${scopeChips(sources ? ['source'] : [])}</div><div class="pview">`
    + (['pending', 'audit'].includes(P.filter) ? `<select id="psort" title="${esc(t('photos.sort'))}">${sorts.map(k => `<option value="${k}"${P.sort === k ? ' selected' : ''}>${t(`sort.${k}`)}</option>`).join('')}</select>` : '')
    + (P.filter === 'failed' && P.counts.failed ? `<button class="btn" id="b-requeue">${t('photos.requeue')}</button>` : '')
    + (!P.embedded && sortOf() === 'date' && P.months.length > 1 ? `<select id="pgoto" title="${esc(t('photos.goto'))}"><option value="">${t('photos.goto')}</option>${P.months.map(x =>
        `<option value="${x.m ?? ''}">${esc(monthLabel(x.m))} (${fmtN(x.n)})</option>`).join('')}</select>` : '')
    + (sortOf() === 'date' ? `<button class="chip${P.stack ? ' on' : ''}" id="b-stack" title="${esc(t('photos.stack_sub'))}">${t('photos.stack')}</button>` : '') + '</div>';
  $$('#ptools [data-menu]').forEach(b => b.onclick = () => {
    const open = P.menu !== b.dataset.menu; closeMenus();
    if (open) { P.menu = b.dataset.menu; b.setAttribute('aria-expanded', 'true'); $(`#ptools .pmenu[data-for="${P.menu}"]`).hidden = false; }
  });
  $$('#ptools input[data-mixed]').forEach(x => { x.indeterminate = true; });
  $$('#ptools input[data-kind]').forEach(x => x.onchange = () => toggleKind(x.dataset.kind));
  $$('#ptools input[data-g]').forEach(x => x.onchange = () => toggleGroup(x.dataset.g, x.dataset.k));
  $('#ptools [data-clear]') && ($('#ptools [data-clear]').onclick = () => setChoice([], []));
  $$('#ptools input[data-source]').forEach(x => x.onchange = () => { P.menu = null; setQuery({ source: x.dataset.source || null, base: null }); reloadPage(); });
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
/* Photo selection (tick of a tile, long press with a finger; shortcuts: Ctrl/Shift + click, mouse drag) and
   bulk review. As long as something is selected, a click on a photo ticks it instead of opening it.
   "Select all" takes all photos of the filter, loaded or not (P.allSel), except those unticked (P.excl). */
const isSel = id => P.allSel ? !P.excl.has(id) : P.sel.has(id);
const selCount = () => P.allSel ? Math.max(0, (P.counts[P.filter] ?? 0) - P.excl.size) : P.sel.size;
function setSel(id, on) {
  if (P.allSel) on ? P.excl.delete(id) : P.excl.add(id); else on ? P.sel.add(id) : P.sel.delete(id);
}
function clearSel() { P.sel.clear(); P.excl.clear(); P.allSel = false; }
function refreshSel() {
  $$('#pgrid .tile').forEach(t => t.classList.toggle('sel', isSel(P.assets[+t.dataset.i]?.asset_id)));
  // Last photo unticked: end of the selection.
  if (P.selecting && !selCount() && !P.allSel) { setSelecting(false); return; }
  renderSelbar();
}
function setSelecting(on) {
  P.selecting = on; if (!on) clearSel();
  $('#pgrid').classList.toggle('selecting', on);
  if (on) renderSelbar(); else { $$('#pgrid .tile.sel').forEach(x => x.classList.remove('sel')); renderSelbar(); }
}
function toggleSel(i, range) {
  // Range in the displayed order (the grid groups by month, not necessarily in the order of the list).
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
  // Same labels as in the viewer. On a phone, Species and Reject move into the menu (.onlyphone).
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
/* Invert: all photos of the filter except the chosen ones, or the other way round (without loading everything). */
function invertSel() {
  if (P.allSel) { P.sel = new Set(P.excl); P.excl.clear(); P.allSel = false; }
  else { P.excl = new Set(P.sel); P.sel.clear(); P.allSel = true; }
  refreshSel();
}
/* Choice of a species (one photo or a selection): search in GBIF, common or Latin name. */
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
/* CSV export of the selection ("all": the identifiers are asked from the server, page by page). */
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
  const sel = P.allSel ? { filter: P.filter, every: true, group: groupParam(), scope: P.scope, exclude: [...P.excl] } : { filter: P.filter, group: groupParam(), asset_ids: [...P.sel] };
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
    if (k === 'reanalyze') {   // same behaviour as for a single photo: the analysis starts without waiting
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
// After a bulk action: a record reloads entirely (figures, main photo), Photos keeps its list.
const afterBulk = () => P.embedded ? (reloadPage(), Promise.resolve()) : loadPhotos(P.base);
/* Month heading, in selection mode: all its displayed photos (click again: none). */
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
  // Not analysed: left out by a setting (reason, the setting in the tooltip), given up after failures, or queued.
  if (!a.analyzed_at && !STATE.status?.manual) out.push(a.skipped
    ? `<span title="${esc(t('tile.skipped_hint'))}">${t(`tile.skipped_${a.skipped}`)}</span>`
    : a.fail_count >= (STATE.status?.max_failures ?? 3)
      ? `<span class="rej" title="${esc(a.last_error || '')}">${t('tile.failed')}</span>` : `<span>${t('tile.queued')}</span>`);
  return out.join('');
}

/* Photo tile: selection, series stack; number of subjects on the Photos page, date and place in a record. */
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
// Heading of a photo: month like the Immich timeline, or year if photos are too sparse; sorted by
// confidence, a single block in the order of the list (that of the viewer arrows).
function sectionOf(a) {
  if (sortOf() !== 'date') return t(`sort.${sortOf()}`);
  const d = a.taken_at ? new Date(a.taken_at) : null;
  return d ? (P.byYear ? String(d.getFullYear()) : monthKey(d)) : t('photos.no_date');
}
const sectionHead = (k, n) => `<div class="month">${esc(k[0].toLocaleUpperCase(LANG) + k.slice(1))}<span>${t('count.photos', { n })}</span>
  <button class="msel" title="${esc(t('bulk.section_hint'))}">${t('bulk.section')}</button></div>`;
// Month of a photo, as the server counts it for the timeline ("YYYY-MM", null without a date).
const keyOf = a => a.taken_at ? a.taken_at.slice(0, 7) : null;
const monthLabel = m => { if (!m) return t('photos.no_date');
  const s = new Date(+m.slice(0, 4), +m.slice(5, 7) - 1, 15).toLocaleDateString(LANG, { month: 'long', year: 'numeric' });
  return s[0].toLocaleUpperCase(LANG) + s.slice(1); };
/* Add photos P.assets[P.shown..] to the grid without touching the tiles already shown: with thousands of
   photos loaded, rebuilding everything at every page made scrolling slower and slower. */
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
    // Stack: the next photos of the same series are added to the head tile instead of having their own.
    const html = idx.map(i => {
      const sid = P.assets[i].series_id, lead = P.lastLead;
      if (stacking() && sid && lead != null && P.assets[lead].series_id === sid) { P.members.get(lead).push(i); grown.add(lead); return ''; }
      P.lastLead = i; P.members.set(i, [i]);
      return tileHTML(P.assets[i], i);
    }).join('');
    if (k === P.lastSection) {   // continuation of the last month shown
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
/* Next page; on failure, a message and a new attempt instead of an endless "Loading…". */
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
  P.byYear = P.assets.length / Math.max(1, nbMonths) < 3;   // decided on the first page, kept afterwards
  // After a jump in the timeline, the more recent photos are not loaded: a button to go back to them.
  $('#pgrid').innerHTML = P.base ? `<div class="pnewer"><button class="btn" id="pnewer">↑ ${t('photos.newer')}</button></div>` : '';
  appendGrid();
  if (P.base) {   // loaded on their own when scrolling back to the top (area under the top bar excluded), button as a fallback
    let busy = false;
    const go = () => { if (busy) return; busy = true; io.disconnect(); loadNewer(); };
    const io = new IntersectionObserver(es => { if (es[0].isIntersecting && P.armed) go(); },
      { rootMargin: `-${Math.round(($('.topbar')?.getBoundingClientRect().bottom ?? 64) + 8)}px 0px 0px 0px` });
    P.armed = false; setTimeout(() => { P.armed = true; }, 800);   // not during the jump itself
    io.observe($('#pnewer')); $('#pnewer').onclick = go;
  }
}
/* Date scroll timeline, like Immich's: the whole height of the screen, each month in proportion to
   its photos (with a minimum, so that small months stay reachable). Hover: month under the pointer;
   click or drag: the list goes to that position, to the photo. Keyboard: arrows (month), Page (year). */
const TL_MIN = 0.25;   // minimum room for a month: a quarter of the average room
function tlLayout() {
  const total = P.months.reduce((s, x) => s + x.n, 0), floor = TL_MIN * total / P.months.length;
  const w = P.months.map(x => Math.max(x.n, floor)), W = w.reduce((s, x) => s + x, 0);
  let off = 0, y = 0;
  P.tl = P.months.map((x, i) => { const r = { ...x, off, y0: y / W, h: w[i] / W }; off += x.n; y += w[i]; return r; });
}
const yearOf = i => P.tl[i]?.m?.slice(0, 4) ?? '?';
const tlAt = f => P.tl.find(r => f < r.y0 + r.h) || P.tl[P.tl.length - 1];   // month at a height (0 to 1)
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
    // Month chosen with the keyboard kept: at the bottom of the list, the page no longer scrolls to it and the photo at the top
    // of the screen would still be that of a more recent month (Page down would stay in place).
    const cur = P.tlKey != null ? P.tlKey : P.tl.findIndex(r => r.m === nav.dataset.m), step = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -1, ArrowRight: 1 }[e.key];
    let i = null;
    if (step) i = cur + step;
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = P.tl.length - 1;
    else if (e.key === 'PageDown') {   // first month of the previous (older) year
      i = cur + 1; while (i < P.tl.length && yearOf(i) === yearOf(cur)) i++;
    } else if (e.key === 'PageUp') {   // first month of the next (more recent) year
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
/* Marks: years (if there is room) and one dot per month. Redrawn when the height changes. */
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
/* Go to the n-th photo of the list (in date order): scrolls if it is loaded, otherwise the list starts again from the
   start of its month (at most 50 photos before it): month heading at the top, and at the top of the timeline it
   does start again from the very first photo. */
async function goToOffset(off) {
  if (off < P.base || off >= P.base + P.assets.length) {
    const m = P.tl ? P.tl.find(r => off < r.off + r.n) : null, base = Math.max(m ? m.off : off, off - 50);
    setQuery({ base: base || null });
    await loadPhotos(base);
    if (off === base) { const first = $('#pgrid .month'); first ? first.scrollIntoView({ block: 'start' }) : window.scrollTo(0, 0); return; }
  }
  const i = off - P.base, lead = [...P.members].find(([, mem]) => mem.includes(i))?.[0] ?? i;
  let el = $(`#pgrid .tile[data-i="${lead}"]`); if (!el) return;
  const block = el.parentElement;   // first photo of the block: the heading is shown too
  if (block.firstElementChild === el && block.previousElementSibling?.classList.contains('month')) el = block.previousElementSibling;
  el.scrollIntoView({ block: 'start' });
}
function goToMonth(m) { const r = P.months.reduce((acc, x) => acc.done ? acc : x.m === m ? { off: acc.off, done: true } : { off: acc.off + x.n }, { off: 0 }); return goToOffset(r.off); }
/* After a jump, the more recent photos are not loaded: when scrolling back to the top, the previous page
   arrives, and the photo being looked at stays in place. */
async function loadNewer() {
  const old = P.base, base = Math.max(0, old - PAGE);
  setQuery({ base: base || null });
  await loadPhotos(base);
  while (P.assets.length <= old - base && P.more) if (!await loadMorePhotos()) break;
  const i = old - base, lead = [...P.members].find(([, mem]) => mem.includes(i))?.[0] ?? i;
  $(`#pgrid .tile[data-i="${lead}"]`)?.scrollIntoView({ block: 'start' });
}
/* Photo at the top of the screen: mark in the timeline. */
function markTimeline() {
  const nav = $('#ptimeline'), grid = $('#pgrid');
  if (nav.hidden || STATE.view !== 'photos' || !P.tl) return;
  const r = grid.getBoundingClientRect(), top = $('.topbar')?.getBoundingClientRect().bottom ?? 64;
  const tl = document.elementFromPoint(r.left + 30, Math.max(top, r.top) + 70)?.closest('#pgrid .tile');
  if (P.tlKey != null && Date.now() - P.tlKeyAt < 400) return;   // scrolling caused by the keyboard
  P.tlKey = null;   // scrolling by the user: the timeline follows the page again
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
/* Keyboard in the grid: arrows from one photo to the next (up and down: the closest photo of the neighbouring
   row). Enter opens, Space selects in selection mode (core.js). */
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
/* Back from the viewer: only the tiles of the reviewed photos are redrawn, the grid and its position stay. */
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
/* Mouse drag over the photos in selection mode: each hovered photo takes the state of the first one. */
let dragSel = null;
let longPress = null, longFired = false;
/* Long press with a finger: first photo selected. */
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
  e.preventDefault();   // no text selection or image drag
});
// pointermove + elementFromPoint: during the press, Firefox sends the events to the starting tile.
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
  if (dragSel?.moved) return;   // end of a drag: already selected
  if (longFired) { longFired = false; return; }   // end of a long press
  const t = e.target.closest('.tile'); if (!t) return;
  const star = e.target.closest('.star');
  if (star) { P.onStar?.(P.assets[+t.dataset.i].asset_id); return; }
  if (P.selecting || e.target.closest('.tick') || e.ctrlKey || e.metaKey || e.shiftKey) { e.preventDefault(); toggleSel(+t.dataset.i, e.shiftKey); return; }
  openViewer(P.assets, +t.dataset.i);
};
// "More" menus: closed by a click elsewhere or on one of their choices.
document.addEventListener('click', e => { $$('.selmore[open]').forEach(m => { if (!m.contains(e.target) || e.target.closest('.menu button, .menu a')) m.open = false; }); });
