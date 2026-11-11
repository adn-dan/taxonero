/* ============================== public identification page ============================== */
/* Page open to anyone while Settings > General > "Public identification page" is on: an image is sent to the server for
   the duration of the analysis (nothing is saved there), shown from the browser memory, and what is recognised comes
   back. On its own: it shares nothing with the dashboard scripts and only calls /api/public/*. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ICON = { animal: '🐾', plant: '🌿', fungus: '🍄', image: '🖼️' };

/* Texts: the catalogues of the dashboard (static/i18n), in the language of the browser, otherwise English; French
   is the reference and fills in a missing text. */
let LANG = 'en', MSG = {}, MSG_FR = {};
const tOr = (key, fallback, p = {}) => (MSG[key] ?? MSG_FR[key] ?? fallback ?? key).replace(/\{(\w+)\}/g, (_, k) => k in p ? p[k] : `{${k}}`);
const t = (key, p) => tOr(key, key, p);
async function loadLang() {
  const get = async l => { try { const r = await fetch(`static/i18n/${l}.json`); return r.ok ? await r.json() : null; } catch (e) { return null; } };
  MSG_FR = await get('fr') || {};
  for (const l of new Set([(navigator.language || 'en').slice(0, 2).toLowerCase(), 'en'])) {
    const m = l === 'fr' ? MSG_FR : /^[a-z]{2}$/.test(l) ? await get(l) : null;
    if (m) { MSG = m; LANG = l; break; }
  }
  document.documentElement.lang = LANG;
}
const fmtPct = x => Number(x).toLocaleString(LANG, { style: 'percent', maximumFractionDigits: 0 });
// Probability from a model: below 0.5 % it reads "< 1 %" (a "0 %" would suggest certainty).
const fmtProb = x => x < 0.005 ? `< ${fmtPct(0.01)}` : fmtPct(x);
const fmtCoord = x => x.toLocaleString(LANG, { maximumFractionDigits: 3 });
const common = v => v?.[LANG] || v?.en || v?.fr || '';

async function api(path, opt = {}) {
  const r = await fetch(path, opt);
  if (!r.ok) {
    const text = await r.text();
    let m = text; try { m = JSON.parse(text).detail ?? text; } catch (e) {}
    throw new Error(m?.code ? tOr(`err.${m.code}`, m.message, m) : typeof m === 'string' ? m : JSON.stringify(m));
  }
  return r.json();
}

const ID = { url: null, req: 0 };
function drawZone() {
  $('#pubtitle').textContent = t('nav.identify');
  document.title = `Taxonero · ${t('nav.identify')}`;
  $('#identify').innerHTML = `<section class="card idzone" id="idzone"><svg class="ic"><use href="#i-camera"/></svg><p>${t('id.intro')}</p>
    <label class="btn primary"><span class="first">${t('id.choose')}</span><span class="next">${t('id.another')}</span><input type="file" id="idfile" accept="image/*" hidden></label>
    <p class="sub">${t('id.drop')}</p><p class="sub">${t('id.private')}</p><p class="sub again">${t('id.drop_short')}</p></section><div id="idresult"></div>`;
  const zone = $('#idzone');
  $('#idfile').onchange = e => { if (e.target.files[0]) identifyFile(e.target.files[0]); e.target.value = ''; };
  zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => { e.preventDefault(); zone.classList.remove('over');
    const f = [...e.dataTransfer.files].find(x => x.type.startsWith('image/')); if (f) identifyFile(f); };
}
document.addEventListener('paste', e => {   // Ctrl+V of a copied image
  const f = [...(e.clipboardData?.files || [])].find(x => x.type.startsWith('image/'));
  if (f) { e.preventDefault(); identifyFile(f); }
});
const failed = msg => { $('#idresult').innerHTML = `<div class="empty">${t('toast.failed', { msg: esc(msg) })}</div>`; };

async function identifyFile(file) {
  if (ID.url) URL.revokeObjectURL(ID.url);
  const req = ++ID.req; ID.url = URL.createObjectURL(file);
  $('#idzone').classList.add('compact');
  $('#idresult').innerHTML = `<section class="card idphoto waiting"><div class="empty idwait"><span id="idwmsg">${t('det.identifying')}</span><span class="sub" id="idwtime"></span></div>
    <div class="idframe"><img src="${ID.url}" alt=""></div></section>`;
  // Too heavy: said before sending it. Models not in memory yet (or not downloaded): the wait is longer, say so.
  const info = await api('/api/public/info').catch(() => null);
  if (req !== ID.req) return;
  if (info && file.size > info.max_mb * 2 ** 20) return failed(t('err.image_too_big', { max: info.max_mb }));
  if (info && !info.models_loaded) $('#idwmsg').textContent = t(info.models_on_disk ? 'id.loading_models' : 'id.downloading_models');
  const t0 = Date.now(), tick = setInterval(() => {
    const el = $('#idwtime'); if (req !== ID.req || !el) return clearInterval(tick);
    el.textContent = new Intl.NumberFormat(LANG, { style: 'unit', unit: 'second', unitDisplay: 'short' }).format(Math.round((Date.now() - t0) / 1000));
  }, 1000);
  let r;
  try { r = await api('/api/public/identify', { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } }); }
  catch (e) { if (req === ID.req) failed(e.message); return; }
  finally { clearInterval(tick); }
  if (req === ID.req) render(r);
}

/* Links to public pages about a taxon: its article in the language of the page (otherwise in another one), and GBIF. */
function links(tx, key) {
  const w = tx?.wikipedia || {}, url = w[LANG] || w.en || Object.values(w)[0];
  const out = [];
  if (url && /^https:\/\//.test(url)) out.push(`<a href="${esc(url)}" target="_blank" rel="noopener">Wikipedia ↗</a>`);
  if (key) out.push(`<a href="https://www.gbif.org/species/${encodeURIComponent(key)}" target="_blank" rel="noopener">GBIF ↗</a>`);
  return out.length ? `<div class="publinks">${out.join('')}</div>` : '';
}
function candName(c) {
  const com = common(c.vernacular);
  return com ? `<b class="cname">${esc(com)}</b> <i>${esc(c.name)}</i>` : `<i>${esc(c.name)}</i>`;
}
function render(r) {
  const dets = r.detections, gps = r.lat != null;
  const boxes = dets.map((d, k) => `<div class="idbx${d.w * d.h > 0.98 ? ' full' : ''}" style="left:${d.x * 100}%;top:${d.y * 100}%;width:${d.w * 100}%;height:${d.h * 100}%"><span>${k + 1}</span></div>`).join('');
  const cards = dets.map((d, k) => { const tx = d.taxon, sci = tx?.scientific_name || d.name || '', name = common(tx?.vernacular) || sci || t('det.unknown');
    return `<section class="card idcard"><div class="sectit">${k + 1} · ${esc(tOr(`rank.${d.taxon_rank}`, d.taxon_rank || ''))}</div>
      <h2>${ICON[d.box_label] || ''} ${esc(name)}${d.taxon_rank !== 'none' ? ` <small class="sub">${fmtProb(d.score)}</small>` : ''}</h2>
      ${sci && name !== sci ? `<div><i>${esc(sci)}</i></div>` : ''}
      ${links(tx, d.taxon_key)}
      ${d.domestic ? `<div class="why">${t('id.domestic')}</div>` : ''}
      ${d.candidates.length ? `<div class="sectit" style="margin-top:12px">${t('cand.title')}</div>` : ''}
      ${d.candidates.map(c => `<div class="cand"><span class="k"></span>
        <span><span class="cn">${candName(c)}</span><small>${esc(c.lineage?.family || '')}${gps ? ` · <span class="dot ${esc(c.plausibility || 'unknown')}"></span>${esc(tOr(`plaus.${c.plausibility || 'unknown'}`, ''))}` : ''}
          ${c.gbif_key ? ` · <a href="https://www.gbif.org/species/${encodeURIComponent(c.gbif_key)}" target="_blank" rel="noopener">GBIF ↗</a>` : ''}</small></span>
        <span class="p${c.p < 0.005 ? ' low' : ''}">${fmtProb(c.p)}</span></div>`).join('')}
    </section>`; }).join('');
  $('#idresult').innerHTML = `<div class="idlayout"><section class="card idphoto"><div class="idframe"><img src="${ID.url}" alt="">${boxes}</div>
      <p class="sub">${gps ? t('id.gps', { lat: fmtCoord(r.lat), lon: fmtCoord(r.lon) }) : t('id.no_gps')}</p>
      ${dets.length ? '' : `<div class="empty">${t('id.none')}</div>`}</section>
    <div class="idcards">${cards}</div></div>`;
}

loadLang().then(drawZone);
