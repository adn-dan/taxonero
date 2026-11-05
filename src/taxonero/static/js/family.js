/* ============================== page d'un rang supérieur ============================== */
/* Règne, embranchement, classe, ordre, famille ou genre (#family/Canidae, #order/Passeriformes…) : sa fiche,
   l'article et la carte, puis ses espèces photographiées. */
const HIGHER_RANKS = ['kingdom', 'phylum', 'class', 'order', 'family', 'genus'];
const SPF = { id: 'fa', key: null, name: null, wsrc: 'wikipedia', wlang: '', map: null };
const higherLink = (rank, name, html) => `<a href="#${rank}/${encodeURIComponent(name)}" title="${esc(rankName(rank))}">${html}</a>`;
async function loadFamilyPage(name, rank = 'family', scope = {}) {
  SPF.name = `${rank}/${name}`; SPF.key = null; SPF.wlang = LANG;
  const root = $('#fapage');
  root.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  let r;
  try { r = await api(`/api/higher/${rank}/${encodeURIComponent(name)}`); }
  catch (e) {
    if (SPF.name !== `${rank}/${name}`) return;
    root.innerHTML = `<div class="empty">${t('fam.none')}</div>`;
    setNavTitle(name); setNavParent('#species', t('nav.species'));
    return;
  }
  if (SPF.name !== `${rank}/${name}`) return;
  SPF.key = r.key;
  const tx = r.taxon || {}, L = tx.lineage || {}, list = r.species, g = groupOf(list[0].taxon);
  // Noms communs et liens des rangs affichés (fil, arbre) : ceux qui ne sont pas encore connus du navigateur.
  const names = [...new Set([name, ...Object.values(L), ...list.flatMap(x => Object.values(x.taxon?.lineage || {}))])]
    .filter(n => n && !(SP.higher || {})[n]);
  if (names.length) {
    try { SP.higher = { ...(SP.higher || {}), ...await api('/api/higher-taxa', { method: 'POST', body: JSON.stringify({ names }) }) }; } catch (e) {}
  }
  setNavTitle(higherNames(name).main);
  const parentRank = HIGHER_RANKS.slice(0, HIGHER_RANKS.indexOf(rank)).reverse().find(k => list[0].taxon?.lineage?.[k]);
  if (parentRank) setNavParent(`#${parentRank}/${encodeURIComponent(list[0].taxon.lineage[parentRank])}`, higherNames(list[0].taxon.lineage[parentRank]).main);
  else setNavParent('#species', t('nav.species'));
  const photos = list.reduce((a, s) => a + s.n, 0), com = higherCommon(name) || common(tx);
  const firsts = list.map(s => s.first).filter(Boolean).sort(), lasts = list.map(s => s.last).filter(Boolean).sort();
  const places = [...new Set(r.points.map(p => p.city).filter(Boolean))], countries = [...new Set(r.points.map(p => p.country).filter(Boolean))];
  const above = HIGHER_RANKS.slice(0, HIGHER_RANKS.indexOf(rank)).map(k => list[0].taxon?.lineage?.[k] && k).filter(Boolean);
  const crumbs = above.map(k => { const n = list[0].taxon.lineage[k]; return higherLink(k, n, crumbLabel(n)); })
    .join('<span class="sep">›</span>');
  const grid = `#photos?filter=all&rank=${rank}&name=${encodeURIComponent(name)}`;
  const kpi = (big, small) => `<div><a class="kpilink" href="${grid}"><b>${big}</b></a><span>${small}</span></div>`;
  const countryLinks = countries.map(c => `<a href="${grid}&country=${encodeURIComponent(c)}">${esc(c)}</a>`).join(', ');
  root.innerHTML = `<section class="sphero card" style="--gc:${g.c}"><div class="sptext">
      <a class="gchip" href="#species?group=${g.id}"><i class="sw" style="background:${g.c}"></i>${g.icon} ${esc(g.name)}</a>
      <h1>${esc(com && com.toLowerCase() !== name.toLowerCase() ? com : name)}</h1>
      <div class="sci"><i>${esc(name)}</i> <span class="auth">${esc(rankName(rank))}${tx.authority ? ` · ${esc(tx.authority)}` : ''}</span></div>
      <div class="crumbs">${crumbs}</div>
      <div class="badges">${tx.wikidata ? `<a class="badge2" href="https://www.wikidata.org/wiki/${esc(tx.wikidata)}" target="_blank" rel="noopener">Wikidata ↗</a>` : ''}
        ${r.key ? `<a class="badge2" href="https://www.gbif.org/species/${r.key}" target="_blank" rel="noopener">GBIF ↗</a>` : ''}</div>
      <div class="spkpis">
        <div><b>${list.length}</b><span>${t('nav.species')}</span></div>
        ${kpi(photos, t('sp.photos', { n: photos }))}
        <div><a class="kpilink" href="${grid}"><b>${places.length || '—'}</b></a><span>${t('sp.places', { n: places.length })}${countries.length ? ` · ${countryLinks}` : ''}</span></div>
        ${firsts.length ? `<div><b>${fmtDate(firsts[0])}</b><span>${t('sp.first')}</span></div>` : ''}
        ${lasts.length > 0 && lasts[lasts.length - 1] !== firsts[0] ? `<div><b>${fmtDate(lasts[lasts.length - 1])}</b><span>${t('sp.last')}</span></div>` : ''}
      </div></div></section>
    ${r.key ? midHTML(SPF.id, t('sp.map_hint')) : ''}
    <div class="fahead"><h2 class="sph">${t('nav.species')} <span>${t('count.species', { n: list.length })} · ${t('count.photos', { n: photos })}</span></h2>
      <button class="chip" data-treeinfo>ⓘ ${t('tree.info_title')}</button>
      <span class="seg">${['grid', 'tree'].map(m => `<button class="${SP.mode === m ? 'on' : ''}" data-mode="${m}">${t(`species.view_${m}`)}</button>`).join('')}</span></div>
    ${treeInfoHTML()}
    <section class="card group" style="--gc:${g.c};--gc-soft:color-mix(in srgb, ${g.c} 40%, var(--line))"><div class="body" id="falist"></div></section>
    <h2 class="sph">${t('nav.photos')}</h2><div class="faphotos"></div>`;
  // Espèces : en arbre (le taxon de la page en racine, puis les rangs en dessous) ou en grille, comme la page Espèces.
  const below = ['phylum', 'class', 'order', 'family'].filter(k => HIGHER_RANKS.indexOf(k) > HIGHER_RANKS.indexOf(rank));
  const drawList = () => {
    root.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === SP.mode));
    // Arbre : horizontal sur écran large (moins de défilement), vertical en grand sinon.
    root.querySelector('#falist').innerHTML = SP.mode !== 'tree' ? `<div class="spgrid2">${spOrder(list).map(spTile).join('')}</div>`
      : matchMedia('(min-width: 1000px)').matches ? `<div class="htree">${treeLevels(list, below, 'h')}</div>`   // racine : l'en-tête de la page
      : `<div class="tree big">${treeNode(name, rank, list, treeLevels(list, below, 'big'))}</div>`;
  };
  drawList();
  root.querySelector('[data-treeinfo]').onclick = e => { const box = root.querySelector('#treeinfo'); box.hidden = !box.hidden;
    e.currentTarget.classList.toggle('on', !box.hidden); };
  root.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { SP.mode = b.dataset.mode;
    try { localStorage.setItem('species-mode', SP.mode); } catch (err) {} drawList(); });
  root.querySelector('#falist').onclick = e => {
    const wk = e.target.closest('[data-wiki]');
    if (wk) { e.preventDefault(); openWiki(+wk.dataset.wiki, 'wikipedia'); return; }
    const c = e.target.closest('.sptile, .tleaf'); if (c) location.hash = `species/${c.dataset.key}`;
  };
  if (r.key) {
    bindMapWiki(SPF, root);
    bindMid(SPF, root);
    drawRangeMap(SPF, r.points);
    loadPanelWiki(SPF);
  }
  await embedGrid(root.querySelector('.faphotos'), { rank, name, ...scope });
}
