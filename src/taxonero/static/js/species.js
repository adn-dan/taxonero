/* ============================== Species page ============================== */
/* Groups of life. The first eight each have their colour, in the order of the validated categorical
   palette (validate_palette.js: light and dark, neighbouring pairs in stacked bars). Beyond eight
   colours, they can no longer be told apart: the rarer groups keep their name and icon, with a shared
   neutral colour. No "Others" group: a taxon outside this list takes the name of its class, or of its
   phylum. */
const inL = (key, names) => L => names.includes(L[key]);
const GROUPS = [   // names: group.<id> in the language catalogues
  { id: 'aves', icon: '🐦', test: inL('class', ['Aves']) },
  { id: 'mammalia', icon: '🦊', test: inL('class', ['Mammalia']) },
  { id: 'insecta', icon: '🦋', test: inL('class', ['Insecta']) },
  { id: 'reptilia', icon: '🦎', test: inL('class', ['Reptilia', 'Squamata', 'Testudines', 'Crocodylia']) },
  { id: 'amphibia', icon: '🐸', test: inL('class', ['Amphibia']) },
  { id: 'plantae', icon: '🌿', test: inL('kingdom', ['Plantae']) },
  // GBIF gives no class to bony fishes (Chordata, then directly the order); all the other
  // vertebrates have one. A chordate without a class is therefore a fish.
  { id: 'pisces', icon: '🐟', test: L => ['Actinopterygii', 'Chondrichthyes', 'Elasmobranchii', 'Holocephali',
      'Sarcopterygii', 'Dipneusti', 'Actinistia', 'Myxini', 'Petromyzonti', 'Cephalaspidomorphi'].includes(L.class)
      || (L.phylum === 'Chordata' && (!L.class || L.class === 'Chordata')) },
  { id: 'fungi', icon: '🍄', test: inL('kingdom', ['Fungi']) },
  // Shared neutral colour (classes listed: the filter of the Photos page only knows the class).
  { id: 'arachnida', icon: '🕷️', test: inL('class', ['Arachnida']) },
  { id: 'crustacea', icon: '🦀', test: L => L.subphylum === 'Crustacea' || ['Malacostraca', 'Branchiopoda',
      'Maxillopoda', 'Hexanauplia', 'Thecostraca', 'Copepoda', 'Ostracoda', 'Ichthyostraca', 'Remipedia', 'Cephalocarida'].includes(L.class) },
  { id: 'myriapoda', icon: '🐛', test: inL('class', ['Chilopoda', 'Diplopoda', 'Pauropoda', 'Symphyla']) },
  { id: 'mollusca', icon: '🐌', test: L => L.phylum === 'Mollusca'
      || ['Gastropoda', 'Bivalvia', 'Cephalopoda', 'Polyplacophora', 'Scaphopoda'].includes(L.class) },
  { id: 'worms', icon: '🪱', test: L => ['Annelida', 'Platyhelminthes', 'Nematoda', 'Nemertea'].includes(L.phylum)
      || ['Clitellata', 'Polychaeta', 'Turbellaria', 'Chromadorea'].includes(L.class) },
  { id: 'cnidaria', icon: '🪼', test: L => L.phylum === 'Cnidaria'
      || ['Anthozoa', 'Hydrozoa', 'Scyphozoa', 'Cubozoa'].includes(L.class) },
  { id: 'echinodermata', icon: '⭐', test: L => L.phylum === 'Echinodermata'
      || ['Asteroidea', 'Echinoidea', 'Holothuroidea', 'Ophiuroidea', 'Crinoidea'].includes(L.class) },
  { id: 'porifera', icon: '🧽', test: L => L.phylum === 'Porifera' || ['Demospongiae', 'Calcarea', 'Hexactinellida'].includes(L.class) },
  { id: 'protista', icon: '🦠', test: inL('kingdom', ['Chromista', 'Protozoa']) },
  { id: 'bacteria', icon: '🧫', test: inL('kingdom', ['Bacteria', 'Archaea']) },
];
GROUPS.forEach(g => {
  g.cv = `--g-${['aves', 'mammalia', 'insecta', 'reptilia', 'amphibia', 'plantae', 'pisces', 'fungi'].includes(g.id) ? g.id : 'other'}`;
  g.c = `var(${g.cv})`;
  Object.defineProperty(g, 'name', { get: () => t(`group.${g.id}`) });   // in the current language
});
/* Group of a taxon. Outside the list: a group created on the fly, named after the class (or the phylum, or the
   kingdom), added to GROUPS to appear in the filters and legends. */
function groupOf(t) {
  const L = t?.lineage || {};
  const known = GROUPS.find(g => !g.dynamic && g.test(L));
  if (known) return known;
  const name = L.class || L.phylum || L.kingdom || '?';
  let g = GROUPS.find(x => x.dynamic && x.name === name);
  if (!g) {
    g = { id: `x-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, icon: '🔹', dynamic: true, cv: '--g-other', c: 'var(--g-other)' };
    const rank = L.class ? 'class' : L.phylum ? 'phylum' : 'kingdom';
    g.test = X => !GROUPS.some(k => !k.dynamic && k.test(X)) && (X[rank] || '?') === name;
    GROUPS.push(g);
  }
  return g;
}
/* Classify all the taxa of a list (creates the groups on the fly) then return GROUPS: to call before
   iterating over GROUPS for a rendering. */
const groupsFor = ts => { ts.forEach(t => groupOf(t)); return GROUPS; };
/* Legend: one chip per colour. Groups with the neutral colour share theirs, with their names. */
function groupLegend(groups) {
  const colored = groups.filter(g => g.cv !== '--g-other'), neutral = groups.filter(g => g.cv === '--g-other');
  return colored.map(g => `<span><i class="sw" style="background:${g.c}"></i>${esc(g.name)}</span>`).join('')
    + (neutral.length ? `<span><i class="sw" style="background:var(--g-other)"></i>${neutral.map(g => esc(g.name)).join(' · ')}</span>` : '');
}
/* Kind of a taxon (animal, plant, fungus, microbe): same rule as the server (curation.DETECTION_GROUP). */
function kindOf(tx) {
  const L = tx?.lineage || {}, k = L.kingdom;
  if (['Bacteria', 'Archaea', 'Viruses'].includes(k) || (k === 'Chromista' && L.phylum !== 'Ochrophyta') || (k === 'Protozoa' && L.phylum !== 'Mycetozoa')) return 'microbe';
  return !k || k === 'Animalia' || k === 'Metazoa' ? 'animal' : k === 'Fungi' ? 'fungus' : 'plant';
}
/* Groups menu of the Species and Statistics pages: the same as on the Photos page (kinds, each with its groups,
   several at once, a chosen kind standing for all its groups), over a list of taxa instead of photos.
   st: { kinds: 'animal,plant', group: 'aves,insecta', gmenu: open or not }. */
const gmList = v => v ? String(v).split(',').filter(Boolean) : [];
const taxonChosen = (st, tx) => { const ks = gmList(st.kinds), gs = gmList(st.group);
  return (!ks.length && !gs.length) || ks.includes(kindOf(tx)) || gs.includes(groupOf(tx).id); };
// Kinds present, each with its groups, most taxa first. counted: the taxa that count in the figures (other filters).
function taxaTree(taxa, counted = taxa) {
  const by = new Map(), add = (tx, n) => { const k = kindOf(tx), g = groupOf(tx); if (!by.has(k)) by.set(k, new Map());
    const m = by.get(k); m.set(g.id, { g, n: (m.get(g.id)?.n || 0) + n }); };
  taxa.forEach(tx => add(tx, 0)); counted.forEach(tx => add(tx, 1));
  return KINDS.filter(k => by.has(k.id)).map(k => { const kids = [...by.get(k.id).values()].sort((a, b) => b.n - a.n);
    return { ...k, n: kids.reduce((a, x) => a + x.n, 0), kids }; });
}
function taxaMenuHTML(st, tree) {
  const ks = gmList(st.kinds), gs = gmList(st.group);
  if (!tree.length || (tree.length === 1 && tree[0].kids.length < 2 && !ks.length && !gs.length)) return '';
  const names = [...tree.filter(k => ks.includes(k.id)).map(k => t(`kind.${k.id}`)),
    ...tree.flatMap(k => ks.includes(k.id) ? [] : k.kids).filter(x => gs.includes(x.g.id)).map(x => x.g.name)];
  const label = !names.length ? t('photos.groups') : names.length > 2 ? `${esc(names[0])}, ${esc(names[1])} +${names.length - 2}` : esc(names.join(', '));
  const item = (attrs, on, mixed, icon, name, n, cls = '') => `<label class="mi${cls}"><input type="checkbox" ${attrs}${on ? ' checked' : ''}${mixed ? ' data-mixed' : ''}>
    <span class="mic">${icon}</span><span class="mname">${esc(name)}</span><span class="n">${fmtN(n)}</span></label>`;
  const body = tree.map(k => {
    const kids = k.kids.length > 1 || k.kids.some(x => gs.includes(x.g.id)) ? k.kids : [];
    const on = ks.includes(k.id), mixed = !on && kids.some(x => gs.includes(x.g.id));
    return item(`data-kind="${k.id}"`, on, mixed, k.icon, t(`kind.${k.id}`), k.n, ' mkind')
      + kids.map(x => item(`data-g="${esc(x.g.id)}" data-k="${k.id}"`, on || gs.includes(x.g.id), false, x.g.icon, x.g.name, x.n, ' mkid')).join('');
  }).join('');
  return `<div class="pdrop gdrop"><button class="chip ddbtn${names.length ? ' on' : ''}" data-gmenu aria-haspopup="true" aria-expanded="${!!st.gmenu}">${label}<svg class="ic dd" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5z"/></svg></button>
    <div class="pmenu"${st.gmenu ? '' : ' hidden'}>${body}${names.length ? `<button class="mclear" data-clear>${t('photos.clear')}</button>` : ''}</div></div>`;
}
let GM_OPEN = null;   // { st, root } of the open menu, to close it on a click elsewhere or Escape
function closeTaxaMenu() {
  if (!GM_OPEN) return;
  const { st, root } = GM_OPEN; GM_OPEN = null; st.gmenu = false;
  const m = root.querySelector('.gdrop .pmenu'), b = root.querySelector('[data-gmenu]');
  if (m) m.hidden = true; b?.setAttribute('aria-expanded', 'false');
  return b;
}
document.addEventListener('click', e => { if (GM_OPEN && !e.target.closest?.('.gdrop')) closeTaxaMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && GM_OPEN) { closeTaxaMenu()?.focus(); e.stopPropagation(); } }, true);
/* changed(): the choice was changed (st.kinds, st.group updated): redraw; the menu stays open while choosing. */
function bindTaxaMenu(root, st, tree, changed) {
  const btn = root.querySelector('[data-gmenu]'); if (!btn) return;
  if (st.gmenu) GM_OPEN = { st, root };
  btn.onclick = () => { const open = !st.gmenu; closeTaxaMenu();
    if (open) { st.gmenu = true; GM_OPEN = { st, root }; btn.setAttribute('aria-expanded', 'true'); root.querySelector('.gdrop .pmenu').hidden = false; } };
  const set = (kinds, groups) => { st.kinds = KINDS.map(k => k.id).filter(k => kinds.includes(k)).join(','); st.group = [...new Set(groups)].join(','); changed(); };
  const kidsOf = id => (tree.find(k => k.id === id)?.kids || []).map(x => x.g.id);
  root.querySelectorAll('.gdrop input[data-mixed]').forEach(x => { x.indeterminate = true; });
  root.querySelectorAll('.gdrop input[data-kind]').forEach(x => x.onchange = () => { const id = x.dataset.kind, ks = gmList(st.kinds), kids = kidsOf(id);
    set(ks.includes(id) ? ks.filter(k => k !== id) : [...ks, id], gmList(st.group).filter(g => !kids.includes(g))); });   // half chosen: the whole kind
  root.querySelectorAll('.gdrop input[data-g]').forEach(x => x.onchange = () => { const id = x.dataset.g, kindId = x.dataset.k, kids = kidsOf(kindId);
    let ks = gmList(st.kinds), gs = gmList(st.group);
    if (ks.includes(kindId)) { ks = ks.filter(k => k !== kindId); gs = [...gs, ...kids.filter(g => g !== id)]; }   // all but this one
    else gs = gs.includes(id) ? gs.filter(g => g !== id) : [...gs, id];
    if (kids.length > 1 && kids.every(g => gs.includes(g))) { ks = [...ks, kindId]; gs = gs.filter(g => !kids.includes(g)); }   // all of them: the kind
    set(ks, gs); });
  const clear = root.querySelector('.gdrop [data-clear]'); if (clear) clear.onclick = () => set([], []);
}
const THREATENED = ['VULNERABLE', 'ENDANGERED', 'CRITICALLY_ENDANGERED'];
const IUCN_CATS = ['CRITICALLY_ENDANGERED', 'ENDANGERED', 'VULNERABLE', 'NEAR_THREATENED', 'LEAST_CONCERN', 'DATA_DEFICIENT', 'NOT_EVALUATED'];
const IUCN_COLOR = { CRITICALLY_ENDANGERED: '#c0392b', ENDANGERED: '#e67e22', VULNERABLE: '#d4ac0d', NEAR_THREATENED: '#7f8c8d',
  LEAST_CONCERN: '#27ae60', DATA_DEFICIENT: '#95a5a6', NOT_EVALUATED: '#bdc3c7' };
// Red List filters: "Threatened" groups VU, EN and CR (IUCN definition), then each category.
const IUCN_FILTERS = [{ id: 'threatened', test: c => THREATENED.includes(c) },
  ...IUCN_CATS.map(k => ({ id: k, test: c => (c || 'NOT_EVALUATED') === k, color: IUCN_COLOR[k] }))];
const iucnFilterName = id => id === 'threatened' ? t('iucn.threatened') : iucnName(id);
const iucnOK = (s, f) => !f || IUCN_FILTERS.find(x => x.id === f).test(s.taxon?.iucn);
/* Groups menu + row of Red List chips. `st` holds { kinds, group, iucn }; `all` is used for the counts. */
const filterTree = (st, all) => taxaTree(all.map(s => s.taxon), all.filter(s => iucnOK(s, st.iucn)).map(s => s.taxon));
function filterRows(st, all, prefix) {
  const iCount = f => all.filter(s => f.test(s.taxon?.iucn) && taxonChosen(st, s.taxon)).length;
  const menu = taxaMenuHTML(st, filterTree(st, all));
  return `${menu ? `<div class="frow gtools"><span class="lab">${t('species.group')}</span>${menu}</div>` : ''}
    <div class="frow"><span class="lab">${t('iucn.red_list')}</span><button class="chip${st.iucn ? '' : ' on'}" data-${prefix}i="">${t('species.all_f')}</button>${IUCN_FILTERS
      .filter(f => all.some(s => f.test(s.taxon?.iucn))).map(f => { const n = iCount(f);
        return `<button class="chip${st.iucn === f.id ? ' on' : ''}" data-${prefix}i="${f.id}" ${n ? '' : 'disabled'}>${f.color ? `<span class="iucn-dot" style="background:${f.color}"></span>` : '⚠️'}${iucnFilterName(f.id)}<span class="n">${n}</span></button>`; }).join('')}</div>`;
}
function bindFilterRows(root, st, all, prefix, redraw) {
  bindTaxaMenu(root, st, filterTree(st, all), redraw);
  root.querySelectorAll(`[data-${prefix}i]`).forEach(b => b.onclick = () => { st.iucn = b.dataset[`${prefix}i`] || null; redraw(); });
}
const SP = { all: [], kinds: '', group: '', gmenu: false, iucn: null, q: '', sort: 'photos', higher: {}, month: null, country: null, camera: null, year: null, mode: 'grid' };
try { SP.mode = localStorage.getItem('species-mode') === 'tree' ? 'tree' : 'grid'; } catch (e) {}
const monthName = m => new Date(Date.UTC(2001, m - 1, 1)).toLocaleDateString(LANG, { month: 'long', timeZone: 'UTC' });

async function loadSpecies() {
  SP.all = await api('/api/species');
  const names = new Set();
  SP.all.forEach(s => ['kingdom', 'phylum', 'class', 'order', 'family', 'genus'].forEach(r => s.taxon?.lineage?.[r] && names.add(s.taxon.lineage[r])));
  try { SP.higher = await api('/api/higher-taxa', { method: 'POST', body: JSON.stringify({ names: [...names] }) }); } catch (e) {}
  renderSpecies();
}
function matches(s) {
  if (!taxonChosen(SP, s.taxon) || !iucnOK(s, SP.iucn)) return false;
  if (SP.month && !(s.months || []).includes(SP.month)) return false;
  if (SP.country && !(s.countries || []).includes(SP.country)) return false;
  if (SP.camera != null && !(s.cameras || []).includes(SP.camera)) return false;
  if (SP.year && !(s.first && new Date(s.first).getUTCFullYear() === SP.year)) return false;
  if (!SP.q) return true;
  const t = s.taxon || {}, q = SP.q.toLowerCase();
  const hay = [t.scientific_name, ...Object.values(t.vernacular || {}), ...Object.values(t.lineage || {}),
               ...Object.values(t.lineage || {}).flatMap(n => Object.values(SP.higher[n]?.vernacular || {}))];
  return hay.some(x => String(x || '').toLowerCase().includes(q));
}
// Context filters (month, country, camera): what they keep of the photos, to carry over to the grid and the records.
const spContext = () => { const q = new URLSearchParams();
  if (SP.month) q.set('month', SP.month); if (SP.country) q.set('country', SP.country); if (SP.camera != null) q.set('camera', SP.camera);
  return q; };
function renderSpecies() {
  const root = $('#species');
  if (!SP.all.length) { root.innerHTML = `<div class="empty">${t('species.empty')}</div>`; return; }
  const shown = SP.all.filter(matches), ctx = spContext(), hasCtx = [...ctx.keys()].length > 0, photos = shown.reduce((a, s) => a + s.n, 0);
  // With a context filter, s.n would also count the photos outside the filter: the right number is on the Photos page.
  ctx.set('filter', 'identified');
  if (SP.group) ctx.set('group', SP.group);
  if (SP.kinds) ctx.set('kinds', SP.kinds);
  const photoCount = hasCtx ? `<a href="#photos?${ctx}">${t('photos.see')}</a>` : t('count.photos', { n: photos });
  // Summary + a single set of filters (group, Red List).
  let h = `<div class="filters"><span class="count">${t('count.species', { n: shown.length })} · ${photoCount}</span>
    ${SP.mode === 'tree' ? `<button class="chip" data-treeinfo title="${esc(t('tree.info_title'))}">ⓘ ${t('tree.info_title')}</button><button class="chip" data-fold="1">${t('species.expand_all')}</button><button class="chip" data-fold="0">${t('species.collapse_all')}</button>` : ''}
    <span class="seg">${['grid', 'tree'].map(m => `<button class="${SP.mode === m ? 'on' : ''}" data-mode="${m}">${t(`species.view_${m}`)}</button>`).join('')}</span>
    <select id="spsort" class="pill" aria-label="${esc(t('species.sort'))}"><option value="photos">${t('species.sort_photos')}</option><option value="name">${t('species.sort_name')}</option></select></div>
    ${extraFilters()}
    <div class="card" style="padding:12px 16px 4px;margin-bottom:16px">${filterRows(SP, SP.all, 's')}</div>
    ${SP.mode === 'tree' ? treeInfoHTML() : ''}
    ${shown.length ? '' : `<div class="empty">${t('species.no_match')}</div>`}`;
  // Grid: by group. Tree: all the branches, from kingdom to species (Animals, Plants, Fungi…).
  h += SP.mode === 'tree' ? renderKingdoms(shown)
    : groupsFor(shown.map(s => s.taxon)).map(g => renderGroup(g, shown.filter(s => groupOf(s.taxon) === g))).join('');
  root.innerHTML = h;
  $('#spsort').value = SP.sort;
  $('#spsort').onchange = e => { SP.sort = e.target.value; renderSpecies(); };
  bindFilterRows(root, SP, SP.all, 's', () => { setQuery({ kinds: SP.kinds || null, group: SP.group || null, iucn: SP.iucn }); renderSpecies(); });
  root.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { SP.mode = b.dataset.mode;
    try { localStorage.setItem('species-mode', SP.mode); } catch (err) {} renderSpecies(); });
  root.querySelector('[data-treeinfo]')?.addEventListener('click', e => {
    const box = $('#treeinfo'); box.hidden = !box.hidden; e.currentTarget.classList.toggle('on', !box.hidden); });
  root.querySelectorAll('[data-fold]').forEach(b => b.onclick = () => root.querySelectorAll('.tnode').forEach(d => d.open = b.dataset.fold === '1'));
  root.querySelectorAll('[data-unset]').forEach(b => b.onclick = () => { const k = b.dataset.unset; SP[k] = k === 'q' ? '' : null; setQuery({ [k]: null }); renderSpecies(); });
}
/* Filters coming from the statistics (month, country, camera, year) and from the search, each removable. */
function extraFilters() {
  const chips = [
    SP.q && ['q', t('species.search', { q: esc(SP.q) })],
    SP.month && ['month', t('species.f_month', { month: monthName(SP.month) })],
    SP.country && ['country', t('species.f_country', { country: esc(SP.country) })],
    SP.camera != null && ['camera', t('species.f_camera', { camera: esc(SP.camera || t('stats.unknown_camera')) })],
    SP.year && ['year', t('species.f_year', { year: String(SP.year) })],
  ].filter(Boolean);
  return chips.length ? `<div class="frow" style="margin-bottom:12px">${chips.map(([k, l]) =>
    `<button class="chip on" data-unset="${k}" title="${esc(t('species.f_remove'))}">${l} ✕</button>`).join('')}</div>` : '';
}
$('#species').onclick = async e => {
  const wk = e.target.closest('[data-wiki]');
  if (wk) { e.preventDefault(); openWiki(+wk.dataset.wiki, 'wikipedia'); return; }
  const c = e.target.closest('.sptile, .tleaf'); if (!c) return;
  const ctx = spContext().toString();   // the record keeps the month, the country or the camera you came from
  location.hash = `species/${c.dataset.key}${ctx ? `?${ctx}` : ''}`;
};
// "object-fit: cover" crop centred on the subject box rather than on the centre of the photo.
const focus = box => box && box[2] * box[3] < 0.95 ? `object-position:${((box[0] + box[2] / 2) * 100).toFixed(1)}% ${((box[1] + box[3] / 2) * 100).toFixed(1)}%` : '';
const spOrder = arr => SP.sort === 'name' ? [...arr].sort((a, b) => shortName(a.taxon).localeCompare(shortName(b.taxon), LANG)) : [...arr].sort((a, b) => b.n - a.n);
function spTile(s) {
  const tx = s.taxon || {};
  return `<div class="sptile" data-key="${s.taxon_key}" title="${esc(tx.scientific_name)}" role="button" tabindex="0" aria-label="${esc(shortName(tx))}"><img loading="lazy" src="/api/thumb/${s.cover}?size=thumbnail" style="${focus(s.cover_box)}" alt="">
    <span class="cnt">${tr('count.photos', { n: s.n })}</span>${tx.iucn ? `<span class="iucn ${tx.iucn}">${esc(iucnName(tx.iucn).toLocaleLowerCase(LANG))}</span>` : ''}
    <div class="shade"><b>${esc(shortName(tx))}</b>${common(tx) ? `<i>${esc(tx.scientific_name)}</i>` : ''}</div></div>`;
}
function spLeaf(s) {
  const tx = s.taxon || {};
  return `<div class="tleaf" data-key="${s.taxon_key}" role="button" tabindex="0" aria-label="${esc(shortName(tx))}"><img loading="lazy" src="/api/thumb/${s.cover}?size=thumbnail" style="${focus(s.cover_box)}" alt="">
    <div><b>${esc(shortName(tx))}</b>${common(tx) ? `<i>${esc(tx.scientific_name)}</i>` : ''}<small>${tr('count.photos', { n: s.n })}${tx.iucn ? ` <span class="iucn ${tx.iucn}">${esc(iucnName(tx.iucn).toLocaleLowerCase(LANG))}</span>` : ''}</small></div></div>`;
}
/* Explanation of the tree (ⓘ button): taxonomic ranks, source, missing levels; link to the Wikipedia article
   "Taxonomic rank" (Q427626) in the interface language. */
function treeInfoHTML() {
  return `<section class="card treeinfo" id="treeinfo" hidden>${t('tree.info_body')}
    <a href="https://www.wikidata.org/wiki/Special:GoToLinkedPage/${LANG}wiki/Q427626" target="_blank" rel="noopener">${t('tree.info_link')} ↗</a></section>`;
}
/* Content of a rank chip: thumbnail of the most photographed species, name and rank, common name,
   number of species, Wikipedia link. */
function treePill(name, rank, list) {
  const top = [...list].sort((a, b) => b.n - a.n)[0];
  const img = `<img loading="lazy" src="/api/thumb/${top.cover}?size=thumbnail" style="${focus(top.cover_box)}" alt="">`;
  // Rank missing from GBIF (bony fishes without a class…): level kept, so that the ranks stay aligned.
  if (name === '—') return `${img}<span class="tn"><span><b class="unranked">${esc(t('tree.unranked', { rank: rankName(rank).toLocaleLowerCase(LANG) }))}</b></span>
    <span class="tmeta"><span class="cnt">${t('count.species_short', { n: list.length })}</span></span></span>`;
  const hi = SP.higher[name] || {}, { main, sci } = higherNames(name);
  return `${img}
    <span class="tn"><span><a class="tfam" href="#${rank}/${encodeURIComponent(name)}" title="${esc(t('fam.open'))}"><b>${esc(main)}</b></a><span class="rank">${esc(rankName(rank))}</span></span>${sci ? `<span class="com sci2">${esc(sci)}</span>` : ''}
    <span class="tmeta"><span class="cnt">${t('count.species_short', { n: list.length })}</span>${hi.has_wiki ? ` · <a class="tw" href="#" data-wiki="${hi.gbif_key}">${t('wiki.wikipedia')}</a>` : ''}</span></span>`;
}
/* Group shared by all the species of list (to colour a branch), otherwise null. */
function uniformGroup(list) { const g = groupOf(list[0].taxon); return list.every(s => groupOf(s.taxon) === g) ? g : null; }
const groupVars = c => `--gc:${c};--gc-soft:color-mix(in srgb, ${c} 40%, var(--line))`;
function treeNode(name, rank, list, inner) {
  const g = uniformGroup(list);   // branch of a single group (Aves, Plantae…): its colour; otherwise the parent's
  return `<details class="tnode" open${g ? ` style="${groupVars(g.c)}"` : ''}><summary>${treePill(name, rank, list)}<span class="caret"></span></summary>
    <div class="tkids">${inner}</div></details>`;
}
/* Header of a group: link to the page of the broadest taxon that contains exactly its species (Birds ->
   class Aves, Fungi -> kingdom Fungi); no link if the group covers several (fishes: several classes). */
function groupTaxonLink(g, list) {
  for (const rank of ['kingdom', 'phylum', 'class']) {
    const name = list[0].taxon?.lineage?.[rank];
    if (!name || !list.every(s => s.taxon?.lineage?.[rank] === name)) continue;
    const inTaxon = SP.all.filter(s => s.taxon?.lineage?.[rank] === name);
    if (inTaxon.length === list.length) return `<a class="gtx" href="#${rank}/${encodeURIComponent(name)}" title="${esc(t('fam.open'))}">${esc(g.name)}</a>`;
  }
  return esc(g.name);
}
/* Tree of the species of list, one level per rank of ranks (e.g. ['order', 'family']), the species as leaves.
   A single construction, three presentations: leaf (Tree view, small cards), big (rank pages, large
   thumbnails) and h (rank pages on a wide screen: each rank on the right of its parent, less scrolling).
   Rank missing from the GBIF taxonomy: "—" level kept, so that the ranks stay aligned. */
const TREE_STYLES = {
  leaf: { leaves: list => `<div class="tleaves">${spOrder(list).map(spLeaf).join('')}</div>`, node: treeNode },
  big: { leaves: list => `<div class="spgrid2">${spOrder(list).map(spTile).join('')}</div>`, node: treeNode },
  h: { leaves: list => `<div class="hleaves">${spOrder(list).map(spTile).join('')}</div>`,
       node: (name, rank, list, inner) => `<div class="hnode"><div class="hpill tnode-pill">${treePill(name, rank, list)}</div><div class="hkids">${inner}</div></div>` },
};
function treeLevels(list, ranks, style = 'leaf') {
  const st = TREE_STYLES[style];
  if (!ranks.length) return st.leaves(list);
  const by = new Map();
  list.forEach(s => { const k = s.taxon?.lineage?.[ranks[0]] || '—'; by.set(k, [...(by.get(k) || []), s]); });
  return [...by.entries()].sort((a, b) => SP.sort === 'name' ? a[0].localeCompare(b[0]) : b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([name, sub]) => st.node(name, ranks[0], sub, treeLevels(sub, ranks.slice(1), style))).join('');
}
/* Tree view: one card per kingdom, then phylum, class, order, family and species. */
const KINGDOM_ICON = { Animalia: '🐾', Plantae: '🌿', Fungi: '🍄', Chromista: '🦠', Protozoa: '🦠', Bacteria: '🧫', Archaea: '🧫' };
function renderKingdoms(list) {
  const by = new Map();
  list.forEach(s => { const k = s.taxon?.lineage?.kingdom || '—'; by.set(k, [...(by.get(k) || []), s]); });
  return [...by.entries()].sort((a, b) => b[1].length - a[1].length).map(([name, sub]) => {
    const g = uniformGroup(sub), photos = sub.reduce((a, s) => a + s.n, 0), { main, sci } = higherNames(name);
    const title = name === '—' ? '?' : `<a class="gtx" href="#kingdom/${encodeURIComponent(name)}" title="${esc(t('fam.open'))}">${esc(main)}</a>${sci ? ` <small class="gsci">${esc(sci)}</small>` : ''}`;
    return `<section class="card group" style="${groupVars(g ? g.c : 'var(--g-other)')}">
      <header><h2>${KINGDOM_ICON[name] || (g ? g.icon : '')} ${title}</h2><span class="n">${t('count.species', { n: sub.length })} · ${t('count.photos', { n: photos })}</span></header>
      <div class="body"><div class="tree">${treeLevels(sub, ['phylum', 'class', 'order', 'family'])}</div></div></section>`;
  }).join('');
}
function renderGroup(g, list) {
  if (!list.length) return '';
  const photos = list.reduce((a, s) => a + s.n, 0);
  const body = `<div class="spgrid2">${spOrder(list).map(spTile).join('')}</div>`;
  return `<section class="card group" style="--gc:${g.c};--gc-soft:color-mix(in srgb, ${g.c} 40%, var(--line))">
    <header><h2>${g.icon} ${groupTaxonLink(g, list)}</h2><span class="n">${t('count.species', { n: list.length })} · ${t('count.photos', { n: photos })}</span></header>
    <div class="body">${body}</div></section>`;
}
