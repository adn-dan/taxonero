/* Settings page: settings saved in the database, models and hardware used. */
const SET_ROWS = [   // labels: set.<key>, set.<key>.sub, set.<key>.<option>, set.group.<group>
  { group: 'detection' },
  { k: 'detection', type: 'seg', opts: ['auto', 'manual'] },
  { k: 'detect_groups', type: 'multi', opts: ['animal', 'plant', 'fungus', 'microbe'], when: v => v.detection !== 'manual' },
  { group: 'compute' },
  { k: 'device', type: 'seg', opts: ['auto', 'cpu'], norm: v => v === 'cpu' ? 'cpu' : 'auto', extra: () => deviceNote() },
  { k: 'gpu_batch', type: 'int', min: 0, max: 64, unit: 'photos', when: () => MODELS?.hardware?.device !== 'cpu' },   // 0: automatic
  { k: 'models_idle_minutes', type: 'int', min: 0, max: 1440, unit: 'min', when: () => MODELS?.hardware?.device !== 'cpu' },
  { k: 'open_detector_mode', type: 'seg', opts: ['auto', 'always', 'fallback'], extra: () => openNote(), when: () => MODELS?.open_detector !== false },
  { group: 'identification' },
  { k: 'exclude_extinct', type: 'bool' },
  { k: 'hide_absent', type: 'bool' },
  { k: 'analyze_videos', type: 'bool' },
  { k: 'skip_people', type: 'bool' },
  { k: 'skip_raw_twins', type: 'bool' },
  { k: 'full_image_fallback', type: 'bool' },
  { k: 'domestic_animals', type: 'seg', opts: ['skip', 'keep'] },
  { k: 'sex_detection', type: 'bool' },
  { k: 'stage_detection', type: 'bool' },
  { group: 'auto' },
  { k: 'auto_confirm', type: 'seg', opts: ['rule', 'calibrated', 'fixed', 'off'], extra: () => groupsNote() },
  { k: 'auto_confirm_threshold', type: 'range', min: .5, max: .99, step: .01, when: v => v.auto_confirm === 'rule' || v.auto_confirm === 'fixed' },
  { k: 'auto_confirm_max_runner_up', type: 'range', min: 0, max: .3, step: .01, when: v => v.auto_confirm === 'rule' },
  { k: 'auto_confirm_audit', type: 'range', min: 0, max: .5, step: .01, when: v => v.auto_confirm !== 'off' },
  { group: 'immich' },
  { k: 'write_description', type: 'bool' },
  { k: 'analysis_window', type: 'window' },
  { k: 'interval_minutes', type: 'int', min: 1, max: 1440, unit: 'min' },
  // Server edition only (the desktop window is local): a page open to anyone, to identify one image.
  { group: 'public', edition: 'server' },
  { k: 'public_identify', type: 'bool', edition: 'server', extra: () => publicNote() },
];
/* Address of the public identification page, to share, once it is on. */
function publicNote() {
  if (!SETS?.values?.public_identify) return '';
  const url = `${location.origin}/identify`;
  return `<div class="publink">${t('set.public_identify.link', { url: `<a href="/identify" target="_blank" rel="noopener"><code>${esc(url)}</code></a>` })}</div>`;
}
let SETS = null, MODELS = null, IMM = null, SRC = null;
/* Tabs of the page: SET_ROWS group -> tab; the open tab is remembered by the browser. */
const TABS = ['source', 'analysis', 'validation', 'models', 'general', 'logs'];
const TAB_LABEL = { source: 'src.title', analysis: 'set.tab.analysis', validation: 'set.group.auto', models: 'models.title', general: 'set.tab.general', logs: 'logs.title' };
const SET_TAB = { detection: 'analysis', compute: 'analysis', identification: 'analysis', auto: 'validation', immich: 'source', public: 'general' };
let setTab = (() => { try { return localStorage.getItem('settings-tab'); } catch { return null; } })();
function showSetTab(k, focus) {
  setTab = k;
  try { localStorage.setItem('settings-tab', k); } catch {}
  $$('#settings [role=tab]').forEach(b => { const on = b.dataset.tab === k; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; if (on && focus) { b.focus(); b.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } });
  $$('#settings [role=tabpanel]').forEach(p => p.hidden = p.id !== `setpanel-${k}`);
  if (k === 'logs') pollLogs();
}
/* Log tab: lines kept in memory by the server. Polled every 5 s while the tab is shown. */
const LOG_LEVELS = { all: () => true, warn: l => l !== 'INFO', err: l => l === 'ERROR' || l === 'CRITICAL' };
let LOGS = [], logLevel = 'all', logTimer = null, logCapacity = 3000, logFloor = 0;   // logFloor: last cleared line
const logShown = () => LOGS.filter(e => LOG_LEVELS[logLevel](e.level));
function logLine(e) {
  const d = new Date(e.t * 1000), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} ${e.level} ${e.name}: ${e.msg}`;
}
function renderLogs() {
  const box = $('#logbox'); if (!box) return;
  const bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;   // do not move someone scrolling back up to read
  const rows = logShown();
  box.innerHTML = rows.length ? rows.map(e => `<div class="logline${e.level === 'INFO' ? '' : ` lv-${e.level.toLowerCase()}`}">${esc(logLine(e))}</div>`).join('')
    : `<div class="sub">${t('logs.empty')}</div>`;
  if (bottom) box.scrollTop = box.scrollHeight;
}
function pollLogs() {
  clearTimeout(logTimer);
  const box = $('#logbox');
  if (!box || !box.offsetParent) return;   // tab or page left: no more polling
  const after = LOGS.at(-1)?.id || logFloor;
  api(`/api/logs?after=${after}`).then(r => {
    logCapacity = r.capacity;
    r.entries = r.entries.filter(e => e.id > logFloor);   // poll sent before a "Clear"
    if (r.entries.length) { LOGS = LOGS.concat(r.entries);
      let over = LOGS.length - logCapacity;   // page left open: information lines go first
      if (over > 0) LOGS = LOGS.filter(e => !(e.level === 'INFO' && over-- > 0));
      renderLogs(); }
    else if (!LOGS.length) renderLogs();
  }).catch(() => {}).finally(() => { logTimer = setTimeout(pollLogs, 5000); });
}
function logsCard() {
  return `<section class="card"><h2>${t('logs.title')}</h2><div class="sub" style="font-size:13px">${t('logs.sub')}</div>
    <div class="logbar"><div class="seg" id="log-level">${Object.keys(LOG_LEVELS).map(k => `<button data-o="${k}" class="${k === logLevel ? 'on' : ''}" aria-pressed="${k === logLevel}">${t(`logs.level.${k}`)}</button>`).join('')}</div>
    <button class="btn" id="log-copy">${t('logs.copy')}</button><button class="btn" id="log-clear">${t('logs.clear')}</button></div>
    <div id="logbox" class="logbox" tabindex="0" role="log" aria-label="${esc(t('logs.title'))}"></div></section>`;
}
function bindLogs() {
  $('#log-level').onclick = ev => { const b = ev.target.closest('button'); if (!b) return; logLevel = b.dataset.o;
    $$('#log-level button').forEach(x => { const on = x === b; x.classList.toggle('on', on); x.setAttribute('aria-pressed', on); });
    const box = $('#logbox'); renderLogs(); box.scrollTop = box.scrollHeight; };
  $('#log-copy').onclick = () => copyText(logShown().map(logLine).join('\n'), t('logs.copied'));
  $('#log-clear').onclick = async () => {
    if (!confirm(t('logs.clear_ask'))) return;
    try { const r = await api('/api/logs', { method: 'DELETE' });
      logFloor = r.last_id; LOGS = []; renderLogs(); toast(t('logs.cleared')); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); }
  };
  renderLogs();
  if (!$('#setpanel-logs').hidden) pollLogs();
}
async function copyText(text, done) {
  try { await navigator.clipboard.writeText(text); }
  catch (err) {   // clipboard unavailable outside HTTPS: copy through a temporary text area
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  toast(done);
}
/* Link to a given tab: changing the address does nothing if the Settings page is already open. */
let setAnchor = null;   // element to show once the page is drawn ("N warnings" link)
function openSetTab(k, anchor) {
  setTab = k; setAnchor = anchor || null;
  try { localStorage.setItem('settings-tab', k); } catch {}
  if (STATE.view === 'settings' && $('#settings [role=tab]')) { showSetTab(k); showSetAnchor() || $('#settings').scrollIntoView({ block: 'start' }); }
}
function showSetAnchor() {
  const el = setAnchor && $(`#${setAnchor}`); setAnchor = null;
  if (!el || !el.offsetParent) return false;
  el.scrollIntoView({ block: 'center' }); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
  return true;
}
/* Removal of what the tool wrote to Immich: what will be touched, then a typed confirmation. */
let RETRACT = null, undoTimer = null;
function undoCard() {
  const u = RETRACT; if (!u || u.source !== 'immich') return '';
  const any = u.photos || u.tags, run = u.state === 'running';
  let h = `<div class="sub" style="font-size:13px">${t('undo.sub')}</div>`;
  if (run) h += `<div class="immst warn">${t('undo.running', { done: u.done, total: u.total })}</div>`;
  if (u.state === 'done') h += `<div class="immst ${u.remaining ? 'warn' : 'ok'}">${t('undo.done', { tags: u.stats.tags_removed, descs: u.stats.descriptions, deleted: u.stats.tags_deleted })}${u.remaining ? ` ${t('undo.remaining', { n: u.remaining })}` : ''}</div>`;
  if (u.state === 'interrupted') h += `<div class="immst warn">${t('undo.interrupted')}</div>`;
  if (u.state === 'failed') h += `<div class="immst bad">${t('undo.failed', { msg: esc(u.error || '') })}</div>`;
  if (!run && any) h += `<div class="undocounts">${t('undo.counts', { photos: u.photos, tags: u.tags, descs: u.descriptions })}</div>`;
  if (!run && !any && u.state !== 'done') h += `<div class="sub">${t('undo.none')}</div>`;
  if (!run && any) h += u.dry_run ? `<div class="warnline">⚠️ ${t('undo.dry')}</div>`
    : `<div class="warnline">⚠️ ${t('undo.warn')}</div><label class="undoconfirm">${t('undo.type', { word: `<b>${esc(t('undo.word'))}</b>` })}
        <input id="undo-word" autocomplete="off" spellcheck="false"></label>
        <button class="btn danger" id="undo-go" disabled>${t('undo.start')}</button>`;
  if (u.paused && !run) h += `<div class="undopaused"><div class="immst warn">${t('undo.paused')}</div><div class="sub">${t('undo.resume_hint')}</div>
      <button class="btn" id="undo-resume">${t('undo.resume')}</button></div>`;
  return `<section class="card" id="undocard"><h2>${t('undo.title')}</h2>${h}</section>`;
}
function drawRetract() {
  const el = $('#undocard'); if (!el) return;
  el.outerHTML = undoCard(); bindUndo();
}
function bindUndo() {
  clearTimeout(undoTimer);
  if (RETRACT?.state === 'running') undoTimer = setTimeout(async () => {
    if (!$('#undocard')) return;
    RETRACT = await api('/api/immich/uninstall').catch(() => RETRACT);
    drawRetract(); if (RETRACT.state !== 'running') loadStatus();
  }, 2000);
  const word = $('#undo-word'), go = $('#undo-go');
  if (word) word.oninput = () => { go.disabled = word.value.trim() !== t('undo.word'); };
  if (go) go.onclick = async () => {
    go.disabled = true;
    try { RETRACT = await api('/api/immich/uninstall', { method: 'POST', body: JSON.stringify({ confirm: 'remove-from-immich' }) }); loadStatus(); drawRetract(); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); word.value = ''; }
  };
  const res = $('#undo-resume');
  if (res) res.onclick = async () => {
    try { RETRACT = await api('/api/immich/resume-writes', { method: 'POST' }); toast(t('undo.resumed')); loadStatus(); drawRetract(); }
    catch (e) { toast(esc(e.message)); }
  };
}
async function loadSettings() {
  RETRACT = await api('/api/immich/uninstall').catch(() => null);
  [SETS, MODELS, IMM, SRC] = await Promise.all([api('/api/settings'), api('/api/models').catch(() => null), api('/api/immich').catch(() => null),
    api('/api/sources').catch(() => null)]);
  renderSettings();
}
/* Immich connection: URL and API key, tested before being saved. The key is never sent back by
   the server (only its last characters). Values set in .env cannot be changed here. */
function immichState(st) {
  if (!st || st.code === 'missing') return `<div class="immst">${t('imm.state.missing')}</div>`;
  const v = esc(st.version || '?');
  let h = st.code === 'ok' ? `<div class="immst ok">✓ ${t('imm.state.ok', { v })}</div>`
    : st.code === 'readonly' ? `<div class="immst warn">⚠️ ${t('imm.state.readonly', { v })}</div>`
    : `<div class="immst bad">✗ ${tOr(`imm.state.${st.code}`, tOr(`err.${st.code}`, st.code))}</div>`;
  if (st.missing?.length) h += `<div class="sub">${t('imm.missing', { perms: st.missing.map(p => `<code>${esc(p)}</code>`).join(' ') })}</div>`;
  if (st.extra?.length) h += `<div class="warnline">⚠️ ${t('imm.extra', { perms: st.extra.map(p => `<code>${esc(p)}</code>`).join(' ') })}</div>`;
  const other = (st.errors || []).filter(e => !/permission/.test(e)).concat((st.warnings || []).filter(w => !/API key/.test(w)));
  if (other.length) h += other.map(e => `<div class="warnline">⚠️ ${esc(e)}</div>`).join('');
  return h;
}
function permChips(m, st) {
  const have = st?.permissions ? new Set(st.permissions) : null;
  const chip = (p, opt) => { const on = have && (have.has(p) || have.has('all'));
    return `<code class="perm${have ? (on ? ' on' : opt ? '' : ' off') : ''}">${have ? (on ? '✓ ' : opt ? '' : '✗ ') : ''}${esc(p)}</code>`; };
  return m.required.map(p => chip(p, false)).join(' ');
}
/* Photo source (Immich, a folder or a photo application) and, on the desktop, result writing. Set by
   the environment (SOURCE, PHOTOS_DIR, CATALOG_PATH): shown without being changeable. A folder or a
   library is given before changing source ("Use" button). */
const srcPick = m => m.options.find(o => o.id === (SRC.pick || m.current)) || m.options[0];
function sourceCard(m) {
  if (!m) return '';
  const o = srcPick(m), lock = m.locked ? ' disabled' : '';
  const browse = STATE.status?.can_pick;   // desktop: native dialog, through the server
  const opts = m.options.filter(x => x.available || x.id === m.current);
  return `<section class="card" id="srccard"><h2>${t('src.title')}</h2><div class="sub" style="font-size:13px">${t('src.sub')}</div>
    ${STATE.status?.models_on_disk === false ? `<div class="sub" style="font-size:13px;margin-top:6px">${t('src.models_note')}</div>` : ''}
    <div class="setrow"><div class="lbl"><b>${t('src.kind')}</b>${m.locked ? `<div class="sub">${t('src.locked', { env: '<code>SOURCE</code>' })}</div>` : ''}</div>
      <div class="ctl"><select id="src-kind" class="pill" aria-label="${esc(t('src.kind'))}"${lock}>${opts.map(x => `<option value="${x.id}"${x.id === o.id ? ' selected' : ''}>${esc(t(`src.${x.id}`))}</option>`).join('')}</select></div></div>
    ${o.kind === 'server' ? '' : `<div class="setrow"><div class="lbl"><b>${t(`src.${o.kind}`)}</b><div class="sub">${t(`src.${o.kind}_sub`)}</div></div>
      <div class="ctl srcpath"><input id="src-dir" aria-label="${esc(t(`src.${o.kind}`))}" spellcheck="false" value="${esc(o.path)}"${lock}>
        ${!m.locked && browse ? `<button class="btn" id="src-browse">${t('src.browse')}</button>` : ''}
        ${!m.locked ? `<button class="btn primary" id="src-use">${t('src.use')}</button>` : ''}</div></div>`}
    ${m.dry_run_editable ? `<div class="setrow"><div class="lbl"><b>${t('src.write')}</b><div class="sub">${t('src.write_sub')}</div></div>
      <div class="ctl"><label class="switch"><input type="checkbox" id="src-write"${m.dry_run ? '' : ' checked'}><span></span></label></div></div>` : ''}
  </section>`;
}
function bindSource() {
  if (!$('#srccard')) return;
  const put = async body => {
    try { SRC = await api('/api/source', { method: 'PUT', body: JSON.stringify(body) }); toast(t('src.saved')); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); return; }
    IMM = await api('/api/immich').catch(() => IMM); renderSettings(); loadStatus();
  };
  $('#src-kind').onchange = e => {
    const o = SRC.options.find(x => x.id === e.target.value);
    if (o.kind !== 'server') { SRC.pick = o.id; renderSettings(); return; }   // folder or library first
    SRC.pick = null; if (o.id !== SRC.current) put({ source: o.id });
  };
  $('#src-use') && ($('#src-use').onclick = () => put({ source: srcPick(SRC).id, path: $('#src-dir').value.trim() }));
  $('#src-browse') && ($('#src-browse').onclick = async () => {
    const o = srcPick(SRC);
    const r = await api('/api/desktop/pick', { method: 'POST', body: JSON.stringify({ kind: o.kind === 'folder' ? 'folder' : 'file',
      suffix: o.suffix || '', start: $('#src-dir').value.trim() }) }).catch(e => { toast(t('set.refused', { msg: esc(e.message) })); return {}; });
    if (r.path) $('#src-dir').value = r.path;
  });
  $('#src-write') && ($('#src-write').onchange = e => put({ dry_run: !e.target.checked }));
}
/* Other sources, next to the main one: a second Immich account, another folder, a photo application. Each
   keeps its photos and gets its own results; removing one hides its photos (they come back if it is added again). */
function extraCard(m) {
  if (!m) return '';
  const opts = m.options.filter(x => x.available);
  const o = opts.find(x => x.id === SRC.addPick) || opts[0];
  const browse = STATE.status?.can_pick;
  const state = x => x.key_missing ? `<span class="bad">✗ ${t('xsrc.key_missing')}</span>`
    : x.errors.length ? `<span class="bad">✗ ${esc(x.errors[0])}</span>`
    : x.writable ? `<span class="ok">✓ ${t(m.dry_run ? 'xsrc.dry' : 'xsrc.write')}</span>`
    : `<span class="warn">⚠️ ${esc(x.warnings[0] || t('xsrc.read'))}</span>`;
  const list = (m.extra || []).map(x => `<div class="setrow"><div class="lbl"><b>${esc(x.label)}</b>
      <div class="sub">${esc(srcTitle(x.type))} · ${esc(x.url || x.path || '')} · ${t('count.photos', { n: x.photos })}</div>
      <div class="sub">${state(x)}</div>
      ${x.type === 'immich' ? `<div class="srcpath xkey"><input type="password" data-xkeyin="${esc(x.id)}" placeholder="${esc(t('xsrc.key'))}"
        aria-label="${esc(t('xsrc.key'))}" autocomplete="off"><button class="btn" data-xkey="${esc(x.id)}">${t('xsrc.key_btn')}</button></div>` : ''}</div>
      <div class="ctl"><button class="btn" data-xremove="${esc(x.id)}">${t('xsrc.remove')}</button></div></div>`).join('');
  const fields = o.kind === 'server'
    ? `<input id="xsrc-url" placeholder="https://immich.example.org" aria-label="${esc(t('imm.url'))}" spellcheck="false">
       <input id="xsrc-key" type="password" placeholder="${esc(t('imm.key'))}" aria-label="${esc(t('imm.key'))}" autocomplete="off">`
    : `<input id="xsrc-path" aria-label="${esc(t(`src.${o.kind}`))}" spellcheck="false" value="${esc(o.kind === 'folder' ? '' : o.path)}"
        placeholder="${esc(t(`src.${o.kind}`))}">${browse ? `<button class="btn" id="xsrc-browse">${t('src.browse')}</button>` : ''}`;
  return `<section class="card" id="xsrccard"><h2>${t('xsrc.title')}</h2><div class="sub" style="font-size:13px">${t('xsrc.sub')}</div>
    ${list || `<div class="sub" style="margin:10px 0">${t('xsrc.none')}</div>`}
    <div class="setrow stack"><div class="lbl"><b>${t('xsrc.add')}</b></div>
      <div class="ctl srcpath"><select id="xsrc-kind" class="pill" aria-label="${esc(t('src.kind'))}">${opts.map(x =>
        `<option value="${x.id}"${x.id === o.id ? ' selected' : ''}>${esc(srcTitle(x.id))}</option>`).join('')}</select>
        ${fields}<input id="xsrc-label" placeholder="${esc(t('xsrc.label'))}" aria-label="${esc(t('xsrc.label'))}">
        <button class="btn primary" id="xsrc-add">${t('xsrc.add_btn')}</button></div></div></section>`;
}
function bindExtra() {
  if (!$('#xsrccard')) return;
  const done = async r => { SRC = r; renderSettings(); loadStatus(); };
  $('#xsrc-kind').onchange = e => { SRC.addPick = e.target.value; renderSettings(); };
  $('#xsrc-add').onclick = async () => {
    const body = { type: $('#xsrc-kind').value, label: $('#xsrc-label').value.trim(),
      ...($('#xsrc-url') ? { url: $('#xsrc-url').value.trim(), key: $('#xsrc-key').value.trim() } : { path: $('#xsrc-path').value.trim() }) };
    $('#xsrc-add').disabled = true;
    try { await done(await api('/api/sources/extra', { method: 'POST', body: JSON.stringify(body) })); toast(t('xsrc.added')); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); $('#xsrc-add').disabled = false; }
  };
  $$('#xsrccard [data-xremove]').forEach(b => b.onclick = async () => {
    if (!confirm(t('xsrc.remove_confirm'))) return;
    try { await done(await api(`/api/sources/extra/${encodeURIComponent(b.dataset.xremove)}`, { method: 'DELETE' })); toast(t('xsrc.removed')); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); }
  });
  $$('#xsrccard [data-xkey]').forEach(b => b.onclick = async () => {
    const id = b.dataset.xkey, key = $(`#xsrccard [data-xkeyin="${CSS.escape(id)}"]`).value.trim();
    if (!key) return;
    b.disabled = true;
    try { await done(await api(`/api/sources/extra/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ key }) })); toast(t('xsrc.key_saved')); }
    catch (e) { toast(t('set.refused', { msg: esc(e.message) })); b.disabled = false; }
  });
  $('#xsrc-browse') && ($('#xsrc-browse').onclick = async () => {
    const o = SRC.options.find(x => x.id === $('#xsrc-kind').value);
    const r = await api('/api/desktop/pick', { method: 'POST', body: JSON.stringify({ kind: o.kind === 'folder' ? 'folder' : 'file',
      suffix: o.suffix || '', start: $('#xsrc-path').value.trim() }) }).catch(e => { toast(t('set.refused', { msg: esc(e.message) })); return {}; });
    if (r.path) $('#xsrc-path').value = r.path;
  });
}
/* "Files" source (folder or photo application): no connection to set; state of the folder or of the
   library, and of the writing of XMP files. */
function fileCard(m) {
  const st = m.status || {}, env = m.kind === 'folder' ? 'PHOTOS_DIR' : 'CATALOG_PATH';
  return `<section class="card" id="immcard"><h2>${srcTitle(m.source)}</h2>
    <div class="sub" style="font-size:13px">${m.source === 'local' ? t('local.sub', { env: '<code>SOURCE=local</code>' }) : t(`local.sub_${m.source}`)}</div>
    <dl class="envlist">
      <dt>${t(m.kind === 'folder' ? 'local.path' : 'local.catalog')}</dt><dd><code>${esc(m.path)}</code> <span class="sub">(<code>${env}</code>)</span></dd>
      <dt>${t('local.writing')}</dt><dd>${st.code === 'ok' ? (m.dry_run ? t('local.dry_run') : t('local.xmp')) : t('local.no_write')}</dd>
    </dl>${(st.errors || []).concat(st.warnings || []).map(e => `<div class="warnline">⚠️ ${esc(e)}</div>`).join('')}</section>`;
}
function immichCard(m) {
  if (!m) return '';
  if (m.source) return fileCard(m);
  const env = m.from_env, envNote = v => `<div class="sub">${t('imm.from_env', { v: `<code>${v}</code>` })}</div>`;
  return `<section class="card" id="immcard"><h2>Immich</h2>
    <div class="sub" style="font-size:13px">${t('imm.sub')}</div>
    <div class="immform">
      <label><b>${t('imm.url')}</b><input id="imm-url" type="url" spellcheck="false" value="${esc(m.url)}" placeholder="http://immich-server:2283"${env.url ? ' disabled' : ''}>
        ${env.url ? envNote('IMMICH_URL') : `<div class="sub">${t('imm.url_sub')}</div>`}</label>
      <label><b>${t('imm.key')}</b><input id="imm-key" type="password" autocomplete="off" spellcheck="false"
        placeholder="${m.key_hint ? `•••• ${esc(m.key_hint)}` : esc(t('imm.key_ph'))}"${env.key ? ' disabled' : ''}>
        ${env.key ? envNote('IMMICH_API_KEY') : m.key_hint ? `<div class="sub">${t('imm.key_keep')}</div>` : ''}</label>
      <div class="immbtns"><button class="btn" id="imm-test">${t('imm.test')}</button>
        ${env.url && env.key ? '' : `<button class="btn primary" id="imm-save">${t('imm.save')}</button>`}</div>
      <div id="imm-state">${immichState(m.status)}</div>
    </div>
    <details class="immhelp"${m.configured ? '' : ' open'}><summary>${t('imm.how')}</summary>
      <ol><li>${t('imm.step1')}</li>
        <li>${t('imm.step2')}<div class="perms" id="imm-perms">${permChips(m, m.status)}</div>
          <button class="chip" id="imm-copy">${t('imm.copy')}</button></li>
        <li>${t('imm.step3')}</li></ol>
      <div class="sub">${t('imm.optional', { perm: '<code>asset.download</code>' })}</div>
    </details></section>`;
}
/* Request on the connection: returns the JSON even when refused (422), to show the reason. */
async function immCall(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return r.json();
}
function bindImmich() {
  if (!$('#imm-url')) return;   // no form: folder or photo application
  const values = () => ({ url: $('#imm-url').value.trim(), key: $('#imm-key').value.trim() });
  const busy = (b, on) => { b.disabled = on; b.classList.toggle('spin', on); };
  $('#imm-test').onclick = async e => {
    busy(e.target, true); $('#imm-state').innerHTML = `<div class="immst">${t('imm.testing')}</div>`;
    try { const st = await immCall('POST', '/api/immich/test', values());
      $('#imm-state').innerHTML = immichState(st); $('#imm-perms').innerHTML = permChips(IMM, st); }
    catch (err) { $('#imm-state').innerHTML = `<div class="immst bad">✗ ${esc(err.message)}</div>`; }
    finally { busy(e.target, false); }
  };
  const save = $('#imm-save');
  if (save) save.onclick = async () => {
    busy(save, true); $('#imm-state').innerHTML = `<div class="immst">${t('imm.testing')}</div>`;
    try {
      const r = await immCall('PUT', '/api/immich', values());
      if (!r.saved) { $('#imm-state').innerHTML = immichState(r) + `<div class="sub">${t('imm.not_saved')}</div>`; return; }
      IMM = r; toast(t('imm.saved')); await loadStatus(); renderSettings();
    } catch (err) { $('#imm-state').innerHTML = `<div class="immst bad">✗ ${esc(err.message)}</div>`; }
    finally { if (save.isConnected) busy(save, false); }
  };
  $('#imm-copy').onclick = () => copyText(IMM.required.join(' '), t('imm.copied'));
}
/* Under the auto-confirmation mode: groups that never benefit from it, and why. */
function groupsNote() {
  const g = Object.entries(STATE.status?.groups || {}).filter(([, v]) => !v.auto);
  if (!g.length) return '';
  const item = ([id, v]) => t(v.measured ? 'auto.group_measured' : 'auto.group_default',
    { group: esc(tOr(`group.${id}`, id)), ok: v.ok, n: v.reviews });
  return `<br><b>${t('auto.groups_blocked', { list: g.map(item).join(', ') })}</b>`;
}
/* Under the GPU / CPU choice: what the server actually has. */
function deviceNote() {
  const hw = MODELS?.hardware; if (!hw) return '';
  const vendor = { nvidia: 'NVIDIA', amd: 'AMD', intel: 'Intel', apple: 'Apple' }[hw.vendor] || '';
  return `<br><b>${hw.device !== 'cpu' ? t('device.now_gpu', { gpu: esc(`${vendor} ${hw.gpu || ''}`.trim()) })
    : SETS.values.device === 'cpu' ? t('device.now_cpu') : t('device.now_cpu_nogpu')}</b>`;
}
/* Under the OWLv2 mode: what really runs (auto mode depends on the hardware). */
function openNote() {
  const m = MODELS?.open_mode; if (!m) return '';
  return `<br><b>${t('plan.now', { what: t(`plan.${m}`) })}</b>`;
}
/* AI models and hardware actually used (not only what is requested). */
const ONNX_NAMES = { CUDAExecutionProvider: 'GPU NVIDIA (CUDA)', MIGraphXExecutionProvider: 'GPU AMD (MIGraphX)', ROCMExecutionProvider: 'GPU AMD (ROCm)',
  OpenVINOExecutionProvider: 'Intel (OpenVINO)', CoreMLExecutionProvider: 'Apple (Core ML)' };
/* GBIF taxonomy: local copy (names, synonyms, lineage) or the API meanwhile. */
function taxonomyText(x) {
  if (!x) return '';
  if (x.local || x.state === 'ready') return t('models.tax_ready', { v: esc(x.version || '') });
  const sub = { off: t('models.tax_off', { env: '<code>GBIF_LOCAL=false</code>' }), downloading: t('models.tax_downloading'),
    building: t('models.tax_building'), error: t('models.tax_error', { msg: esc(x.error || '') }) }[x.state] || t('models.tax_waiting');
  return `${t('models.tax_api')}<div class="sub">${sub}</div>`;
}
const STAGES = ['download', 'decode', 'megadetector', 'owlv2', 'crops', 'bioclip', 'geo', 'save'];
const onnxName = p => p === 'CPUExecutionProvider' ? t('models.cpu') : ONNX_NAMES[p] || p;
function modelsCard(m) {
  if (!m) return '';
  const hw = m.hardware, gpu = hw.device !== 'cpu';
  const vendor = { nvidia: 'NVIDIA', amd: 'AMD', intel: 'Intel', apple: 'Apple' }[hw.vendor] || '';
  const calc = gpu ? `GPU ${vendor} : ${esc(hw.gpu || hw.device)}${hw.memory_gb ? `, ${t('models.memory', { gb: hw.memory_gb })}` : ''}${hw.runtime ? ` · ${esc(hw.runtime)}` : ''}${m.batch ? ` · ${t('models.batch', { n: m.batch })}` : ''}` : t('models.cpu');
  const torchOn = gpu ? `GPU ${vendor}` : t('models.cpu');
  // MegaDetector runs under ONNX Runtime: its hardware may differ from PyTorch's.
  // MegaDetector: in PyTorch (same hardware as the other models), or in ONNX if a file is provided.
  const mdOn = !m.detector_file ? null : !m.detector_onnx ? torchOn : m.detector_providers ? onnxName(m.detector_providers[0])
    : t('models.planned', { what: onnxName(hw.onnx_wanted[0]) });
  const mdWarn = m.detector_onnx && gpu && m.detector_providers && m.detector_providers[0] === 'CPUExecutionProvider' && hw.onnx_wanted[0] !== 'CPUExecutionProvider';
  const vers = Object.entries(hw.versions).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ');
  return `<section class="card"><h2>${t('models.title')}</h2><dl class="envlist">
    <dt>${t('models.compute')}</dt><dd>${calc}<div class="sub">${tOr(`device.why.${hw.why}`, hw.why)} (<code>DEVICE=${esc(hw.wanted)}</code>)</div></dd>
    <dt>${t('models.fauna')}</dt><dd><b>MegaDetector v6</b> (RT-DETR, Microsoft, Apache 2.0), ${m.detector_onnx ? 'ONNX' : 'PyTorch'}
      <div class="sub">${mdOn ? t('models.runs_on', { what: esc(mdOn) }) : t('models.no_detector')}</div>
      ${mdWarn ? `<div class="warnline">⚠️ ${t('models.md_cpu_warning')}</div>` : ''}</dd>
    <dt>${t('models.flora')}</dt><dd>${m.open_detector ? `<b>OWLv2</b> (Google, Apache 2.0), ${t('models.open_vocab')}
      <div class="sub"><code>${esc(m.open_detector_model)}</code> · ${t('models.runs_on', { what: torchOn })}</div>` : `${t('models.disabled')} (<code>OPEN_DETECTOR=false</code>)`}</dd>
    <dt>${t('models.identification')}</dt><dd><b>BioCLIP 2</b> (Imageomics, MIT) : ${t('models.bioclip', { n: 950000 })}
      <div class="sub"><code>imageomics/bioclip-2</code> · ${t('models.runs_on', { what: torchOn })} · ${t('models.then_geo')}</div></dd>
    <dt>${t('models.taxonomy')}</dt><dd>${taxonomyText(m.taxonomy)}</dd>
    <dt>${t('models.small')}</dt><dd>${m.original === true ? t('models.small_on')
      : m.original === false ? t('models.small_off', { perm: '<code>asset.download</code>' })
      : `${t('models.small_maybe', { perm: '<code>asset.download</code>' })} <span class="sub">(${t('models.small_checked')})</span>`}</dd>
    <dt>${t('models.speed')}</dt><dd>${m.speed ? `${t('models.per_photo', { s: m.speed.median_s })} <span class="sub">(${t('models.speed_hint', { n: m.speed.n })})</span>`
      : t('models.not_measured')} · ${m.loaded ? t('models.loaded') : t('models.lazy')}
      ${m.speed_geo ? `<div>${t('models.speed_geo', { s: m.speed_geo.median_s })} <span class="sub">(${t('models.speed_geo_hint')})</span></div>` : ''}</dd>
    ${m.stages && Object.keys(m.stages).length ? `<dt>${t('models.stages')}</dt><dd>${STAGES.filter(k => k in m.stages)
      .map(k => `${t(`models.stage.${k}`)} <b>${m.stages[k]}</b>`).join(' · ')} <span class="sub">(${t('models.stages_hint')})</span></dd>` : ''}
    <dt>${t('models.accuracy')}</dt><dd>${t('models.accuracy_value', { ok: 53, n: 73, pct: fmtPct(53 / 73) })} <span class="sub">(${t('models.accuracy_hint')})</span></dd>
    <dt>${t('models.version')}</dt><dd><code>${esc(m.analyzer_id)}</code>${m.outdated ? `<div class="warnline">${t('models.outdated', { n: m.outdated })} : <code>taxonero reanalyze outdated</code></div>` : ''}</dd>
    <dt>${t('models.libraries')}</dt><dd class="sub">${vers}</dd>
  </dl></section>`;
}
function setCtl(r, v) {
  const id = `set-${r.k}`;
  if (r.type === 'bool') return `<label class="switch"><input type="checkbox" id="${id}" aria-label="${esc(t(`set.${r.k}`))}"${v ? ' checked' : ''}><span></span></label>`;
  if (r.type === 'seg') { const n = r.norm ? r.norm(v) : v;
    return `<div class="seg" id="${id}">${r.opts.map(o => `<button data-o="${o}" class="${o === n ? 'on' : ''}">${t(`set.${r.k}.${o}`)}</button>`).join('')}</div>`; }
  if (r.type === 'multi') { const on = new Set((v || '').split(','));
    return `<div class="seg multi" id="${id}">${r.opts.map(o => `<button data-o="${o}" class="${on.has(o) ? 'on' : ''}" aria-pressed="${on.has(o)}">${t(`set.${r.k}.${o}`)}</button>`).join('')}</div>`; }
  if (r.type === 'range') return `<input type="range" id="${id}" aria-label="${esc(t(`set.${r.k}`))}" min="${r.min}" max="${r.max}" step="${r.step}" value="${v}"><span class="val" id="${id}-v">${fmtPct(v)}</span>`;
  if (r.type === 'int') return `<input type="number" id="${id}" aria-label="${esc(t(`set.${r.k}`))}" min="${r.min}" max="${r.max}" value="${v}"> ${r.unit ? t(`unit.${r.unit}`) : ''}`;
  if (r.type === 'window') {
    const [a, b] = v ? v.split('-') : ['', ''];
    return `<div class="seg" id="${id}-mode"><button data-o="all" class="${v ? '' : 'on'}">${t('set.analysis_window.always')}</button><button data-o="win" class="${v ? 'on' : ''}">${t('set.analysis_window.window')}</button></div>
      <span id="${id}-win"${v ? '' : ' hidden'}>${t('set.analysis_window.range', { from: `<input type="time" id="${id}-a" aria-label="${esc(t('set.analysis_window'))}" value="${a || '01:00'}">`, to: `<input type="time" id="${id}-b" aria-label="${esc(t('set.analysis_window'))}" value="${b || '07:00'}">` })}</span>`;
  }
}
function renderSettings() {
  const v = SETS.values, d = SETS.defaults, e = SETS.env;
  const langs = [...new Set([...(STATE.status?.langs || []), ...(STATE.status?.ui_langs || []), LANG])];
  // Each language in its own language; "names only" if the interface is not translated (fallback: French).
  const opt = l => `${langName(l, l)}${l !== LANG && langName(l) !== langName(l, l) ? ` (${langName(l)})` : ''}${hasUI(l) ? '' : ` · ${t('set.lang.names_only')}`}`;
  // Tabs: each group of settings goes in the SET_TAB tab (Analysis by default).
  const tabs = { source: sourceCard(SRC) + immichCard(IMM) + extraCard(SRC) + undoCard(), analysis: '', validation: '', models: modelsCard(MODELS), logs: logsCard(),
    general: `<section class="card"><h2>${t('set.group.display')}</h2><div class="setrow"><div class="lbl"><b>${t('set.lang')}</b>
    <div class="sub">${t('set.lang.sub')}</div></div>
    <div class="ctl"><select id="set-lang" class="pill" aria-label="${esc(t('set.lang'))}">${langs.map(l => `<option value="${esc(l)}"${l === LANG ? ' selected' : ''}>${esc(opt(l))}</option>`).join('')}</select></div></div>
    ${namesRows(v, opt)}</section>` };
  let tab = null;
  for (const r of SET_ROWS) {
    if (r.edition && r.edition !== (STATE.status?.edition || 'server')) continue;
    if (r.group) { if (tab) tabs[tab] += '</section>'; tab = SET_TAB[r.group] || 'analysis'; tabs[tab] += `<section class="card"><h2>${t(`set.group.${r.group}`)}</h2>`; continue; }
    if (r.when && !r.when(v)) continue;
    const changed = r.norm ? r.norm(v[r.k]) !== r.norm(d[r.k]) : JSON.stringify(v[r.k]) !== JSON.stringify(d[r.k]);
    tabs[tab] += `<div class="setrow${r.type === 'multi' ? ' stack' : ''}"><div class="lbl"><b>${t(`set.${r.k}`)}</b><div class="sub">${t(`set.${r.k}.sub`)}${r.extra ? r.extra() : ''}</div></div>
      <div class="ctl">${changed ? `<button class="reset" data-reset="${r.k}" title="${esc(t('set.default_is', { v: fmtSet(r, d[r.k]) }))}">${t('set.default')}</button>` : ''}${setCtl(r, v[r.k])}</div></div>`;
  }
  tabs[tab] += '</section>';
  tabs.general += `<section class="card"><h2>${t('env.title')}</h2><div class="sub" style="font-size:13px">${t('env.sub', { file: '<code>.env</code>' })}</div>
    <dl class="envlist">
      <dt>${t('env.writing')}</dt><dd>${e.dry_run ? `${t('env.writing_off')} (<code>DRY_RUN=true</code>)` : t('env.writing_on')}</dd>
      <dt>${t('env.tag_root')}</dt><dd>${esc(e.tag_root)}</dd>
      <dt>${t('env.open_detector')}</dt><dd>${e.open_detector ? t('env.open_detector_on') : t('env.open_detector_off')}</dd>
    </dl></section>`;
  const cur = STATE.status?.immich_configured && TABS.includes(setTab) ? setTab : 'source';   // no source yet: Source
  let h = `<div class="settabs" role="tablist">${TABS.map(k => `<button role="tab" id="settab-${k}" aria-controls="setpanel-${k}" aria-selected="${k === cur}" tabindex="${k === cur ? 0 : -1}" data-tab="${k}">${t(TAB_LABEL[k])}</button>`).join('')}</div>`;
  h += TABS.map(k => `<div role="tabpanel" id="setpanel-${k}" aria-labelledby="settab-${k}"${k === cur ? '' : ' hidden'}>${tabs[k]}</div>`).join('');
  $('#settings').innerHTML = h;
  bindSettings();
  bindImmich();
  bindSource();
  bindExtra();
  bindLogs();
  bindUndo();
  showSetAnchor();
}
/* Names written to the photos: scientific names only, or common names in the interface language or a fixed one. */
function namesRows(v, opt) {
  const sw = (id, on, label) => `<label class="switch"><input type="checkbox" id="${id}" aria-label="${esc(label)}"${on ? ' checked' : ''}><span></span></label>`;
  const langs = STATE.status?.langs || [v.names_lang];
  let h = `<div class="setrow"><div class="lbl"><b>${t('set.scientific_names')}</b><div class="sub">${t('set.scientific_names.sub')}</div></div>
    <div class="ctl">${sw('set-scientific_names', v.scientific_names, t('set.scientific_names'))}</div></div>`;
  if (v.scientific_names) return h;
  h += `<div class="setrow"><div class="lbl"><b>${t('set.names_follow_ui')}</b><div class="sub">${t('set.names_follow_ui.sub')}</div></div>
    <div class="ctl">${sw('set-names_follow_ui', v.names_follow_ui, t('set.names_follow_ui'))}</div></div>`;
  if (!v.names_follow_ui) h += `<div class="setrow"><div class="lbl"><b>${t('set.names_lang')}</b><div class="sub">${t('set.names_lang.sub')}</div></div>
    <div class="ctl"><select id="set-names_lang" class="pill" aria-label="${esc(t('set.names_lang'))}">${langs.map(l => `<option value="${esc(l)}"${l === v.names_lang ? ' selected' : ''}>${esc(opt(l))}</option>`).join('')}</select></div></div>`;
  return h;
}
async function changeNames(values) {
  if (await saveNames(values)) { SETS = await api('/api/settings'); toast(t('set.saved')); drawBack(); }
  renderSettings();
}
function fmtSet(r, x) {
  if (r.type === 'bool') return x ? t('common.yes') : t('common.no');
  if (r.type === 'seg') { const n = r.norm ? r.norm(x) : x; return tOr(`set.${r.k}.${n}`, x); }
  if (r.type === 'range') return fmtPct(x);
  if (r.type === 'multi') return (x || '').split(',').filter(Boolean).map(o => t(`set.${r.k}.${o}`)).join(', ');
  if (r.type === 'window') return x || t('set.analysis_window.always');
  return `${x} ${r.unit ? t(`unit.${r.unit}`) : ''}`;
}
async function saveSetting(k, value) {
  const filters = k === 'exclude_extinct' || k === 'hide_absent';
  try {
    SETS = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ [k]: value }) });
    if (['device', 'open_detector_mode'].includes(k)) MODELS = await api('/api/models').catch(() => MODELS);
    toast(filters ? t('set.saved_filters') : k === 'device' ? t('set.saved_device') : t('set.saved'));
  } catch (e) {
    toast(t('set.refused', { msg: esc(e.message) })); SETS = await api('/api/settings');
  }
  renderSettings();
}
function bindSettings() {
  $$('#settings [role=tab]').forEach(b => {
    b.onclick = () => showSetTab(b.dataset.tab);
    b.onkeydown = ev => {   // arrows: neighbouring tab (ARIA tabs pattern)
      const i = TABS.indexOf(b.dataset.tab), d = { ArrowRight: 1, ArrowLeft: -1, Home: -i, End: TABS.length - 1 - i }[ev.key];
      if (d != null) { ev.preventDefault(); showSetTab(TABS[(i + d + TABS.length) % TABS.length], true); }
    };
  });
  $('#set-scientific_names').onchange = e => changeNames({ scientific_names: e.target.checked });
  $('#set-names_follow_ui') && ($('#set-names_follow_ui').onchange = e => changeNames(e.target.checked && STATE.status?.langs?.includes(LANG)
    ? { names_follow_ui: true, names_lang: LANG } : { names_follow_ui: e.target.checked }));
  $('#set-names_lang') && ($('#set-names_lang').onchange = e => changeNames({ names_lang: e.target.value }));
  $('#set-lang').onchange = async e => { await setLang(e.target.value); renderSettings(); toast(t('set.lang.changed', { lang: esc(langName(LANG)) })); };
  for (const r of SET_ROWS) {
    if (!r.k) continue;
    const el = $(`#set-${r.k}`);
    if (r.type === 'bool' && el) el.onchange = () => saveSetting(r.k, el.checked);
    if (r.type === 'seg' && el) el.onclick = ev => { const b = ev.target.closest('button'); if (b) saveSetting(r.k, b.dataset.o); };
    if (r.type === 'multi' && el) el.onclick = ev => {   // checkboxes: at least one stays ticked
      const b = ev.target.closest('button'); if (!b) return;
      const on = new Set(SETS.values[r.k].split(',').filter(Boolean));
      if (on.has(b.dataset.o)) on.delete(b.dataset.o); else on.add(b.dataset.o);
      if (!on.size) { toast(t('set.detect_groups.one')); return; }
      saveSetting(r.k, r.opts.filter(o => on.has(o)).join(','));
    };
    if (r.type === 'range' && el) {
      el.oninput = () => { $(`#set-${r.k}-v`).textContent = fmtPct(el.value); };
      el.onchange = () => saveSetting(r.k, +el.value);
    }
    if (r.type === 'int' && el) el.onchange = () => saveSetting(r.k, +el.value);
    if (r.type === 'window' && $(`#set-${r.k}-mode`)) {
      const win = () => `${$(`#set-${r.k}-a`).value}-${$(`#set-${r.k}-b`).value}`;
      $(`#set-${r.k}-mode`).onclick = ev => { const b = ev.target.closest('button'); if (b) saveSetting(r.k, b.dataset.o === 'all' ? '' : win()); };
      $(`#set-${r.k}-a`).onchange = $(`#set-${r.k}-b`).onchange = () => saveSetting(r.k, win());
    }
  }
  $$('#settings [data-reset]').forEach(b => b.onclick = () => saveSetting(b.dataset.reset, null));
}
