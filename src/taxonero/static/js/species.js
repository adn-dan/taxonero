/* ============================== page Espèces ============================== */
/* Groupes du vivant. Les huit premiers ont chacun leur couleur, dans l'ordre de la palette catégorielle
   validée (validate_palette.js : clair et sombre, couples voisins dans les barres empilées). Au-delà de huit
   couleurs, on ne les distingue plus : les groupes plus rares gardent leur nom et leur icône, avec une couleur
   neutre commune. Pas de groupe « Autres » : un taxon hors de cette liste prend le nom de sa classe, ou de son
   embranchement. */
const inL = (key, names) => L => names.includes(L[key]);
const GROUPS = [   // noms : group.<id> dans les catalogues de langue
  { id: 'aves', icon: '🐦', test: inL('class', ['Aves']) },
  { id: 'mammalia', icon: '🦊', test: inL('class', ['Mammalia']) },
  { id: 'insecta', icon: '🦋', test: inL('class', ['Insecta']) },
  { id: 'reptilia', icon: '🦎', test: inL('class', ['Reptilia', 'Squamata', 'Testudines', 'Crocodylia']) },
  { id: 'amphibia', icon: '🐸', test: inL('class', ['Amphibia']) },
  { id: 'plantae', icon: '🌿', test: inL('kingdom', ['Plantae']) },
  // GBIF ne donne pas de classe aux poissons osseux (Chordata, puis directement l'ordre) ; tous les autres
  // vertébrés en ont une. Un chordé sans classe est donc un poisson.
  { id: 'pisces', icon: '🐟', test: L => ['Actinopterygii', 'Chondrichthyes', 'Elasmobranchii', 'Holocephali',
      'Sarcopterygii', 'Dipneusti', 'Actinistia', 'Myxini', 'Petromyzonti', 'Cephalaspidomorphi'].includes(L.class)
      || (L.phylum === 'Chordata' && (!L.class || L.class === 'Chordata')) },
  { id: 'fungi', icon: '🍄', test: inL('kingdom', ['Fungi']) },
  // Couleur neutre commune (classes listées : le filtre de la page Photos ne connaît que la classe).
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
  Object.defineProperty(g, 'name', { get: () => t(`group.${g.id}`) });   // dans la langue courante
});
/* Groupe d'un taxon. Hors liste : un groupe créé à la volée, nommé d'après la classe (ou l'embranchement, ou le
   règne), ajouté à GROUPS pour apparaître dans les filtres et légendes. */
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
/* Classe tous les taxons d'une liste (crée les groupes à la volée) puis renvoie GROUPS : à appeler avant
   d'itérer sur GROUPS pour un rendu. */
const groupsFor = ts => { ts.forEach(t => groupOf(t)); return GROUPS; };
/* Légende : une pastille par couleur. Les groupes à couleur neutre partagent la leur, avec leurs noms. */
function groupLegend(groups) {
  const colored = groups.filter(g => g.cv !== '--g-other'), neutral = groups.filter(g => g.cv === '--g-other');
  return colored.map(g => `<span><i class="sw" style="background:${g.c}"></i>${esc(g.name)}</span>`).join('')
    + (neutral.length ? `<span><i class="sw" style="background:var(--g-other)"></i>${neutral.map(g => esc(g.name)).join(' · ')}</span>` : '');
}
/* Pastilles de groupes : les groupes créés à la volée (classes peu représentées, au nom latin), s'ils sont plusieurs,
   sous une seule pastille « Autres » qui les déplie. Dépliée d'office si l'un d'eux est le filtre actif. */
const GX = { open: false };
function groupChips(groups, active, chip, count) {
  const known = groups.filter(g => !g.dynamic), dyn = groups.filter(g => g.dynamic);
  if (dyn.length < 2) return groups.map(chip).join('');
  const open = GX.open || dyn.some(g => g.id === active);
  return known.map(chip).join('') + `<button class="chip" data-gmore aria-expanded="${open}">🔹 ${t('group.other')}<span class="n">${dyn.reduce((a, g) => a + count(g), 0)}</span></button>`
    + `<span class="gxchips${open ? ' on' : ''}">${dyn.map(chip).join('')}</span>`;
}
document.addEventListener('click', e => {
  const b = e.target.closest?.('[data-gmore]'); if (!b) return;
  GX.open = b.getAttribute('aria-expanded') !== 'true';
  b.setAttribute('aria-expanded', GX.open);
  b.nextElementSibling.classList.toggle('on', GX.open);
});
const THREATENED = ['VULNERABLE', 'ENDANGERED', 'CRITICALLY_ENDANGERED'];
const IUCN_CATS = ['CRITICALLY_ENDANGERED', 'ENDANGERED', 'VULNERABLE', 'NEAR_THREATENED', 'LEAST_CONCERN', 'DATA_DEFICIENT', 'NOT_EVALUATED'];
const IUCN_COLOR = { CRITICALLY_ENDANGERED: '#c0392b', ENDANGERED: '#e67e22', VULNERABLE: '#d4ac0d', NEAR_THREATENED: '#7f8c8d',
  LEAST_CONCERN: '#27ae60', DATA_DEFICIENT: '#95a5a6', NOT_EVALUATED: '#bdc3c7' };
// Filtres Liste rouge : « Menacées » regroupe VU, EN et CR (définition UICN), puis chaque catégorie.
const IUCN_FILTERS = [{ id: 'threatened', test: c => THREATENED.includes(c) },
  ...IUCN_CATS.map(k => ({ id: k, test: c => (c || 'NOT_EVALUATED') === k, color: IUCN_COLOR[k] }))];
const iucnFilterName = id => id === 'threatened' ? t('iucn.threatened') : iucnName(id);
const iucnOK = (s, f) => !f || IUCN_FILTERS.find(x => x.id === f).test(s.taxon?.iucn);
const groupOK = (s, g) => !g || groupOf(s.taxon)?.id === g;
/* Rangée de pastilles groupe + Liste rouge. `st` porte { group, iucn } ; `all` sert aux comptages. */
function filterRows(st, all, prefix) {
  const gCount = g => all.filter(s => groupOf(s.taxon) === g && iucnOK(s, st.iucn)).length;
  const iCount = f => all.filter(s => f.test(s.taxon?.iucn) && groupOK(s, st.group)).length;
  groupsFor(all.map(s => s.taxon));
  return `<div class="frow"><span class="lab">${t('species.group')}</span><button class="chip${st.group ? '' : ' on'}" data-${prefix}g="">${t('species.all')}</button>${groupChips(GROUPS
      .filter(g => all.some(s => groupOf(s.taxon) === g)), st.group, g => { const n = gCount(g);
        return `<button class="chip${st.group === g.id ? ' on' : ''}" data-${prefix}g="${g.id}" ${n ? '' : 'disabled'}><i class="sw" style="background:${g.c}"></i>${g.icon} ${esc(g.name)}<span class="n">${n}</span></button>`; }, gCount)}</div>
    <div class="frow"><span class="lab">${t('iucn.red_list')}</span><button class="chip${st.iucn ? '' : ' on'}" data-${prefix}i="">${t('species.all_f')}</button>${IUCN_FILTERS
      .filter(f => all.some(s => f.test(s.taxon?.iucn))).map(f => { const n = iCount(f);
        return `<button class="chip${st.iucn === f.id ? ' on' : ''}" data-${prefix}i="${f.id}" ${n ? '' : 'disabled'}>${f.color ? `<span class="iucn-dot" style="background:${f.color}"></span>` : '⚠️'}${iucnFilterName(f.id)}<span class="n">${n}</span></button>`; }).join('')}</div>`;
}
function bindFilterRows(root, st, prefix, redraw) {
  root.querySelectorAll(`[data-${prefix}g]`).forEach(b => b.onclick = () => { st.group = b.dataset[`${prefix}g`] || null; redraw(); });
  root.querySelectorAll(`[data-${prefix}i]`).forEach(b => b.onclick = () => { st.iucn = b.dataset[`${prefix}i`] || null; redraw(); });
}
const SP = { all: [], group: null, iucn: null, q: '', sort: 'photos', higher: {}, month: null, country: null, camera: null, year: null, mode: 'grid' };
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
  if (!groupOK(s, SP.group) || !iucnOK(s, SP.iucn)) return false;
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
// Filtres de contexte (mois, pays, appareil) : ce qu'ils gardent des photos, à reporter sur la grille et les fiches.
const spContext = () => { const q = new URLSearchParams();
  if (SP.month) q.set('month', SP.month); if (SP.country) q.set('country', SP.country); if (SP.camera != null) q.set('camera', SP.camera);
  return q; };
function renderSpecies() {
  const root = $('#species');
  if (!SP.all.length) { root.innerHTML = `<div class="empty">${t('species.empty')}</div>`; return; }
  const shown = SP.all.filter(matches), ctx = spContext(), hasCtx = [...ctx.keys()].length > 0, photos = shown.reduce((a, s) => a + s.n, 0);
  // Avec un filtre de contexte, s.n compterait aussi les photos hors filtre : le nombre juste est sur la page Photos.
  ctx.set('filter', 'identified');
  if (SP.group) ctx.set('group', SP.group);
  const photoCount = hasCtx ? `<a href="#photos?${ctx}">${t('photos.see')}</a>` : t('count.photos', { n: photos });
  // Résumé + un seul jeu de filtres (groupe, Liste rouge).
  let h = `<div class="filters"><span class="count">${t('count.species', { n: shown.length })} · ${photoCount}</span>
    ${SP.mode === 'tree' ? `<button class="chip" data-treeinfo title="${esc(t('tree.info_title'))}">ⓘ ${t('tree.info_title')}</button><button class="chip" data-fold="1">${t('species.expand_all')}</button><button class="chip" data-fold="0">${t('species.collapse_all')}</button>` : ''}
    <span class="seg">${['grid', 'tree'].map(m => `<button class="${SP.mode === m ? 'on' : ''}" data-mode="${m}">${t(`species.view_${m}`)}</button>`).join('')}</span>
    <select id="spsort" class="pill" aria-label="${esc(t('species.sort'))}"><option value="photos">${t('species.sort_photos')}</option><option value="name">${t('species.sort_name')}</option></select></div>
    ${extraFilters()}
    <div class="card" style="padding:12px 16px 4px;margin-bottom:16px">${filterRows(SP, SP.all, 's')}</div>
    ${SP.mode === 'tree' ? treeInfoHTML() : ''}
    ${shown.length ? '' : `<div class="empty">${t('species.no_match')}</div>`}`;
  // Grille : par groupe. Arbre : toutes les branches, du règne à l'espèce (Animaux, Plantes, Champignons…).
  h += SP.mode === 'tree' ? renderKingdoms(shown)
    : groupsFor(shown.map(s => s.taxon)).map(g => renderGroup(g, shown.filter(s => groupOf(s.taxon) === g))).join('');
  root.innerHTML = h;
  $('#spsort').value = SP.sort;
  $('#spsort').onchange = e => { SP.sort = e.target.value; renderSpecies(); };
  bindFilterRows(root, SP, 's', () => { setQuery({ group: SP.group, iucn: SP.iucn }); renderSpecies(); });
  root.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { SP.mode = b.dataset.mode;
    try { localStorage.setItem('species-mode', SP.mode); } catch (err) {} renderSpecies(); });
  root.querySelector('[data-treeinfo]')?.addEventListener('click', e => {
    const box = $('#treeinfo'); box.hidden = !box.hidden; e.currentTarget.classList.toggle('on', !box.hidden); });
  root.querySelectorAll('[data-fold]').forEach(b => b.onclick = () => root.querySelectorAll('.tnode').forEach(d => d.open = b.dataset.fold === '1'));
  root.querySelectorAll('[data-unset]').forEach(b => b.onclick = () => { const k = b.dataset.unset; SP[k] = k === 'q' ? '' : null; setQuery({ [k]: null }); renderSpecies(); });
}
/* Filtres venus des statistiques (mois, pays, appareil, année) et de la recherche, chacun retirable. */
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
  const ctx = spContext().toString();   // la fiche garde le mois, le pays ou l'appareil d'où l'on vient
  location.hash = `species/${c.dataset.key}${ctx ? `?${ctx}` : ''}`;
};
// Recadrage « object-fit: cover » centré sur le rectangle du sujet plutôt que sur le centre de la photo.
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
/* Explication de l'arbre (bouton ⓘ) : rangs taxonomiques, source, niveaux absents ; lien vers l'article
   Wikipédia « Rang taxonomique » (Q427626) dans la langue de l'interface. */
function treeInfoHTML() {
  return `<section class="card treeinfo" id="treeinfo" hidden>${t('tree.info_body')}
    <a href="https://www.wikidata.org/wiki/Special:GoToLinkedPage/${LANG}wiki/Q427626" target="_blank" rel="noopener">${t('tree.info_link')} ↗</a></section>`;
}
/* Contenu d'une pastille de rang : miniature de l'espèce la plus photographiée, nom et rang, nom commun,
   nombre d'espèces, lien Wikipédia. */
function treePill(name, rank, list) {
  const top = [...list].sort((a, b) => b.n - a.n)[0];
  const img = `<img loading="lazy" src="/api/thumb/${top.cover}?size=thumbnail" style="${focus(top.cover_box)}" alt="">`;
  // Rang absent chez GBIF (poissons osseux sans classe…) : niveau gardé, pour que les rangs restent alignés.
  if (name === '—') return `${img}<span class="tn"><span><b class="unranked">${esc(t('tree.unranked', { rank: rankName(rank).toLocaleLowerCase(LANG) }))}</b></span>
    <span class="tmeta"><span class="cnt">${t('count.species_short', { n: list.length })}</span></span></span>`;
  const hi = SP.higher[name] || {}, { main, sci } = higherNames(name);
  return `${img}
    <span class="tn"><span><a class="tfam" href="#${rank}/${encodeURIComponent(name)}" title="${esc(t('fam.open'))}"><b>${esc(main)}</b></a><span class="rank">${esc(rankName(rank))}</span></span>${sci ? `<span class="com sci2">${esc(sci)}</span>` : ''}
    <span class="tmeta"><span class="cnt">${t('count.species_short', { n: list.length })}</span>${hi.has_wiki ? ` · <a class="tw" href="#" data-wiki="${hi.gbif_key}">${t('wiki.wikipedia')}</a>` : ''}</span></span>`;
}
/* Groupe commun à toutes les espèces de list (pour colorer une branche), sinon null. */
function uniformGroup(list) { const g = groupOf(list[0].taxon); return list.every(s => groupOf(s.taxon) === g) ? g : null; }
const groupVars = c => `--gc:${c};--gc-soft:color-mix(in srgb, ${c} 40%, var(--line))`;
function treeNode(name, rank, list, inner) {
  const g = uniformGroup(list);   // branche d'un seul groupe (Aves, Plantae…) : sa couleur ; sinon celle du parent
  return `<details class="tnode" open${g ? ` style="${groupVars(g.c)}"` : ''}><summary>${treePill(name, rank, list)}<span class="caret"></span></summary>
    <div class="tkids">${inner}</div></details>`;
}
/* En-tête d'un groupe : lien vers la page du taxon le plus large qui contient exactement ses espèces (Oiseaux ->
   classe Aves, Champignons -> règne Fungi) ; aucun lien si le groupe en couvre plusieurs (poissons : plusieurs classes). */
function groupTaxonLink(g, list) {
  for (const rank of ['kingdom', 'phylum', 'class']) {
    const name = list[0].taxon?.lineage?.[rank];
    if (!name || !list.every(s => s.taxon?.lineage?.[rank] === name)) continue;
    const inTaxon = SP.all.filter(s => s.taxon?.lineage?.[rank] === name);
    if (inTaxon.length === list.length) return `<a class="gtx" href="#${rank}/${encodeURIComponent(name)}" title="${esc(t('fam.open'))}">${esc(g.name)}</a>`;
  }
  return esc(g.name);
}
/* Arbre des espèces de list, un niveau par rang de ranks (ex. ['order', 'family']), les espèces en feuilles.
   Une seule construction, trois présentations : leaf (vue Arbre, petites fiches), big (pages des rangs, grandes
   vignettes) et h (pages des rangs sur écran large : chaque rang à droite de son parent, moins de défilement).
   Rang absent de la taxonomie GBIF : niveau « — » gardé, pour que les rangs restent alignés. */
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
/* Vue Arbre : une carte par règne, puis embranchement, classe, ordre, famille et espèces. */
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
