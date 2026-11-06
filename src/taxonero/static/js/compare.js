/* ============================== comparaison en images ============================== */
/* The subject (cropped from the photo) on the left, and for each candidate a row of reference photos
   (iNaturalist, Creative Commons licences). Clicking a reference photo shows it next to the subject.
   Keyboard: 1-5 candidate, left and right arrows reference photo, up and down another photo of your series, Enter
   choose the species (always for the starting detection).
   Each photo can be zoomed (wheel, pinch, mouse click, double tap with a finger) and moved; at the first zoom, its
   full-resolution version replaces the one shown.
   Used by the viewer and the Identify page. */
const CMP = { open: false, req: 0, onPick: null, cands: [], current: null, k: -1, refs: new Map() };
const REF = new Map();   // gbif_key -> promise of the reference photos (one request per taxon and per session)
const refPhotos = c => {
  if (!REF.has(c.gbif_key)) REF.set(c.gbif_key, api(`/api/refphotos/${c.gbif_key}?name=${encodeURIComponent(c.name)}`)
    .catch(() => { REF.delete(c.gbif_key); return null; }));
  return REF.get(c.gbif_key);
};
/* Crop of the subject (with a margin) in an image already loaded, as a data URL. */
function subjectCrop(img, box) {
  const W = img.naturalWidth, H = img.naturalHeight;
  if (!W || !H) return '';
  const m = 0.1, x = Math.max(0, (box.x - box.w * m) * W), y = Math.max(0, (box.y - box.h * m) * H);
  const w = Math.min(W - x, box.w * (1 + 2 * m) * W), h = Math.min(H - y, box.h * (1 + 2 * m) * H);
  const k = Math.min(1, 4096 / Math.max(w, h)), cv = document.createElement('canvas');
  cv.width = Math.round(w * k); cv.height = Math.round(h * k);
  cv.getContext('2d').drawImage(img, x, y, w, h, 0, 0, cv.width, cv.height);
  try { return cv.toDataURL('image/jpeg', 0.9); } catch (e) { return ''; }
}
/* Zoom of a photo in its frame; hires(img): replaces the image with its full resolution, once. */
function cmpZoom(frame, hires) {
  const img = frame.querySelector('img'); if (!img) return;
  const pz = Panzoom(img, { minScale: 1, maxScale: 8, contain: 'outside', cursor: '' });
  img.addEventListener('panzoomchange', e => {
    const on = e.detail.scale > 1.001;
    frame.classList.toggle('zoomed', on);
    if (on && hires) { const h = hires; hires = null; h(img); }
  });
  frame.addEventListener('wheel', e => { e.preventDefault();
    pz.zoomToPoint(Math.min(8, Math.max(1, pz.getScale() * wheelFactor(e))), { clientX: e.clientX, clientY: e.clientY }, { animate: false }); }, { passive: false });
  // Detection by hand: Panzoom cancels mouse events, click and dblclick do not arrive.
  // Mouse: one click zooms or goes back (the second click of a double click is ignored). Finger: double tap.
  const toggle = e => {
    if (pz.getScale() > 1.001) pz.reset({ animate: false });
    else pz.zoomToPoint(3, { clientX: e.clientX, clientY: e.clientY }, { animate: false });
  };
  let last = null, press = null, lastClick = -1000;
  frame.addEventListener('pointerdown', e => { press = { x: e.clientX, y: e.clientY }; }, true);   // capture: Panzoom stops it on the image
  frame.addEventListener('pointerup', e => {
    const still = press && Math.hypot(e.clientX - press.x, e.clientY - press.y) < 5;
    press = null;
    if (e.pointerType === 'mouse') {
      if (!still) return;
      if (e.timeStamp - lastClick >= 350) toggle(e);
      lastClick = e.timeStamp;
      return;
    }
    const dbl = last && e.timeStamp - last.t < 350 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 20;
    last = dbl ? null : { t: e.timeStamp, x: e.clientX, y: e.clientY };
    if (dbl) toggle(e);
  });
}
/* Place of your photo: coordinates, or a link to enter it. A GPS from the photo cannot be changed. */
function cmpPlace({ place, onLocate }) {
  if (!place) return '';
  const known = place.lat != null, coord = v => v.toLocaleString(LANG, { maximumFractionDigits: 4 });
  const text = known ? `${place.user ? `${t('loc.user')} · ` : ''}${coord(place.lat)}, ${coord(place.lon)}` : t('loc.none');
  return `<span class="cmploc">📍 <span title="${esc(text)}">${esc(text)}</span>${onLocate && (!known || place.user)
    ? `<button class="linkbtn" id="cmp-loc">${t(known ? 'loc.change' : 'loc.set')}</button>` : ''}</span>`;
}
/* Zoom of your photo; at the first zoom, the subject cropped from the original. */
function cmpMineZoom(source, box, full) {
  const req = CMP.req;
  cmpZoom($('#cmp-mine'), full && (img => {
    const im = new Image(), ar = source.naturalWidth / source.naturalHeight;
    im.onload = () => {   // different proportions (photo cropped in Immich): the box would no longer fit
      if (req !== CMP.req || !img.isConnected || Math.abs(im.naturalWidth / im.naturalHeight / ar - 1) > 0.02) return;
      const hi = subjectCrop(im, box || { x: 0, y: 0, w: 1, h: 1 });
      if (hi) img.src = hi;
    };
    im.src = full;   // on failure (no permission, unreadable RAW): the preview is kept
  }));
}
/* Series of your photo: the other shots of the same subject, to see it from other angles. */
async function cmpLoadSeries(id, img, box, full, req) {
  const r = await api(`/api/assets/${id}/series`).catch(() => null);
  if (req !== CMP.req || !r || r.assets.length < 2) return;
  CMP.series = { ids: r.assets, i: r.assets.indexOf(id), first: id, req: 0, start: { img, box, full } };
  const nav = $('#cmp-series'); nav.hidden = false;
  nav.innerHTML = `<button class="linkbtn" id="cmp-sprev" aria-label="${esc(t('cmp.series_prev'))}" title="${esc(t('cmp.series_prev'))}">‹</button><span id="cmp-sn"></span><button class="linkbtn" id="cmp-snext" aria-label="${esc(t('cmp.series_next'))}" title="${esc(t('cmp.series_next'))}">›</button>`;
  $('#cmp-sprev').onclick = () => cmpSeriesGo(-1);
  $('#cmp-snext').onclick = () => cmpSeriesGo(1);
  cmpSeriesCount();
}
function cmpSeriesCount() { $('#cmp-sn').textContent = `${CMP.series.i + 1}/${CMP.series.ids.length}`; }
/* The same subject in another photo: the detection that best proposes one of the compared candidates, failing that the
   first one, and the whole photo if there is none. */
function cmpSubjectIn(asset) {
  const keys = new Set(CMP.cands.map(c => c.gbif_key));
  const score = d => keys.has(d.taxon_key) ? 2 : Math.max(0, ...(d.candidates || []).filter(c => keys.has(c.gbif_key)).map(c => c.p || 0));
  const dets = (asset.detections || []).filter(d => !['rejected', 'deleted'].includes(d.status));
  return dets.reduce((best, d) => !best || score(d) > score(best) ? d : best, null);
}
async function cmpSeriesGo(step) {
  const sr = CMP.series; if (!sr) return;
  const n = sr.ids.length, i = (sr.i + step + n) % n, id = sr.ids[i], req = CMP.req, go = ++sr.req;
  sr.i = i; cmpSeriesCount();
  const stale = () => req !== CMP.req || go !== sr.req;
  let view = sr.start;
  if (id !== sr.first) {
    const a = await api(`/api/assets/${id}`).catch(() => null);
    if (stale()) return;
    if (!a) { toast(t('cmp.series_error')); return; }
    const img = new Image();
    img.src = `/api/thumb/${id}`;
    try { await img.decode(); } catch (e) { if (!stale()) toast(t('cmp.series_error')); return; }
    if (stale()) return;
    view = { img, box: cmpSubjectIn(a), full: a.kind !== 'VIDEO' ? `/api/full/${id}` : null };
  }
  const crop = subjectCrop(view.img, view.box || { x: 0, y: 0, w: 1, h: 1 });
  if (!crop) return;
  $('#cmp-mine')?.remove();
  const frame = document.createElement('div'); frame.className = 'cmpzoom'; frame.id = 'cmp-mine';
  frame.innerHTML = `<img src="${crop}" alt="">`;
  $('#cmp-yours').append(frame);
  cmpMineZoom(view.img, view.box, view.full);
}
/* opts : { img, box, candidates: [{gbif_key, name, p}], onPick(c) | null, focus: gbif_key, full: URL de l'original,
   place: {lat, lon, user} (user: entered by hand), onLocate(): enter or change the place,
   asset: id of the photo, to browse its series } */
function openCompare(opts) {
  const req = ++CMP.req, cands = opts.candidates.filter(c => c.gbif_key).slice(0, 5);
  Object.assign(CMP, { open: true, onPick: opts.onPick || null, cands, current: opts.focus ?? cands[0]?.gbif_key, k: -1, refs: new Map() });
  let el = $('#compare');
  if (!el) { el = document.createElement('div'); el.id = 'compare'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true'); el.tabIndex = -1; document.body.append(el);
    // Mouse click: the button gives the focus back, so Enter still chooses the species.
    el.addEventListener('click', e => { if (e.detail) e.target.closest?.('button,a')?.blur(); }); }
  CMP.series = null;
  const crop = subjectCrop(opts.img, opts.box || { x: 0, y: 0, w: 1, h: 1 });
  el.innerHTML = `<div class="cmpbar"><b id="cmp-title">${t('cmp.title')}</b><span class="sub">${t('cmp.source')} · ${t('cmp.zoom_hint')}</span>
      <button class="iconbtn" id="cmp-x" title="${esc(t('viewer.close'))}">${ic('close')}</button></div>
    <div class="cmpbody"><div class="cmpside">
        <figure id="cmp-yours"><figcaption class="cmpcap"><span>${t('cmp.yours')}</span><span class="cmpnav" id="cmp-series" hidden></span>${cmpPlace(opts)}</figcaption>${crop ? `<div class="cmpzoom" id="cmp-mine"><img src="${crop}" alt=""></div>` : ''}</figure>
        <figure id="cmp-big"><figcaption>${t('cmp.ref')}</figcaption><div class="sub">${t('cmp.pick_ref')}</div></figure></div>
      <div class="cmprows">${cands.map((c, k) => `<section class="cmprow" data-key="${c.gbif_key}">
        <div class="cmphead"><kbd>${k + 1}</kbd><span class="cn" data-ck="${c.gbif_key}">${candName(c)}</span>${opts.place?.lat != null ? `<span class="plaus"><span class="dot ${c.plausibility}"></span>${t(`plaus.${c.plausibility || 'unknown'}`)}</span>` : ''}${c.p != null ? `<span class="p">${fmtProb(c.p)}</span>` : ''}
          ${CMP.onPick ? `<button class="btn" data-pick="${c.gbif_key}">${ic('check')} ${t('cand.pick')}</button>` : ''}</div>
        <div class="cmpstrip"><div class="sub">${t('common.loading')}</div></div></section>`).join('')}</div></div>`;
  el.classList.add('on'); el.setAttribute('aria-labelledby', 'cmp-title');
  if (crop) cmpMineZoom(opts.img, opts.box, opts.full);
  if (opts.asset) cmpLoadSeries(opts.asset, opts.img, opts.box, opts.full, req);
  $('#cmp-x').onclick = closeCompare;
  $('#cmp-loc') && ($('#cmp-loc').onclick = opts.onLocate);
  el.focus();
  cmpMarkRow();
  el.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => cmpPick(cands.find(x => x.gbif_key === +b.dataset.pick)));
  cands.forEach(async c => {
    const r = await refPhotos(c);
    const strip = el.querySelector(`.cmprow[data-key="${c.gbif_key}"] .cmpstrip`);
    if (req !== CMP.req || !strip) return;
    if (!r) { strip.innerHTML = `<div class="sub">${t('cmp.unavailable')}</div>`; return; }
    CMP.refs.set(c.gbif_key, r.photos);
    if (!r.photos.length) { strip.innerHTML = `<div class="sub">${t('cmp.none')}</div>`; return; }
    strip.innerHTML = r.photos.map((p, k) => `<button class="cmpimg" data-k="${k}" title="${esc(p.attribution)}"><img loading="lazy" src="${esc(p.thumb)}" alt="${esc(c.name)}"></button>`).join('')
      + (r.taxon ? `<a class="cmpmore" href="${esc(r.taxon.url)}" target="_blank" rel="noopener">iNaturalist ↗</a>` : '');
    strip.querySelectorAll('.cmpimg').forEach(b => b.onclick = () => showRef(c, r.photos[+b.dataset.k]));
    if (CMP.current === c.gbif_key && r.photos[0]) showRef(c, r.photos[0]);
  });
  fillNames(cands, el);
  if (opts.focus) el.querySelector(`.cmprow[data-key="${opts.focus}"]`)?.scrollIntoView({ block: 'nearest' });
}
function cmpMarkRow() { $$('#compare .cmprow').forEach(r => r.classList.toggle('cur', +r.dataset.key === CMP.current)); }
function cmpPick(c) { if (!c || !CMP.onPick) return; const pick = CMP.onPick; closeCompare(); pick(c); }
/* Candidate chosen with the keyboard: its first photo, or the empty frame if it has none (or not yet). */
function cmpSelect(c) {
  CMP.current = c.gbif_key; CMP.k = -1; cmpMarkRow();
  $(`#compare .cmprow[data-key="${c.gbif_key}"]`)?.scrollIntoView({ block: 'nearest' });
  const first = CMP.refs.get(c.gbif_key)?.[0];
  if (first) { showRef(c, first); return; }
  $$('#compare .cmpimg').forEach(b => b.classList.remove('on'));
  $('#cmp-big').innerHTML = `<figcaption>${t('cmp.ref')} · <span class="cn">${candName(c)}</span></figcaption><div class="sub">${t(CMP.refs.has(c.gbif_key) ? 'cmp.none' : 'common.loading')}</div>`;
}
function cmpKey(e) {
  if (e.key === 'Escape') { closeCompare(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (['Enter', ' '].includes(e.key) && e.target.closest?.('button,a')) return;   // the focused button gets activated
  const c = CMP.cands.find(x => x.gbif_key === CMP.current);
  if (/^[1-5]$/.test(e.key)) { if (CMP.cands[+e.key - 1]) cmpSelect(CMP.cands[+e.key - 1]); }
  else if (e.key === 'Enter') cmpPick(c);
  else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && CMP.series) { e.preventDefault(); cmpSeriesGo(e.key === 'ArrowDown' ? 1 : -1); }
  else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    const ps = c && CMP.refs.get(c.gbif_key); if (!ps?.length) return;
    e.preventDefault();
    showRef(c, ps[(CMP.k + (e.key === 'ArrowRight' ? 1 : ps.length - 1)) % ps.length]);
    $('#compare .cmpimg.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
}
function showRef(c, p) {
  CMP.current = c.gbif_key; cmpMarkRow();
  CMP.k = CMP.refs.get(c.gbif_key)?.indexOf(p) ?? -1;
  $$('#compare .cmpimg').forEach(b => b.classList.remove('on'));
  $(`#compare .cmprow[data-key="${c.gbif_key}"] .cmpimg[data-k="${CMP.k}"]`)?.classList.add('on');
  $('#cmp-big').innerHTML = `<figcaption>${t('cmp.ref')} · <span class="cn">${candName(c)}</span></figcaption><div class="cmpzoom"><img src="${esc(p.large)}" alt="${esc(c.name)}"></div>
    <div class="sub cmpcredit">${esc(p.attribution)}${p.url ? ` · <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.license)} ↗</a>` : ''}</div>`;
  cmpZoom($('#cmp-big .cmpzoom'), p.full && (img => { const im = new Image(); im.onload = () => { img.src = im.src; }; im.src = p.full; }));
}
function closeCompare() {
  CMP.open = false; CMP.req++;
  $('#compare')?.classList.remove('on');
}
