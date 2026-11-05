const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const api = async (path, opt = {}) => {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opt });
  if (!r.ok) {
    const t = await r.text(); console.warn(t);
    let m = t; try { m = JSON.parse(t).detail ?? t; } catch (e) {}
    // Erreur du serveur avec un code : traduite (texte français du serveur en repli).
    throw new Error(m?.code ? tOr(`err.${m.code}`, m.message, m) : typeof m === 'string' ? m : JSON.stringify(m));
  }
  return r.json();
};
const fail = e => toast(t('toast.failed', { msg: esc(e.message || e) }));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ic = id => `<svg class="ic"><use href="#i-${id}"/></svg>`;
const ICON = { animal: '🐾', plant: '🌿', fungus: '🍄', image: '🖼️', manual: '✏️' };
// Icône d'un sujet : le groupe de son taxon ; le type de boîte seulement s'il n'est pas identifié.
const subjectIcon = d => d.taxon ? groupOf(d.taxon).icon : ICON[d.box_label] || '';

/* ============================== langue ============================== */
/* Une seule langue pour l'interface, les noms d'espèces et Wikipédia (préférence de ce navigateur).
   Textes de l'interface : static/i18n/<langue>.json, le français servant de référence et de repli.
   Pluriels : clé_one, clé_other (et _zero, _two, _few, _many selon la langue), choisis par Intl.PluralRules.
   Dates, nombres et pourcentages : Intl, dans la langue choisie. */
let LANG = 'fr', MSG = {}, MSG_FR = {}, PLURAL = new Intl.PluralRules('fr');
const PLURAL_FR = new Intl.PluralRules('fr');
try { LANG = localStorage.getItem('names-lang') || (navigator.language || 'fr').slice(0, 2); } catch (e) {}
const fmtN = n => Number(n).toLocaleString(LANG, { useGrouping: 'min2', maximumFractionDigits: 1 });
const fmtPct = (x, d = 0) => Number(x).toLocaleString(LANG, { style: 'percent', maximumFractionDigits: d });
// Probabilité d'un modèle : sous 0,5 % on écrit « < 1 % » (un « 0 % » ferait croire à une certitude).
const fmtProb = x => x < 0.005 ? `< ${fmtPct(0.01)}` : fmtPct(x);
const fmtDate = (d, o = { day: 'numeric', month: 'long', year: 'numeric' }) => new Date(d).toLocaleDateString(LANG, o);
function msgFor(key, n) {
  if (typeof n === 'number') {   // forme plurielle si le message en a, sinon forme unique
    const m = MSG[`${key}_${PLURAL.select(n)}`] ?? MSG[`${key}_other`] ?? MSG_FR[`${key}_${PLURAL_FR.select(n)}`] ?? MSG_FR[`${key}_other`];
    if (m != null) return m;
  }
  return MSG[key] ?? MSG_FR[key];
}
/* Message traduit. Les valeurs numériques sont mises en forme ; les autres sont insérées telles quelles
   (à échapper par l'appelant si elles viennent des données). */
function t(key, p = {}) { return tOr(key, key, p); }
function tOr(key, fallback, p = {}) {
  const m = msgFor(key, p.n) ?? fallback ?? key;
  return m.replace(/\{(\w+)\}/g, (_, k) => k in p ? (typeof p[k] === 'number' ? fmtN(p[k]) : p[k]) : `{${k}}`);
}
const rankName = r => tOr(`rank.${r}`, r || '');
const tr = (...a) => t(...a);   // même fonction, pour les blocs où `t` désigne un taxon
const iucnName = k => t(`iucn.${k}`);
// Raison de non-validation automatique renvoyée par le serveur ({code, valeurs}).
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
  $('#b-export').href = `/api/export.csv?lang=${encodeURIComponent(l)}`;   // noms communs dans la langue choisie
}
/* Textes du HTML fixe : data-i18n (contenu), data-i18n-title, data-i18n-ph (placeholder). */
function applyStatic() {
  $$('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n); });
  $$('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  $$('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  $$('[data-i18n-aria]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
}
const LANG_NAMES = {};
const langName = (l, inLang = LANG) => {   // nom d'une langue, dans la langue de l'interface (ou dans la sienne)
  try { const k = `${inLang}:${l}`; LANG_NAMES[k] ??= new Intl.DisplayNames([inLang], { type: 'language' }).of(l);
        const n = LANG_NAMES[k]; return n && n !== l ? n[0].toLocaleUpperCase(inLang) + n.slice(1) : l; } catch (e) { return l; }
};
const hasUI = l => l === 'fr' || (STATE.status?.ui_langs || []).includes(l);

const common = t => t?.vernacular?.[LANG] || t?.vernacular?.en || t?.vernacular?.fr || '';
const taxName = t => !t ? '?' : (common(t) ? `${esc(common(t))} <i>${esc(t.scientific_name)}</i>` : `<i>${esc(t.scientific_name)}</i>`);
/* Noms anglais des ordres et familles souvent en « Title Case » (Hawks, Eagles, Kites, And Allies) : remis en
   casse de phrase. Seulement en anglais (l'allemand met une majuscule aux noms) et si tous les mots en ont une. */
const tidyCase = s => !s || LANG !== 'en' || !/^(\p{Lu}[^\s]*[\s,-]+)+\p{Lu}\S*$/u.test(s) ? s
  : s[0] + s.slice(1).toLocaleLowerCase('en');
const higherCommon = name => tidyCase(SP.higher?.[name]?.vernacular?.[LANG] || '');
/* Nom d'un rang supérieur, comme pour les espèces : nom commun en principal, nom scientifique en secondaire
   (seul s'il n'y a pas de nom commun). */
const higherNames = name => { const com = higherCommon(name);
  return com && com.toLowerCase() !== name.toLowerCase() ? { main: com, sci: name } : { main: name, sci: '' }; };
const crumbLabel = name => { const { main, sci } = higherNames(name); return `${esc(main)}${sci ? ` <small>${esc(sci)}</small>` : ''}`; };
const shortName = tx => tx ? (common(tx) || tx.scientific_name) : t('det.unknown');
const hasWiki = t => t?.wikipedia && Object.keys(t.wikipedia).length;
/* Noms dans les autres langues : repliés (une ligne cliquable), on les déplie au besoin. */
function otherNames(tx) {
  const items = Object.entries(tx?.vernacular || {}).filter(([l]) => l !== LANG).map(([l, n]) => `<code title="${esc(langName(l))}">${esc(l)}</code><b>${esc(n)}</b>`);
  return items.length ? `<details class="names"><summary>${t('names.other', { n: items.length })}</summary>${items.join(' · ')}</details>` : '';
}

/* ============================== état & navigation ============================== */
const STATE = { view: 'photos', status: null };
const srcTitle = id => id === 'local' ? t('local.title') : t(`src.${id}`);   // nom de la source des photos
async function loadStatus() {
  const s = STATE.status = await api('/api/status');
  const pending = (await api('/api/photos/counts')).pending || 0;   // photos à relire (comme le filtre)
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
/* Changement de langue : interface, noms d'espèces et articles ; la page courante est redessinée. */
async function setLang(l) {
  try { localStorage.setItem('names-lang', l); } catch (err) {}
  await loadLang(l);
  W.lang = LANG; SPP.wlang = LANG; IDP.wlang = LANG;
  drawBack(); loadStatus(); pollSync(false);
  if (V.open) drawViewer();
}
/* Synchronisation : nouvelles photos d'Immich, analyse, tags, mise en local des fiches, aires et articles.
   Tourne côté serveur ; la barre latérale affiche la progression. */
let syncPoll = null;
function toast(msg) {
  let t = $('#toast'); if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.innerHTML = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 6000);
}
/* Étape en cours ; pendant l'analyse : barre, photo en cours et temps restant (mesuré sur les photos déjà faites). */
function syncProgress(st) {
  const done = Math.min(st.done, st.total);
  // Synchronisation : en dry-run rien n'est écrit (simulation) ; source « dossier » : fichiers XMP, pas Immich.
  const step = st.step === 'sync' ? (STATE.status?.dry_run ? 'sync_dry' : STATE.status?.source !== 'immich' ? 'sync_xmp' : 'sync') : st.step;
  let h = `<b>${t('sync.running')}</b><br>${esc(step ? tOr(`step.${step}`, step) : '')}${st.total ? ` ${fmtN(done)}/${fmtN(st.total)}` : ''}`;
  if (!st.total) return h;
  h += `<div class="syncbar"><i style="width:${(100 * Math.max(done - 1, 0) / st.total).toFixed(1)}%"></i></div>`;
  if (st.current) h += `<span class="syncfile" title="${esc(st.current)}">${esc(st.current)}</span>`;
  if (done > 3 && st.step_started) {
    const left = (Date.now() / 1000 - st.step_started) / (done - 1) * (st.total - done + 1) / 60;
    const unit = (n, u) => new Intl.NumberFormat(LANG, { style: 'unit', unit: u, unitDisplay: 'short' }).format(n);
    h += t('sync.eta', { time: left < 60 ? unit(Math.max(1, Math.round(left)), 'minute')
      : `${unit(Math.floor(left / 60), 'hour')} ${unit(Math.round(left % 60), 'minute')}` });
  }
  return h;
}
async function pollSync(announce) {
  let st;
  try { st = await api('/api/sync/status'); }
  catch (e) {   // serveur momentanément injoignable (redémarrage) : on réessaie, sinon le suivi s'arrêterait là
    clearTimeout(syncPoll); syncPoll = setTimeout(() => pollSync(announce), 20000); return;
  }
  $('#b-sync').classList.toggle('spin', st.running);
  showPause(st.analysis_paused);
  const bar = $('#topprog');   // fine barre sous la barre du haut : avancement de l'analyse, sinon animation
  bar.hidden = !st.running; bar.classList.toggle('indet', st.running && !st.total);
  bar.firstChild.style.width = st.running && st.total ? `${(100 * Math.max(Math.min(st.done, st.total) - 1, 0) / st.total).toFixed(1)}%` : '';
  $('#syncline').innerHTML = st.running ? syncProgress(st)
    : (st.last_run ? t('sync.last', { when: new Date(st.last_run.at * 1000).toLocaleString(LANG, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) }) : '');
  if (!st.running && st.analysis_paused) $('#syncline').insertAdjacentHTML('afterbegin', `<b>⏸ ${t('sync.paused')}</b><br>`);
  if (!st.running && st.error) $('#syncline').insertAdjacentHTML('beforeend', ` <span title="${esc(t('sync.failed', { msg: st.error }))}">⚠</span>`);
  clearTimeout(syncPoll);
  if (st.running) { syncPoll = setTimeout(() => pollSync(true), 1500); return; }
  syncPoll = setTimeout(() => pollSync(false), 20000);   // repère un cycle lancé par la boucle automatique
  if (announce && st.error) {   // cycle en échec : les chiffres de last_run seraient ceux du cycle précédent
    toast(t('sync.failed', { msg: esc(st.error) }));
    loadStatus(); if (!V.open) reloadPage();
  } else if (announce && st.last_run) {
    const r = st.last_run, sy = r.sync || {};
    toast(t('sync.done', { new: t('sync.new_photos', { n: r.scanned || 0 }), analyzed: t('sync.analyzed', { n: r.analyzed || 0 }),
      tags: t('sync.tags', { n: sy.tags_added || 0 }) + (r.dry_run ? ` (${t('sync.dry_run')})` : ''), cached: t('sync.cached', { n: r.prefetched || 0 }) }) + (sy.failed ? ` ${t('sync.failed_photos', { n: sy.failed })}` : ''));
    loadStatus(); if (!V.open) reloadPage();
  }
}
/* Pause et reprise de l'analyse par l'IA (elle occupe toute la machine) : gardée d'un lancement à l'autre. La
   reprise lance l'analyse aussitôt. */
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
/* Tuiles cliquables (photos, espèces) : atteintes avec Tab, ouvertes avec Entrée ou Espace. En capture et
   arrêtée là : sinon la même touche Entrée validerait aussitôt la détection dans la visionneuse qu'elle ouvre. */
document.addEventListener('keydown', e => {
  if ((e.key !== 'Enter' && e.key !== ' ') || !e.target.matches?.('[role=button][tabindex]:not(button)')) return;
  if (typeof V !== 'undefined' && V.open) return;
  e.preventDefault(); e.stopImmediatePropagation();
  e.target.click();
}, true);
/* Zoom d'un événement molette, proportionnel à la distance : ×1,1 par cran (100 px). Un pavé tactile envoie beaucoup
   de petits événements, compter chacun comme un cran ferait bondir le zoom. Pincement (ctrl) : très petites distances. */
function wheelFactor(e) {
  const px = e.deltaY * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1) * (e.ctrlKey ? 8 : 1);
  return Math.pow(1.1, -Math.max(-120, Math.min(120, px)) / 100);
}
