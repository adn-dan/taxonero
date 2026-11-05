/* ============================== démarrage ============================== */
loadLang(LANG).then(loadStatus).then(() => {
  // Langue enregistrée ou du navigateur, si l'interface ou les noms existent dans cette langue ; sinon français.
  if (!hasUI(LANG) && !(STATE.status?.langs || []).includes(LANG)) return setLang('fr').then(route);
  // Premier lancement : pas encore de connexion à Immich, on ouvre directement son réglage.
  if (!STATE.status?.immich_configured && !location.hash.startsWith('#settings')) location.hash = 'settings';
  route(); pollSync(false);
}, e => { fail(e); route(); pollSync(false); });   // serveur injoignable au chargement : l'erreur, pas une page vide
setInterval(() => loadStatus().catch(() => {}), 60000);
