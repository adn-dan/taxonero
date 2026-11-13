/* ============================== facts about a taxon ============================== */
/* Beyond the names (server: facts.py): season and presence around a place, status in its country, a few figures, a
   recording. Shown on the species page (around your photos of the species) and in the viewer (around the photo).
   Loaded on its own, after the page: the first time, the server asks GBIF and Wikidata, then keeps the answers. */
const FACTS = new Map();   // taxon and place -> { promise, value once received }
function factsEntry(key, place) {
  const at = place && place.lat != null && place.lon != null ? place : null;
  const k = `${key}${at ? `:${at.lat.toFixed(1)}:${at.lon.toFixed(1)}` : ''}`;
  if (!FACTS.has(k)) {
    if (FACTS.size >= 300) FACTS.delete(FACTS.keys().next().value);
    const e = { value: null };
    e.promise = api(`/api/facts/${key}${at ? `?lat=${at.lat}&lon=${at.lon}` : ''}`).then(v => (e.value = v), () => { FACTS.delete(k); return null; });
    FACTS.set(k, e);
  }
  return FACTS.get(k);
}
const factMonth = (m, width = 'long') => new Date(2001, m, 15).toLocaleDateString(LANG, { month: width });
function factValue(x) {
  if (!x.unit) return fmtN(x.value);
  try { return new Intl.NumberFormat(LANG, { style: 'unit', unit: x.unit, unitDisplay: 'short', maximumFractionDigits: 1 }).format(x.value); }
  catch (e) { return `${fmtN(x.value)} ${x.unit}`; }
}
const factCountry = code => { try { return new Intl.DisplayNames([LANG], { type: 'region' }).of(code) || code; } catch (e) { return code; } };
/* A recording goes on playing after its player has left the page (viewer closed, Back, another photo): stopped then. */
let FACT_AUDIO = null;
document.addEventListener('play', e => { if (e.target.tagName === 'AUDIO') FACT_AUDIO = e.target; }, true);
function stopFactAudio(always) {
  if (FACT_AUDIO && (always || !FACT_AUDIO.isConnected)) { FACT_AUDIO.pause(); FACT_AUDIO = null; }
}
window.addEventListener('hashchange', () => stopFactAudio(true));
/* Your photos per month (12 numbers) among assets, those taken in the region of the season only (the ±5° square around
   place): a photo from the other hemisphere says nothing about the season here. */
function photoMonths(assets, place) {
  const out = Array(12).fill(0);
  (assets || []).forEach(a => {
    const m = a.taken_at ? +a.taken_at.slice(5, 7) : 0;
    if (m < 1 || m > 12 || !place || a.lat == null || a.lon == null) return;
    const dlon = Math.abs(a.lon - place.lon);
    if (Math.abs(a.lat - place.lat) <= 5.5 && Math.min(dlon, 360 - dlon) <= 5.5) out[m - 1]++;
  });
  return out;
}
/* opt.month: month of the photo (0-11), highlighted in the season. opt.assets: your photos of the taxon (species page):
   the months of those taken in the region are coloured, the more photos the stronger. */
function factsHTML(f, opt = {}) {
  if (!f) return '';
  const parts = [];
  if (f.months) {
    const max = Math.max(...f.months, 1), mine = opt.assets ? photoMonths(opt.assets, f.place) : [], most = Math.max(...mine, 1);
    const tip = (n, m) => mine[m] ? tr('facts.month_yours', { month: factMonth(m), n, k: mine[m] }) : tr('facts.month', { month: factMonth(m), n });
    parts.push(`<div class="fseason"><div class="fmonths" role="img" aria-label="${esc(tr(`facts.season_${opt.assets || f.place?.source === 'photos' ? 'photos' : 'photo'}`))}">${f.months.map((n, m) =>
      `<div class="fm${m === opt.month || mine[m] ? ' cur' : ''}" title="${esc(tip(n, m))}"><i style="height:${Math.max(n || mine[m] ? 4 : 0, Math.round(100 * n / max))}%${mine[m] ? `;opacity:${(0.4 + 0.6 * mine[m] / most).toFixed(2)}` : ''}"></i><span>${esc(factMonth(m, 'narrow'))}</span></div>`).join('')}</div>
      <div class="sub">${tr(`facts.season_${opt.assets || f.place?.source === 'photos' ? 'photos' : 'photo'}`)}${mine.some(Boolean) ? ` ${tr('facts.yours_hint')}` : ''}</div></div>`);
  }
  if (f.near != null && f.region != null) {
    parts.push(`<div class="fline"><span class="dot ${esc(f.presence || 'unknown')}"></span>${f.presence ? `${tr(`plaus.${f.presence}`)} · ` : ''}${tr('facts.counts', { near: f.near, region: f.region })}</div>`);
  }
  const statuses = f.status && f.country ? [{ country: f.country, status: f.status }] : [];
  if (statuses.length) {
    const badge = `<div class="fline fstatuses">${statuses.map(x => `<span class="badge2 fstatus ${esc(x.status)}" title="${esc(tr('facts.status_hint'))}">${tr(`facts.status.${x.status}`, { country: esc(factCountry(x.country)) })}</span>`).join('')}</div>`;
    if (f.near != null && f.region != null) parts[parts.length - 1] += badge; else parts.push(badge);
  }
  if (f.traits?.length) {
    parts.push(`<dl class="ftraits">${f.traits.map(x => `<div><dt>${tr(`facts.trait.${x.id}`)}</dt><dd>${esc(factValue(x))}</dd></div>`).join('')}</dl>`);
  }
  if (f.audio || f.xeno_canto) {
    parts.push(`<div class="faudio">${f.audio ? `<audio controls preload="none" aria-label="${esc(tr('facts.audio'))}"><source src="${esc(f.audio.src)}"><source src="${esc(f.audio.mp3)}" type="audio/mpeg"></audio>
      <a class="sub" href="${esc(f.audio.page)}" target="_blank" rel="noopener">${tr('facts.audio')} · Wikimedia Commons ↗</a>` : ''}
      ${f.xeno_canto ? `<a class="sub" href="${esc(f.xeno_canto)}" target="_blank" rel="noopener">${tr('facts.xeno')} ↗</a>` : ''}</div>`);
  }
  if (!f.place && opt.photo) parts.push(`<div class="sub fnote">${tr('facts.no_place')}</div>`);
  // Species page, photos in several regions: one tab per region (season, presence and status of each), one click.
  const name = r => r.country ? factCountry(r.country) : `${fmtN(r.lat)}°, ${fmtN(r.lon)}°`;
  const regions = opt.regions?.length > 1 ? `<div class="ptabs fregions" role="tablist" aria-label="${esc(tr('facts.region'))}">${opt.regions.map((r, i) =>
    `<button role="tab" data-fregion="${i}" aria-selected="${i === (opt.region || 0)}" class="${i === (opt.region || 0) ? 'on' : ''}">${esc(name(r))}<span class="n">${fmtN(r.n)}</span></button>`).join('')}</div>` : '';
  if (regions) return `<div class="sectit">${tr('facts.title')}</div>${regions}<div class="fparts">${parts.map(x => `<div class="fpart">${x}</div>`).join('')}</div>`;
  return parts.length ? `<div class="sectit">${tr('facts.title')}</div><div class="fparts">${parts.map(x => `<div class="fpart">${x}</div>`).join('')}</div>` : '';
}
/* Fill el with the facts of a taxon: at once when they are already known, otherwise when they arrive (unless el has
   been given another taxon or region meanwhile). Species page (place null): the answer lists the regions of your photos;
   choosing another one (tabs) asks for its facts. */
function showFacts(el, key, place, opt = {}) {
  stopFactAudio();   // the player it was started from is gone (panel redrawn)
  if (!el || !key) return;
  const e = factsEntry(key, place);
  el.factsOf = e;
  const draw = v => {
    if (v?.regions) opt = { ...opt, regions: v.regions, region: 0 };
    el.innerHTML = factsHTML(v, opt);
    el.querySelectorAll('[data-fregion]').forEach(tab => tab.onclick = () => { const i = +tab.dataset.fregion, r = opt.regions[i];
      if (i !== (opt.region || 0)) showFacts(el, key, { lat: r.lat, lon: r.lon }, { ...opt, region: i }); });
  };
  if (e.value) { draw(e.value); return; }
  e.promise.then(v => { if (el.isConnected && el.factsOf === e) draw(v); });
}
