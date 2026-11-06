const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const api = async (path, opt = {}) => {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opt });
  if (!r.ok) {
    const t = await r.text(); console.warn(t);
    let m = t; try { m = JSON.parse(t).detail ?? t; } catch (e) {}
    // Server error with a code: translated (the server's English text as a fallback).
    throw new Error(m?.code ? tOr(`err.${m.code}`, m.message, m) : typeof m === 'string' ? m : JSON.stringify(m));
  }
  return r.json();
};
const fail = e => toast(t('toast.failed', { msg: esc(e.message || e) }));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ic = id => `<svg class="ic"><use href="#i-${id}"/></svg>`;
const ICON = { animal: '🐾', plant: '🌿', fungus: '🍄', image: '🖼️', manual: '✏️' };
// Icon of a subject: the group of its taxon; the box type only if it is not identified.
const subjectIcon = d => d.taxon ? groupOf(d.taxon).icon : ICON[d.box_label] || '';

/* ============================== langue ============================== */
/* A single language for the interface, the species names and Wikipedia (preference of this browser).
   Interface texts: static/i18n/<language>.json, French being the reference and the fallback.
   Plurals: key_one, key_other (and _zero, _two, _few, _many depending on the language), chosen by Intl.PluralRules.
   Dates, numbers and percentages: Intl, in the chosen language. */
let LANG = 'fr', MSG = {}, MSG_FR = {}, PLURAL = new Intl.PluralRules('fr');
const PLURAL_FR = new Intl.PluralRules('fr');
try { LANG = localStorage.getItem('names-lang') || (navigator.language || 'fr').slice(0, 2); } catch (e) {}
const fmtN = n => Number(n).toLocaleString(LANG, { useGrouping: 'min2', maximumFractionDigits: 1 });
const fmtPct = (x, d = 0) => Number(x).toLocaleString(LANG, { style: 'percent', maximumFractionDigits: d });
// Probability from a model: below 0.5 % it reads "< 1 %" (a "0 %" would suggest certainty).
const fmtProb = x => x < 0.005 ? `< ${fmtPct(0.01)}` : fmtPct(x);
const fmtDate = (d, o = { day: 'numeric', month: 'long', year: 'numeric' }) => new Date(d).toLocaleDateString(LANG, o);
function msgFor(key, n) {
  if (typeof n === 'number') {   // plural form if the message has one, otherwise the single form
    const m = MSG[`${key}_${PLURAL.select(n)}`] ?? MSG[`${key}_other`] ?? MSG_FR[`${key}_${PLURAL_FR.select(n)}`] ?? MSG_FR[`${key}_other`];
    if (m != null) return m;
  }
  return MSG[key] ?? MSG_FR[key];
}
/* Translated message. Numeric values are formatted; the others are inserted as is
   (to be escaped by the caller if they come from the data). */
function t(key, p = {}) { return tOr(key, key, p); }
function tOr(key, fallback, p = {}) {
  const m = msgFor(key, p.n) ?? fallback ?? key;
  return m.replace(/\{(\w+)\}/g, (_, k) => k in p ? (typeof p[k] === 'number' ? fmtN(p[k]) : p[k]) : `{${k}}`);
}
const rankName = r => tOr(`rank.${r}`, r || '');
const tr = (...a) => t(...a);   // same function, for blocks where `t` stands for a taxon
const iucnName = k => t(`iucn.${k}`);
// Reason for no auto-confirmation returned by the server ({code, values}).
const whyText = r => typeof r === 'string' ? esc(r)
  : tr(`why.${r.code}`, { conf: fmtPct(r.conf ?? 0), thr: fmtPct(r.thr ?? 0), p: fmtPct(r.p ?? 0),
                          group: r.group ? esc(tOr(`group.${r.group}`, r.group)) : '' });
async function loadLang(l) {
  const get = async lang => { try { const r = await fetch(`static/i18n/${lang}.json`); return r.ok ? r.json() : {}; } catch (e) { return {}; } };
  if (!Object.keys(MSG_FR).length) MSG_FR = await get('fr');
  MSG = l === 'fr' ? MSG_FR : await get(l);
  LANG = l; PLURAL = new Intl.PluralRules(l);
  document.documentElement.lang = l;
  applyStatic();
  $('#b-export').href = `/api/export.csv?lang=${encodeURIComponent(l)}`;   // common names in the chosen language
}
/* Texts of the fixed HTML: data-i18n (content), data-i18n-title, data-i18n-ph (placeholder). */
function applyStatic() {
  $$('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n); });
  $$('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  $$('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  $$('[data-i18n-aria]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
}
const LANG_NAMES = {};
const langName = (l, inLang = LANG) => {   // name of a language, in the interface language (or in its own)
  try { const k = `${inLang}:${l}`; LANG_NAMES[k] ??= new Intl.DisplayNames([inLang], { type: 'language' }).of(l);
        const n = LANG_NAMES[k]; return n && n !== l ? n[0].toLocaleUpperCase(inLang) + n.slice(1) : l; } catch (e) { return l; }
};
const hasUI = l => l === 'fr' || (STATE.status?.ui_langs || []).includes(l);

const common = t => t?.vernacular?.[LANG] || t?.vernacular?.en || t?.vernacular?.fr || '';
const taxName = t => !t ? '?' : (common(t) ? `${esc(common(t))} <i>${esc(t.scientific_name)}</i>` : `<i>${esc(t.scientific_name)}</i>`);
/* English names of orders and families often in "Title Case" (Hawks, Eagles, Kites, And Allies): set back to
   sentence case. Only in English (German capitalises nouns) and if all the words have a capital. */
const tidyCase = s => !s || LANG !== 'en' || !/^(\p{Lu}[^\s]*[\s,-]+)+\p{Lu}\S*$/u.test(s) ? s
  : s[0] + s.slice(1).toLocaleLowerCase('en');
const higherCommon = name => tidyCase(SP.higher?.[name]?.vernacular?.[LANG] || '');
/* Name of a higher rank, as for species: common name as the main one, scientific name as the secondary one
   (alone if there is no common name). */
const higherNames = name => { const com = higherCommon(name);
  return com && com.toLowerCase() !== name.toLowerCase() ? { main: com, sci: name } : { main: name, sci: '' }; };
const crumbLabel = name => { const { main, sci } = higherNames(name); return `${esc(main)}${sci ? ` <small>${esc(sci)}</small>` : ''}`; };
const shortName = tx => tx ? (common(tx) || tx.scientific_name) : t('det.unknown');
const hasWiki = t => t?.wikipedia && Object.keys(t.wikipedia).length;
/* Names in the other languages: folded (one clickable line), unfolded when needed. */
function otherNames(tx) {
  const items = Object.entries(tx?.vernacular || {}).filter(([l]) => l !== LANG).map(([l, n]) => `<code title="${esc(langName(l))}">${esc(l)}</code><b>${esc(n)}</b>`);
  return items.length ? `<details class="names"><summary>${t('names.other', { n: items.length })}</summary>${items.join(' · ')}</details>` : '';
}

/* ============================== state & navigation ============================== */
const STATE = { view: 'photos', status: null };
const srcTitle = id => id === 'local' ? t('local.title') : t(`src.${id}`);   // name of the photo source
async function loadStatus() {
  const s = STATE.status = await api('/api/status');
  const pending = (await api('/api/photos/counts')).pending || 0;   // photos to review (like the filter)
  $('#nb-pending').hidden = !pending; $('#nb-pending').textContent = pending;
  if (!s.immich_configured) {
    $('#server').innerHTML = `<div><span class="dot bad"></span><b>Immich</b></div><div><a href="#settings">${t('server.not_configured')}</a></div>`;
    return;
  }
  const level = !s.writable ? 'bad' : (s.dry_run || s.warnings.length) ? 'warn' : '';
  const name = s.source === 'immich' ? `Immich ${esc(s.immich_version || '?')}` : `${srcTitle(s.source)}</b> <code>${esc(s.source_path)}</code><b>`;
  $('#server').onclick = ev => { if (ev.target.closest('[data-settab]')) { const a = ev.target.closest('[data-settab]').dataset; openSetTab(a.settab, a.anchor); } };
  $('#server').innerHTML = `<div><span class="dot ${level}"></span><b>${name}</b></div>
    <div>${s.writable ? t('server.compatible') : t('server.readonly')} · ${s.dry_run ? t('server.dry_run') : s.writes_paused ? `<a href="#settings" data-settab="source">${t('server.paused')}</a>` : t('server.writing')}</div>
    <div>${s.manual ? `<a href="#settings">${t('server.manual')}</a>` : `<a href="#photos?filter=all">${t('server.analyzed', { done: s.analyzed, n: s.assets })}</a>`}</div>
    ${s.skipped ? `<div><a href="#settings">${t('server.skipped', { n: s.skipped })}</a></div>` : ''}
    ${s.failed ? `<div><a href="#photos?filter=failed">${t('server.failed', { n: s.failed })}</a></div>` : ''}
    ${s.missing ? `<div title="${esc(t('server.missing_hint'))}">${t('server.missing', { n: s.missing })}</div>` : ''}
    ${s.warnings.length ? `<div title="${esc(s.warnings.join('\n'))}"><a href="#settings" data-settab="source" data-anchor="immcard">${t('server.warnings', { n: s.warnings.length })}</a></div>` : ''}`;
}
/* Language change: interface, species names and articles; the current page is redrawn. */
async function setLang(l) {
  try { localStorage.setItem('names-lang', l); } catch (err) {}
  await loadLang(l);
  W.lang = LANG; SPP.wlang = LANG; IDP.wlang = LANG;
  drawBack(); loadStatus(); pollSync(false);
  if (V.open) drawViewer();
}
/* Synchronisation: new photos from Immich, analysis, tags, local copies of records, range maps and articles.
   Runs on the server; the sidebar shows the progress. */
let syncPoll = null;
function toast(msg) {
  let t = $('#toast'); if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.innerHTML = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 6000);
}
/* Current step; with a known total (analysis, tag writing, folder scan): bar and remaining time (measured on
   what is already done); otherwise a counter (photos received from Immich). Tag writing counts in units (read,
   tags, description: st.scale per photo) and shows photos. */
function syncProgress(st) {
  const done = Math.min(st.done, st.total), scale = st.scale || 1, files = STATE.status?.source && STATE.status.source !== 'immich';
  // Synchronisation: in dry-run nothing is written (simulation); "folder" source: XMP files, not Immich.
  const step = st.step === 'sync' ? (STATE.status?.dry_run ? 'sync_dry' : files ? 'sync_xmp' : 'sync')
    : ['scan', 'reconcile'].includes(st.step) && files ? `${st.step}_files` : st.step;
  const count = st.total ? ` ${fmtN(Math.floor(done / scale))}/${fmtN(Math.round(st.total / scale))}`
    : st.done ? ` · ${t('count.photos', { n: st.done })}` : '';
  let h = `<b>${t('sync.running')}</b><br>`;
  // Remaining time from what is already done since start (seconds), as "about N min" or "N h N min".
  const eta = (start, d, n) => {
    if (d <= 3 || !start) return '';
    const left = (Date.now() / 1000 - start) / (d - 1) * (n - d + 1) / 60;
    const unit = (x, u) => new Intl.NumberFormat(LANG, { style: 'unit', unit: u, unitDisplay: 'short' }).format(x);
    return t('sync.eta', { time: left < 60 ? unit(Math.max(1, Math.round(left)), 'minute')
      : `${unit(Math.floor(left / 60), 'hour')} ${unit(Math.round(left % 60), 'minute')}` });
  };
  // Analysis in two phases running at the same time: one bar for the AI part (GPU), one for the geographic check
  // (GBIF), each with its remaining time; a finished or waiting phase is greyed.
  const ph = st.phases && ['analyze', 'geo'].includes(st.step) ? st.phases : null;
  if (ph) {
    const active = st.active || [st.step];
    // Third bar while tags are written during the analysis: same wording as the tag writing step.
    const sync = STATE.status?.dry_run ? 'sync_dry' : files ? 'sync_xmp' : 'sync';
    const label = k => k === 'tags' ? (s => s.charAt(0).toUpperCase() + s.slice(1))(t(`step.${sync}`)) : t(`sync.phase_${k}`);
    h += ['analyze', 'geo', 'tags'].filter(k => k !== 'tags' || ph.tags?.[1]).map(k => {
      const [d, n, start] = ph[k] || [0, 0], v = Math.min(d, n), on = k === 'tags' ? v < n : active.includes(k);
      return `<div class="phase${on ? ' on' : ''}">${label(k)} ${fmtN(v)}/${fmtN(n)}
        <div class="syncbar"><i style="width:${n ? (100 * v / n).toFixed(1) : 0}%"></i></div>${on && v < n ? `<span class="sub">${eta(start, v, n)}</span>` : ''}</div>`; }).join('');
    if (st.current) h += `<span class="syncfile" title="${esc(st.current)}">${esc(st.current)}</span>`;
    return h;
  }
  h += `${esc(step ? tOr(`step.${step}`, step) : '')}${count}`;
  if (!st.total) return h;
  h += `<div class="syncbar"><i style="width:${(100 * Math.max(done - 1, 0) / st.total).toFixed(1)}%"></i></div>`;
  if (st.current) h += `<span class="syncfile" title="${esc(st.current)}">${esc(st.current)}</span>`;
  h += eta(st.step_started, done, st.total);
  return h;
}
async function pollSync(announce) {
  let st;
  try { st = await api('/api/sync/status'); }
  catch (e) {   // server temporarily unreachable (restart): try again, otherwise the tracking would stop there
    clearTimeout(syncPoll); syncPoll = setTimeout(() => pollSync(announce), 20000); return;
  }
  $('#b-sync').classList.toggle('spin', st.running);
  showPause(st.analysis_paused);
  const bar = $('#topprog');   // thin bar under the top bar: analysis progress, otherwise an animation
  bar.hidden = !st.running; bar.classList.toggle('indet', st.running && !st.total);
  bar.firstChild.style.width = st.running && st.total ? `${(100 * Math.max(Math.min(st.done, st.total) - 1, 0) / st.total).toFixed(1)}%` : '';
  $('#syncline').innerHTML = st.running ? syncProgress(st)
    : (st.last_run ? t('sync.last', { when: new Date(st.last_run.at * 1000).toLocaleString(LANG, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) }) : '');
  if (!st.running && st.analysis_paused) $('#syncline').insertAdjacentHTML('afterbegin', `<b>⏸ ${t('sync.paused')}</b><br>`);
  if (!st.running && st.error) $('#syncline').insertAdjacentHTML('beforeend', ` <span title="${esc(t('sync.failed', { msg: st.error }))}">⚠</span>`);
  clearTimeout(syncPoll);
  if (st.running) { syncPoll = setTimeout(() => pollSync(true), 1500); return; }
  syncPoll = setTimeout(() => pollSync(false), 20000);   // spots a cycle started by the automatic loop
  if (announce && st.error) {   // failed cycle: the figures of last_run would be those of the previous cycle
    toast(t('sync.failed', { msg: esc(st.error) }));
    loadStatus(); if (!V.open) reloadPage();
  } else if (announce && st.last_run) {
    const r = st.last_run, sy = r.sync || {};
    toast(t('sync.done', { new: t('sync.new_photos', { n: r.scanned || 0 }), analyzed: t('sync.analyzed', { n: r.analyzed || 0 }),
      tags: t('sync.tags', { n: sy.tags_added || 0 }) + (r.dry_run ? ` (${t('sync.dry_run')})` : ''), cached: t('sync.cached', { n: r.prefetched || 0 }) }) + (sy.failed ? ` ${t('sync.failed_photos', { n: sy.failed })}` : ''));
    loadStatus(); if (!V.open) reloadPage();
  }
}
/* Pause and resume of the AI analysis (it takes the whole machine): kept from one launch to the next.
   Resuming starts the analysis right away. */
function showPause(paused) {
  const b = $('#b-pause'); if (paused == null) return;
  b.hidden = false; b.dataset.paused = paused ? '1' : '';
  b.setAttribute('aria-pressed', paused); b.classList.toggle('on', paused);
  b.title = t(paused ? 'top.resume' : 'top.pause'); b.setAttribute('aria-label', b.title);
  b.querySelector('use').setAttribute('href', paused ? '#i-play' : '#i-pause');
}
$('#b-pause').onclick = async () => {
  const paused = !$('#b-pause').dataset.paused;
  try { const r = await api('/api/analysis/pause', { method: 'POST', body: JSON.stringify({ paused }) });
    showPause(r.paused); toast(t(r.paused ? 'sync.paused_toast' : 'sync.resumed_toast')); }
  catch (e) { toast(t('set.refused', { msg: esc(e.message) })); }
  setTimeout(() => pollSync(true), 500);
};
$('#b-sync').onclick = async () => {
  const r = await api('/api/sync', { method: 'POST' });
  if (!r.started) toast(t('sync.already'));
  pollSync(true);
};
/* Clickable tiles (photos, species): reached with Tab, opened with Enter or Space. In the capture phase and
   stopped there: otherwise the same Enter key would immediately confirm the detection in the viewer it opens. */
document.addEventListener('keydown', e => {
  if ((e.key !== 'Enter' && e.key !== ' ') || !e.target.matches?.('[role=button][tabindex]:not(button)')) return;
  if (typeof V !== 'undefined' && V.open) return;
  e.preventDefault(); e.stopImmediatePropagation();
  e.target.click();
}, true);
/* Zoom from a wheel event, proportional to the distance: ×1.1 per notch (100 px). A touchpad sends many
   small events, counting each one as a notch would make the zoom jump. Pinch (ctrl): very small distances. */
function wheelFactor(e) {
  const px = e.deltaY * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1) * (e.ctrlKey ? 8 : 1);
  return Math.pow(1.1, -Math.max(-120, Math.min(120, px)) / 100);
}
