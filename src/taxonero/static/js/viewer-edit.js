/* Visionneuse : annulation et raccourcis clavier (rectangles et zoom : viewer-boxes.js). */
$('#v-undo').onclick = undo;

/* Clic à la souris : le bouton rend le focus, Entrée continue donc de valider la détection. */
$('#viewer').addEventListener('click', e => { if (e.detail) e.target.closest?.('button,a')?.blur(); });
/* Tab parcourt les contrôles de la visionneuse (et du panneau Wikipédia ouvert par-dessus), sans passer derrière. */
function keepFocusInViewer(e) {
  const els = $$('#viewer, #wiki.on').flatMap(root => [...root.querySelectorAll('button,a[href],input,select,textarea,[tabindex="0"]')])
    .filter(el => !el.disabled && el.getClientRects().length);
  if (!els.length) { e.preventDefault(); return; }
  // Focus hors de la visionneuse, ou aux extrémités : on reboucle. Ailleurs (zone défilante comprise), Tab fait son travail.
  const active = document.activeElement, outside = !active?.closest('#viewer, #wiki.on');
  if (outside || active === els[e.shiftKey ? 0 : els.length - 1]) { e.preventDefault(); els[e.shiftKey ? els.length - 1 : 0].focus(); }
}

/* clavier */
document.addEventListener('keydown', e => {
  if (CMP.open) { if (!$('#locpick').classList.contains('on')) cmpKey(e); return; }   // comparaison ouverte : touches de la visionneuse inactives
  if ($('#taxpick').open || $('#locpick').classList.contains('on')) return;   // boîte de dialogue au-dessus
  if (V.open && e.key === 'Tab') { keepFocusInViewer(e); return; }
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === 'Escape') {
    if (drawing) stopDraw(); else if ($('#wiki').classList.contains('on')) closeWiki(); else if (V.open) leaveViewer();
    else if (P.selecting || selCount()) setSelecting(false);
    return;
  }
  const k = e.key;
  if (!V.open) {   // U : annulation de la session, partout
    if ((k === 'u' || k === 'U') && !e.ctrlKey && !e.metaKey && !e.altKey) undo();
    return;
  }
  // Bouton, lien ou candidat qui a le focus : la touche l'active lui, sans valider la détection en plus.
  const widget = e.target.closest?.('button,a,summary,[role=button]');
  if (widget && (k === 'Enter' || k === ' ')) {
    if (!widget.matches('button,a,summary')) { e.preventDefault(); widget.click(); }
    return;
  }
  if (k === 'ArrowRight') go(1);
  else if (k === 'ArrowLeft') go(-1);
  else if (k === 'Enter') act('confirm');
  else if (k === 'x' || k === 'X') act('reject');
  else if (k === 'u' || k === 'U') undo();
  else if (k === 'b' || k === 'B') enableDraw();
  else if (k === 'i' || k === 'I') toggleInfo();
  else if (k === 'w' || k === 'W') { if ($('#wiki').classList.contains('on')) closeWiki(); else if (curDet()?.taxon_key) openWiki(curDet().taxon_key); }
  else if (k === '/') { e.preventDefault(); chooseTaxon(); }
  else if (k === 'Delete') { e.preventDefault(); deleteDet(); }
  else if (k === 's' || k === 'S') { V.sel = (V.sel + 1) % Math.max(1, cur().detections.length); drawBoxes(); drawInfo(); }
  else if (k === 'z' || k === 'Z') zoomToSubject();
  else if (k === '+' || k === '=') zoomCenter(1.5);
  else if (k === '-') zoomCenter(1 / 1.5);
  else if (/^[1-8]$/.test(k)) pick(+k - 1);
  else return;
});
