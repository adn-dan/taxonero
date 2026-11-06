/* ============================== page Identifier ============================== */
/* Une image déposée ici est identifiée puis oubliée : envoyée au serveur le temps de l'analyse (rien n'y est
   enregistré), affichée depuis la mémoire du navigateur et effacée dès qu'on quitte la page. */
const ID = { url: null, req: 0 };
const IDP = { id: 'id', key: null, wsrc: 'wikipedia', wlang: '', map: null };   // carte + article du taxon choisi
function loadIdentify() {
  $('#identify').innerHTML = `<section class="card idzone" id="idzone">${ic('camera')}<p>${t('id.intro')}</p>
    <label class="btn primary"><span class="first">${t('id.choose')}</span><span class="next">${t('id.another')}</span><input type="file" id="idfile" accept="image/*" hidden></label>
    <p class="sub">${t('id.drop')}</p><p class="sub">${t('id.private')}</p><p class="sub again">${t('id.drop_short')}</p></section><div id="idresult"></div>`;
  const zone = $('#idzone');
  $('#idfile').onchange = e => { if (e.target.files[0]) identifyFile(e.target.files[0]); e.target.value = ''; };
  zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => { e.preventDefault(); zone.classList.remove('over');
    const f = [...e.dataTransfer.files].find(x => x.type.startsWith('image/')); if (f) identifyFile(f); };
}
document.addEventListener('paste', e => {   // Ctrl+V d'une image copiée
  if (STATE.view !== 'identify') return;
  const f = [...(e.clipboardData?.files || [])].find(x => x.type.startsWith('image/'));
  if (f) { e.preventDefault(); identifyFile(f); }
});
function clearIdentify() {
  if (ID.url) URL.revokeObjectURL(ID.url);
  if (IDP.map) { IDP.map.remove(); IDP.map = null; }
  IDP.key = null;
  ID.url = null; ID.req++;
  const r = $('#idresult'); if (r) r.innerHTML = '';
}
/* place : lieu indiqué à la main, pour une image sans GPS ; il sert à cette identification et n'est pas enregistré. */
async function identifyFile(file, place = null) {
  clearIdentify();
  Object.assign(ID, { file, place });
  const req = ID.req; ID.url = URL.createObjectURL(file);
  $('#idzone')?.classList.add('compact');
  $('#idresult').innerHTML = `<section class="card idphoto waiting"><div class="empty idwait"><span id="idwmsg">${t('det.identifying')}</span><span class="sub" id="idwtime"></span></div>
    <div class="idframe"><img src="${ID.url}" alt=""></div></section>`;
  // Modèles pas encore en mémoire (premier usage depuis le démarrage) : on le dit, l'attente est plus longue.
  // Pas encore téléchargés (tout premier usage) : plusieurs Go, on le dit aussi.
  api('/api/sync/status').then(st => { if (req === ID.req && !st.models_loaded && $('#idwmsg')) $('#idwmsg').textContent = t(st.models_on_disk ? 'id.loading_models' : 'id.downloading_models'); }).catch(() => {});
  const t0 = Date.now(), tick = setInterval(() => {
    const el = $('#idwtime'); if (req !== ID.req || !el) return clearInterval(tick);
    el.textContent = new Intl.NumberFormat(LANG, { style: 'unit', unit: 'second', unitDisplay: 'short' }).format(Math.round((Date.now() - t0) / 1000));
  }, 1000);
  let r;
  try { r = await api(`/api/identify${place ? `?lat=${place.lat}&lon=${place.lon}` : ''}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } }); }
  catch (e) { if (req === ID.req) $('#idresult').innerHTML = `<div class="empty">${t('toast.failed', { msg: esc(e.message) })}</div>`; return; }
  finally { clearInterval(tick); }
  if (req === ID.req) renderIdentify(r);
}
/* Lieu indiqué pour une image sans GPS : identification relancée avec ce lieu. */
async function locateIdentified() {
  const p = await pickLocation({ current: ID.place });
  if (p) { closeCompare(); identifyFile(ID.file, p); }
}
function renderIdentify(r) {
  const dets = r.detections, gps = r.lat != null;
  const boxes = dets.map((d, k) => `<div class="idbx${d.w * d.h > 0.98 ? ' full' : ''}" style="left:${d.x * 100}%;top:${d.y * 100}%;width:${d.w * 100}%;height:${d.h * 100}%"><span>${k + 1}</span></div>`).join('');
  const cards = dets.map((d, k) => { const tx = d.taxon;
    return `<section class="card idcard"><div class="sectit">${k + 1} · ${esc(rankName(d.taxon_rank))}</div>
      <h2>${subjectIcon(d)} ${esc(shortName(tx))}${d.taxon_rank !== 'none' ? ` <small class="sub">${fmtProb(d.score)}</small>` : ''}</h2>
      ${tx ? `<div><i>${esc(tx.scientific_name)}</i></div>` : ''}
      ${r.mine[d.taxon_key] ? `<a class="idmine" href="#species/${d.taxon_key}">${t('id.in_collection', { n: r.mine[d.taxon_key] })} →</a>` : ''}
      ${d.domestic ? `<div class="why">${t('id.domestic')}</div>` : ''}
      <div class="sectit" style="margin-top:12px">${t('cand.title')} <span style="text-transform:none;letter-spacing:0">· ${t('id.cand_hint')}</span></div>
      ${d.candidates.length ? `<button class="btn cmpbtn" data-cmp="${k}" title="${esc(t('cmp.hint'))}">${ic('search')} ${t('cmp.button')}</button>` : ''}
      ${d.candidates.map(c => `<div class="cand" data-key="${c.gbif_key || ''}" data-name="${esc(c.name)}"><span class="k">${ic('book')}</span>
        <span><span class="cn" data-ck="${c.gbif_key || ''}">${candName(c)}</span><small>${esc(c.lineage?.family || '')}${gps ? ` · <span class="dot ${c.plausibility}"></span>${t(`plaus.${c.plausibility || 'unknown'}`)}` : ''}</small></span>
        <span class="p${c.p < 0.005 ? ' low' : ''}">${fmtProb(c.p)}</span></div>`).join('')}
    </section>`; }).join('');
  $('#idresult').innerHTML = `<div class="idlayout"><section class="card idphoto"><div class="idframe"><img src="${ID.url}" alt="">${boxes}</div>
      <p class="sub">${gps && ID.place ? `📍 ${t('loc.user')} (${r.lat.toLocaleString(LANG, { maximumFractionDigits: 3 })}, ${r.lon.toLocaleString(LANG, { maximumFractionDigits: 3 })})` : gps ? t('id.gps', { lat: r.lat.toLocaleString(LANG, { maximumFractionDigits: 3 }), lon: r.lon.toLocaleString(LANG, { maximumFractionDigits: 3 }) }) : t('id.no_gps')}${!gps || ID.place ? ` <button class="linkbtn" id="id-loc">${t(ID.place ? 'loc.change' : 'loc.set')}</button>` : ''}</p>
      ${dets.length ? '' : `<div class="empty">${t('id.none')}</div>`}</section>
    <div class="idcards">${cards}</div></div><div class="idmore" id="idmore"></div>`;
  const root = $('#idresult');
  dets.forEach(d => fillNames(d.candidates, root));
  root.querySelectorAll('.cand[data-key]').forEach(el => el.onclick = () => selectIdTaxon(+el.dataset.key, el.dataset.name, r));
  root.querySelectorAll('[data-cmp]').forEach(b => b.onclick = () => { const d = dets[+b.dataset.cmp];
    openCompare({ img: root.querySelector('.idframe img'), box: d, candidates: d.candidates, onPick: c => selectIdTaxon(c.gbif_key, c.name, r),
      place: { lat: r.lat, lon: r.lon, user: !!ID.place },
      onLocate: locateIdentified }); });
  $('#id-loc') && ($('#id-loc').onclick = locateIdentified);
  // Par défaut : le taxon retenu pour le premier sujet identifié. Il peut être un genre ou une famille
  // (candidats trop proches), absent alors de la liste des candidats.
  const first = dets.find(d => d.taxon_key && d.taxon_rank !== 'none');
  if (first) selectIdTaxon(first.taxon_key, first.taxon?.scientific_name || '', r);
}
/* Carte et article sous le résultat, pour le taxon choisi (fiche récupérée au besoin). */
async function selectIdTaxon(key, name, r) {
  if (!key || key === IDP.key) return;
  const req = ID.req;
  $$('#idresult .cand').forEach(x => x.classList.toggle('sel', +x.dataset.key === key));
  IDP.key = key; IDP.wlang = LANG;   // article dans la langue de l'interface (le menu permet d'en changer)
  let tx;
  try { tx = await api(`/api/taxon/${key}?name=${encodeURIComponent(name)}`); } catch (e) { tx = null; }
  if (req !== ID.req || IDP.key !== key) return;
  const box = $('#idmore');
  box.innerHTML = `<h2 class="idsel">${esc(tx ? shortName(tx) : name)}${tx && shortName(tx) !== tx.scientific_name ? ` <i>${esc(tx.scientific_name)}</i>` : ''}</h2>
    ${mapWikiHTML('id', t(r.lat != null ? 'id.map_hint' : 'id.map_hint_nogps'))}`;
  bindMapWiki(IDP, box);
  drawRangeMap(IDP, r.lat != null ? [{ lat: r.lat, lon: r.lon, img: ID.url }] : []);
  loadPanelWiki(IDP);
}
/* Noms communs d'une liste de candidats, dans un bloc donné (récupérés en arrière-plan s'ils manquent). */
async function fillNames(cands, root, tries = 0) {
  const refs = cands.filter(c => c.gbif_key && !NAMES.has(c.gbif_key)).map(c => ({ key: c.gbif_key, name: c.name }));
  if (!refs.length) return;
  let r;
  try { r = await api('/api/names', { method: 'POST', body: JSON.stringify(refs) }); } catch (e) { return; }
  Object.entries(r.names).forEach(([k, v]) => NAMES.set(+k, v));
  if (!root.isConnected) return;
  root.querySelectorAll('.cn').forEach(el => { const c = cands.find(x => String(x.gbif_key) === el.dataset.ck); if (c) el.innerHTML = candName(c); });
  if (r.pending && tries < 6) setTimeout(() => fillNames(cands, root, tries + 1), 2500);
}
