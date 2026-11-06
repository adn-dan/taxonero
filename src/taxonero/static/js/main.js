/* ============================== startup ============================== */
loadLang(LANG).then(loadStatus).then(() => {
  // Saved language or the browser's, if the interface or the names exist in that language; otherwise French.
  if (!hasUI(LANG) && !(STATE.status?.langs || []).includes(LANG)) return setLang('fr').then(route);
  // First launch: no Immich connection yet, its setting is opened directly.
  if (!STATE.status?.immich_configured && !location.hash.startsWith('#settings')) location.hash = 'settings';
  route(); pollSync(false);
}, e => { fail(e); route(); pollSync(false); });   // server unreachable at load: the error, not an empty page
setInterval(() => loadStatus().catch(() => {}), 60000);
