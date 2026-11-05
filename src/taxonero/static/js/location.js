/* ============================== lieu indiqué à la main ============================== */
/* Photos sans GPS : on clique sur la carte à l'endroit de la prise de vue. Le lieu aide l'identification (espèces
   présentes dans la région) ; un GPS de la source n'est jamais remplacé. Le dernier lieu choisi est retenu dans ce
   navigateur pour l'appliquer d'un clic aux photos suivantes de la même sortie. */
const LOC = { map: null, marker: null, resolve: null };
const lastLoc = () => { try { return JSON.parse(localStorage.getItem('last-location') || 'null'); } catch (e) { return null; } };
const saveLastLoc = p => { try { localStorage.setItem('last-location', JSON.stringify(p)); } catch (e) {} };

/* Ouvre la carte ; renvoie {lat, lon}, 'clear' (effacer le lieu indiqué) ou null (annulé). */
function pickLocation({ current = null, canClear = false } = {}) {
  const box = $('#locpick'), last = lastLoc();
  box.innerHTML = `<div class="lpcard" role="dialog" aria-modal="true" aria-labelledby="lp-title">
    <div class="lphead"><b id="lp-title">${t('loc.title')}</b><button class="iconbtn" data-a="cancel" title="${esc(t('loc.cancel'))}">✕</button></div>
    <div class="sub lphint">${t('loc.hint')}</div>
    <div id="lpmap"></div>
    <div class="lpbtns">
      ${last ? `<button class="btn" data-a="last">${t('loc.same')}</button>` : ''}
      ${canClear ? `<button class="btn" data-a="clear">${t('loc.clear')}</button>` : ''}
      <span class="grow"></span>
      <button class="btn" data-a="cancel">${t('loc.cancel')}</button>
      <button class="btn primary" data-a="save" disabled>${t('loc.save')}</button>
    </div></div>`;
  box.classList.add('on');
  LOC.map?.remove();
  LOC.map = L.map('lpmap', { worldCopyJump: true }).addLayer(baseLayer());
  const start = current || last;
  if (start) LOC.map.setView([start.lat, start.lon], current ? 11 : 8); else LOC.map.setView([46.5, 2.5], 5);
  LOC.marker = null;
  const place = ll => {
    const p = { lat: +ll.lat.toFixed(5), lon: +L.latLng(ll).wrap().lng.toFixed(5) };
    if (LOC.marker) LOC.marker.setLatLng(ll); else LOC.marker = L.circleMarker(ll, photoIcon(true)).addTo(LOC.map);
    LOC.chosen = p; box.querySelector('[data-a=save]').disabled = false;
  };
  if (current) place(L.latLng(current.lat, current.lon));
  LOC.map.on('click', e => place(e.latlng));
  setTimeout(() => LOC.map.invalidateSize());
  return new Promise(resolve => {
    const done = v => { box.classList.remove('on'); LOC.map?.remove(); LOC.map = null; document.removeEventListener('keydown', esc_, true); resolve(v); };
    const esc_ = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
    document.addEventListener('keydown', esc_, true);
    box.onclick = e => {
      const a = e.target.closest('[data-a]')?.dataset.a;
      if (e.target === box || a === 'cancel') done(null);
      else if (a === 'clear') done('clear');
      else if (a === 'last') { saveLastLoc(last); done(last); }
      else if (a === 'save' && LOC.chosen) { saveLastLoc(LOC.chosen); done(LOC.chosen); }
    };
  });
}

/* Envoie le lieu au serveur pour une sélection (même forme que la relecture en lot) ; renvoie la réponse. */
async function sendLocation(sel, p) {
  const body = { ...sel, ...(p === 'clear' ? { lat: null, lon: null } : p) };
  const r = await api('/api/assets/location', { method: 'POST', body: JSON.stringify(body) });
  toast((p === 'clear' ? t('loc.cleared', { photos: t('count.photos', { n: r.changed }) }) : t('loc.saved', { photos: t('count.photos', { n: r.changed }) }))
    + (r.skipped && p !== 'clear' ? ` · ${t('loc.skipped', { photos: t('count.photos', { n: r.skipped }) })}` : '')
    + (r.background ? ` · ${t('loc.background')}` : ''));
  return r;
}

/* Ligne « lieu » de la visionneuse : pas de GPS, ou lieu indiqué à la main (modifiable). */
function locLine(a) {
  if (a.lat != null && !a.geo_user) return '';
  return `<div class="locline">📍 ${a.geo_user ? t('loc.user') : t('loc.none')}
    <button class="linkbtn" id="b-loc">${a.geo_user ? t('loc.change') : t('loc.set')}</button></div>`;
}
async function editLocation() {
  const a = cur(); if (!a) return;
  const p = await pickLocation({ current: a.geo_user ? { lat: a.lat, lon: a.lon } : null, canClear: !!a.geo_user });
  if (!p) return false;
  try { await sendLocation({ asset_ids: [a.asset_id] }, p); await refreshAsset(); drawViewer(); return true; } catch (e) { fail(e); return false; }
}
