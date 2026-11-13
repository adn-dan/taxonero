/* ============================== species page ============================== */
const SPP = { id: 'sp', key: null, assets: [], wsrc: 'wikipedia', wlang: '', map: null };
/* Page of a species: the species and its figures, the article and the map, then the shared photo grid. scope:
   month, country or camera you came from, kept on the grid (removable). */
async function loadSpeciesPage(key, scope = {}) {
  const root = $('#sppage'), P_ = SPP;
  P_.key = key; P_.wlang = LANG;   // interface language by default
  const notFound = () => { root.innerHTML = `<div class="empty">${tr('species.no_match')}</div>`; setNavTitle(tr('nav.species')); setNavParent('#species', tr('nav.species')); };
  if (!Number.isInteger(key)) { notFound(); return; }
  root.innerHTML = `<div class="empty">${tr('common.loading')}</div>`;
  const r = await api(`/api/species/${key}`);
  if (P_.key !== key || !root.isConnected) return;
  if (!r.taxon && !r.assets.length) { notFound(); return; }
  const t = r.taxon || {}, L = t.lineage || {}, g = groupOf(t);
  P_.assets = r.assets;
  const missing = Object.values(L).filter(n => n && !(SP.higher || {})[n]);   // common names of the classification trail
  if (missing.length) {
    try { SP.higher = { ...(SP.higher || {}), ...await api('/api/higher-taxa', { method: 'POST', body: JSON.stringify({ names: missing }) }) }; } catch (e) {}
  }
  setNavTitle(shortName(t));
  const parentRank = ['genus', 'family', 'order', 'class', 'phylum', 'kingdom'].find(k => L[k] && L[k] !== t.scientific_name);
  if (parentRank) setNavParent(`#${parentRank}/${encodeURIComponent(L[parentRank])}`, higherNames(L[parentRank]).main);
  else setNavParent('#species', tr('nav.species'));
  const dated = r.assets.filter(a => a.taken_at).sort((a, b) => a.taken_at < b.taken_at ? -1 : 1);
  const places = [...new Set(r.assets.map(a => a.city).filter(Boolean))];
  const countries = [...new Set(r.assets.map(a => a.country).filter(Boolean))];
  const crumbs = ['kingdom', 'phylum', 'class', 'order', 'family', 'genus'].filter(k => L[k]).map(k => higherLink(k, L[k], crumbLabel(L[k]))).join('<span class="sep">›</span>');
  const iucn = t.iucn && iucnName(t.iucn);
  const grid = q => `#photos?filter=all&taxon=${key}${q}`, open = a => `#species/${key}?photo=${encodeURIComponent(a.asset_id)}`;
  const kpi = (href, big, small) => `<div><a class="kpilink" href="${href}"><b>${big}</b></a><span>${small}</span></div>`;
  const countryLinks = countries.map(c => `<a href="${grid(`&country=${encodeURIComponent(c)}`)}">${esc(c)}</a>`).join(', ');
  // At the top: the species and its figures; below, the article on the left and the map on the right, of the same height,
  // separated by movable dividers (layout.js); then the photos.
  root.innerHTML = `<section class="sphero card" style="--gc:${g.c}">
      <div class="sptext">
        <div class="sphead"><span class="spthumb" title="${esc(tr('cover.open'))}"></span><div>
        <a class="gchip" href="#species?group=${g.id}"><i class="sw" style="background:${g.c}"></i>${g.icon} ${esc(g.name)}</a>
        <h1>${esc(shortName(t))}</h1>
        <div class="sci"><i>${esc(t.scientific_name)}</i> <span class="auth">${esc(t.authority || '')}</span></div></div></div>
        ${otherNames(t)}
        <div class="crumbs">${crumbs}</div>
        <div class="badges">${iucn ? `<a class="badge2" href="#species?iucn=${t.iucn}"><span class="iucn-dot" style="background:${IUCN_COLOR[t.iucn]}"></span>${tr('iucn.long', { cat: esc(iucn) })}</a>` : ''}
          ${t.wikidata ? `<a class="badge2" href="https://www.wikidata.org/wiki/${esc(t.wikidata)}" target="_blank" rel="noopener">Wikidata ↗</a>` : ''}
          <a class="badge2" href="https://www.gbif.org/species/${key}" target="_blank" rel="noopener">GBIF ↗</a></div>
        <div class="spkpis">
          ${kpi(grid(''), r.assets.length, tr('sp.photos', { n: r.assets.length }))}
          <div><a class="kpilink" href="${grid('')}"><b>${places.length || '—'}</b></a><span>${tr('sp.places', { n: places.length })}${countries.length ? ` · ${countryLinks}` : ''}</span></div>
          ${dated.length ? kpi(open(dated[0]), fmtDate(dated[0].taken_at), tr('sp.first')) : `<div><b>—</b><span>${tr('sp.first')}</span></div>`}
          ${dated.length > 1 ? kpi(open(dated[dated.length - 1]), fmtDate(dated[dated.length - 1].taken_at), tr('sp.last')) : ''}
        </div>
      </div>
    </section>
    ${midHTML(P_.id, tr('sp.map_hint'))}
    <section class="card facts spfacts"></section>
    <h2 class="sph">${tr('nav.photos')}</h2>
    <div class="spphotos"></div>`;
  P_.cover = r.cover;
  root.querySelector('.spthumb').onclick = () => { if (P_.cover) location.hash = open(P_.cover); };
  bindMapWiki(P_, root);
  bindMid(P_, root);
  drawRangeMap(P_, r.assets);
  loadPanelWiki(P_);
  showCover(P_, root);
  // Around where most of its photos were taken; the months of your photos of that region coloured in the season.
  if (r.taxon) showFacts(root.querySelector('.spfacts'), key, null, { assets: r.assets });
  await embedGrid(root.querySelector('.spphotos'), { taxon: key, ...scope }, assetId => setCover(key, assetId));
  starTiles();
}
/* Main photo: thumbnail on the left of the title, and a star on each photo of the species to choose it
   (full star: main photo; clicking the one chosen by hand goes back to the automatic choice). */
function showCover(P_, root) {
  const c = P_.cover, th = root.querySelector('.spthumb');
  th.innerHTML = c ? `<img src="/api/thumb/${c.asset_id}?size=preview" style="${focus(c.box)}" alt="">` : '';
  th.hidden = !c;
}
function starTiles() {
  if (STATE.view !== 'sp') return;
  const c = SPP.cover;
  $$('#pgrid .tile').forEach(tl => {
    const a = P.assets[+tl.dataset.i], on = c?.asset_id === a.asset_id;
    let b = tl.querySelector('.star');
    if (!b) { b = document.createElement('button'); b.className = 'star'; tl.appendChild(b); }
    b.classList.toggle('on', on); b.textContent = on ? '★' : '☆';
    b.title = t(on ? (c.chosen ? 'cover.chosen' : 'cover.auto') : 'cover.set');
  });
}
/* Choose the main photo of a taxon (from the record or the viewer); clicking it again: back to the automatic choice. */
async function setCover(key, assetId) {
  const onPage = STATE.view === 'sp' && SPP.key === key;
  const reset = onPage && SPP.cover?.chosen && SPP.cover.asset_id === assetId;
  try {
    const r = await api(`/api/species/${key}/cover`, { method: 'PUT', body: JSON.stringify({ asset_id: reset ? null : assetId }) });
    if (onPage) { SPP.cover = r.cover; showCover(SPP, $('#sppage')); starTiles(); }
    toast(t(reset ? 'cover.reset_done' : 'cover.saved'));
  } catch (e) { fail(e); }
}
/* Range map and article (Wikipedia or Wikispecies) side by side: species page and
   Identify page. P: state of the block (id prefix, taxon, source and language of the article, Leaflet map). */
function mapWikiHTML(id, hint) {
  return `<div class="sp2">${mapCardHTML(id, hint)}<section class="card spwiki">${wikiCardHTML(id)}</section></div>`;
}
function mapCardHTML(id, hint) {
  return `<section class="card spmap"><h2 class="sph">${t('map.range')} <span title="${esc(hint)}">${hint}</span></h2>
    <div id="${id}map" class="rmap"></div><div id="${id}legend" class="minicap"></div></section>`;
}
function wikiCardHTML(id) {
  return `<div class="wbar">
      <button class="chip" data-ws="wikipedia">${t('wiki.wikipedia')}</button>
      <button class="chip" data-ws="wikispecies">Wikispecies</button>
      <select class="pill" id="${id}wlang"></select></div>
    <div class="wbody" id="${id}wbody"><div class="empty">${t('common.loading')}</div></div>`;
}
function bindMapWiki(P, root) {
  const bs = root.querySelectorAll('[data-ws]');
  bs.forEach(b => { b.classList.toggle('on', b.dataset.ws === P.wsrc);
    b.onclick = () => { P.wsrc = b.dataset.ws; bs.forEach(x => x.classList.toggle('on', x === b)); loadPanelWiki(P); }; });
  $(`#${P.id}wlang`).onchange = e => { P.wlang = e.target.value; loadPanelWiki(P); };
}
async function loadPanelWiki(P) {
  const key = P.key, body = $(`#${P.id}wbody`), sel = $(`#${P.id}wlang`);
  const label = P.wsrc === 'wikispecies' ? 'Wikispecies' : t('wiki.wikipedia');
  body.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  try {
    const a = await api(`/api/wiki/${key}?src=${P.wsrc}&lang=${P.wlang}`);
    if (P.key !== key || !body.isConnected) return;
    sel.style.display = P.wsrc === 'wikipedia' ? '' : 'none';
    sel.innerHTML = a.languages.map(l => `<option value="${esc(l)}" ${l === (a.lang || P.wlang) ? 'selected' : ''}>${esc(langName(l))}</option>`).join('');
    body.innerHTML = `<h1>${esc(a.title)}</h1><a class="readon" href="${esc(a.url)}" target="_blank" rel="noopener">${t('wiki.read_on', { site: label })} ↗</a>
      ${a.thumbnail && P.wsrc === 'wikipedia' ? `<img class="hero" src="${esc(a.thumbnail)}" alt="">` : ''}
      ${a.html || `<p><i>${t('wiki.no_text')}</i></p>`}`;
    body.scrollTop = 0;
  } catch (e) { if (P.key === key) body.innerHTML = `<div class="empty">${t('wiki.no_page', { site: label })}</div>`; }
}
/* pts: photos to place on the map ({lat, lon}, with asset_id for the preview, or img: local image). */
async function drawRangeMap(P, pts) {
  if (P.map) { P.map.remove(); P.map = null; }
  const m = P.map = L.map(`${P.id}map`, { ...MAP_TOUCH, worldCopyJump: true, scrollWheelZoom: true }).addLayer(baseLayer());
  pts = pts.filter(a => a.lat != null);
  if (pts.length) m.fitBounds(pts.map(a => [a.lat, a.lon]), { padding: [40, 40], maxZoom: 5, animate: false }); else m.setView([30, 10], 2, { animate: false });
  const req = P.mapReq = (P.mapReq || 0) + 1, legend = $(`#${P.id}legend`);
  try {
    const rl = await rangeLayer(P.key, m);
    if (req !== P.mapReq || m !== P.map) return;
    rl.addTo(m);
    legend.innerHTML = rl.cells ? `<div class="maplegend inline">${rangeLegend(rl.breaks, rl.max)}</div>` : t('map.no_obs');
  } catch (e) { if (req === P.mapReq) legend.textContent = t('map.unavailable'); }
  pts.forEach(a => L.circleMarker([a.lat, a.lon], photoIcon(true))
    .bindPopup(`<img src="${a.img || `/api/thumb/${a.asset_id}?size=thumbnail`}" style="width:170px;border-radius:8px">${a.asset_id ? `<br>${esc([a.city, a.country].filter(Boolean).join(', '))}` : ''}`).addTo(m));
  setTimeout(() => { if (P.map === m && m.getContainer().isConnected) m.invalidateSize(); }, 60);   // map still shown
}
