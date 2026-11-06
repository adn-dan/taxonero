/* Viewer: boxes (Annotorious) and zoom (Panzoom).
   - Annotorious draws, selects, moves and resizes the boxes, in the pixel frame of
     the displayed image (thumbnail, preview or original: the detections, normalised 0..1, are converted to it at
     every load).
   - Panzoom zooms the #pz layer (photo, boxes, labels): wheel towards the cursor, pinch, mouse
     click (×3 zoom or back), double tap with a finger, Z, + and - keys. Zoomed, a drag moves the photo; it
     is left to Annotorious in draw mode (B), and on the handles of the selected box.
   - Click on a box other than the selected one: it becomes the displayed subject, without zoom.
   - Drawing a box needs draw mode (B or icon), with the mouse as with a finger.
   The labels (name of the taxon) are an HTML layer on top, of constant size on screen. */
const HANDLES = '.a9s-handle, .a9s-shape-handle, .a9s-edge-handle, .a9s-corner-handle, .a9s-touch-handle';
const anno = Annotorious.createImageAnnotator($('#photo'), {
  drawingEnabled: false, drawingMode: 'drag', disableUndoRedoKeys: true, theme: 'dark',
});
const pz = Panzoom($('#pz'), { minScale: 1, maxScale: 8, contain: 'outside', excludeClass: 'pz-skip', cursor: '' });

let drawing = false;   // draw mode (B or icon): a single box, then back to normal mode
const zoomed = () => pz.getScale() > 1.001;
function syncDraw() {
  const on = drawing;
  if (anno.isDrawingEnabled() !== on) anno.setDrawingEnabled(on);
  $('#frame').classList.toggle('pz-skip', on);
}
function enableDraw() { drawing = true; $('#frame').classList.add('drawing'); $('#drawhint').classList.add('on'); syncDraw(); }
function stopDraw() {
  drawing = false; anno.cancelDrawing();
  $('#frame').classList.remove('drawing'); $('#drawhint').classList.remove('on'); syncDraw();
}

/* -- boxes ------------------------------------------------------------ */
const natural = () => { const img = $('#photo'); return [img.naturalWidth, img.naturalHeight]; };
function toAnno(d) {
  const [W, H] = natural(), x = d.x * W, y = d.y * H, w = d.w * W, h = d.h * H;
  return { id: String(d.id), bodies: [], target: { annotation: String(d.id), selector: { type: 'RECTANGLE',
    geometry: { x, y, w, h, bounds: { minX: x, minY: y, maxX: x + w, maxY: y + h } } } } };
}
function fromGeometry(g) {
  const [W, H] = natural(), c = v => Math.min(1, Math.max(0, v));
  const x = c(g.x / W), y = c(g.y / H);
  return { x, y, w: Math.min(1 - x, g.w / W), h: Math.min(1 - y, g.h / H) };
}
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function boxStyle(a) {
  const k = cur()?.detections.findIndex(d => String(d.id) === a.id), d = cur()?.detections[k];
  const color = d?.status === 'confirmed' ? css('--box-ok') : d?.status === 'rejected' ? 'rgba(255,255,255,.55)' : css('--box');
  // Thickness divided by the zoom: under a CSS transform, Firefox also enlarges the strokes (vector-effect
  // non-scaling-stroke does not compensate for it).
  const width = k === V.sel ? 3 : d && d.w * d.h > 0.98 ? 1 : 2;
  return { stroke: color, strokeWidth: width / pz.getScale(), fill: color, fillOpacity: 0 };
}
function drawBoxes() {
  // Image not displayed yet (size 0 at opening): Annotorious would compute NaN; redrawn when it loads.
  const a = cur(); if (!a || V.opening || !natural()[0] || !$('#photo').clientWidth) return;
  anno.setStyle(a2 => boxStyle(a2));   // new function: redraw with the up-to-date selection
  anno.setAnnotations(a.detections.map(toAnno), true);
  const d = curDet();
  if (d) anno.setSelected(String(d.id)); else anno.cancelSelected();
  drawLabels();
}
function drawLabels() {
  const a = cur(); if (!a) return;
  $('#labels').innerHTML = a.detections.map((d, k) => {
    const inside = d.w * d.h > 0.98 || d.y < 0.07;
    const pos = inside ? `left:${d.x * 100}%;top:${d.y * 100}%` : `left:${d.x * 100}%;bottom:${(1 - d.y) * 100}%`;
    return `<span class="vlbl ${d.status}${k === V.sel ? ' sel' : ''}${inside ? ' in' : ''}" style="${pos}">${subjectIcon(d)} ${esc(shortName(d.taxon))}${d.status === 'confirmed' ? ' ✓' : ''}</span>`;
  }).join('');
}
// After layout: Annotorious reads the image size again at the next frame (otherwise a NaN scale for a moment).
$('#photo').addEventListener('load', () => { if (V.open) requestAnimationFrame(() => requestAnimationFrame(() => { if (V.open) drawBoxes(); })); });

// Click on a box: it becomes the subject shown in the panel.
anno.on('selectionChanged', sel => {
  const k = sel[0] ? cur()?.detections.findIndex(d => String(d.id) === sel[0].id) : -1;
  if (k >= 0 && k !== V.sel) { V.sel = k; anno.setStyle(a => boxStyle(a)); drawLabels(); drawInfo(); }
});

// New box drawn: identified by the server, then selected.
anno.on('createAnnotation', async created => {
  const box = fromGeometry(created.target.selector.geometry);
  stopDraw();
  if (box.w < 0.02 || box.h < 0.02) { anno.removeAnnotation(created.id); return; }
  if (!V.info) toggleInfo();
  $('#vi-body').innerHTML = `<div class="empty">${t('det.identifying')}</div>`;
  const before = new Set(cur().detections.map(x => x.id));
  try {
    const a = await api(`/api/assets/${cur().asset_id}/boxes`, { method: 'POST', body: JSON.stringify(box) });
    V.list[V.i] = { ...cur(), ...a }; V.touched.add(V.i);
    const made = a.detections.find(x => !before.has(x.id));
    if (made) pushUndo({ asset: cur().asset_id, det: made.id, created: true });   // U deletes the created box
    V.sel = Math.max(0, a.detections.findIndex(x => x === made));
  } catch (e) { fail(e); }
  drawViewer();
});

/* Box moved or resized: at the end of the gesture, new identification of the crop. */
let editing = null, press = null;
$('#frame').addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  if (e.pointerType === 'mouse') press = { x: e.clientX, y: e.clientY, sel: V.sel, target: e.target };
  else if (doubleTap(e)) { e.stopPropagation(); e.preventDefault(); return; }
  // Outside the selected box, Annotorious would use this drag to deselect it: do it beforehand,
  // so that the drag draws a box.
  if (anno.isDrawingEnabled() && !e.target.closest(HANDLES)) anno.cancelSelected();
  if (!e.target.closest(HANDLES) || drawing) return;
  const d = curDet(); if (!d) return;
  editing = { id: d.id, before: { x: d.x, y: d.y, w: d.w, h: d.h } };
  $('#frame').classList.add('pz-skip');   // Panzoom does not move the photo during this gesture
}, true);
window.addEventListener('pointerup', ev => {
  const e = editing, p = press; editing = press = null;
  if (e) setTimeout(() => commitEdit(e));   // after Annotorious, which finishes updating the box
  syncDraw();
  if (p && !drawing && Math.hypot(ev.clientX - p.x, ev.clientY - p.y) < 5) clickPhoto(ev, p);
});
/* Mouse click: another box -> it becomes the subject; otherwise ×3 zoom at that spot, or back to the whole
   photo. The second click of a double click is ignored: it would immediately cancel the zoom. */
let lastClick = 0;
function clickPhoto(e, p) {
  if (!V.open || p.target.closest('.a9s-corner-handle, .a9s-edge-handle')) return;
  const now = Date.now(), repeat = now - lastClick < 350;
  lastClick = now;
  if (repeat) return;
  const r = $('#photo').getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  const hits = cur().detections.map((d, k) => ({ k, area: d.w * d.h, in: x >= d.x && x <= d.x + d.w && y >= d.y && y <= d.y + d.h }))
    .filter(h => h.in).sort((a, b) => a.area - b.area);
  if (hits[0] && hits[0].k !== p.sel) { V.sel = hits[0].k; drawBoxes(); drawInfo(); return; }
  if (zoomed()) resetZoom(); else zoomAt(e.clientX, e.clientY, 3);
  // Annotorious deselects on a click outside the box: the displayed subject stays selected.
  setTimeout(() => { const d = curDet(); if (d) anno.setSelected(String(d.id)); });
}
async function commitEdit({ id, before }) {
  const a = anno.getAnnotationById(String(id)), d = cur()?.detections.find(x => x.id === id);
  if (!a || !d) return;
  const b = fromGeometry(a.target.selector.geometry);
  if (Math.abs(b.x - before.x) + Math.abs(b.y - before.y) + Math.abs(b.w - before.w) + Math.abs(b.h - before.h) < 0.01) return;   // simple click
  pushUndo({ asset: cur().asset_id, det: d.id, prev: snapshot(d) });
  $('#vi-body').innerHTML = `<div class="empty">${t('det.reidentify')}</div>`;
  try {
    const r = await api(`/api/detections/${d.id}/box`, { method: 'PUT', body: JSON.stringify(b) });
    V.list[V.i] = { ...cur(), ...r }; V.touched.add(V.i);
  } catch (err) { fail(err); }
  V.sel = Math.max(0, cur().detections.findIndex(x => x.id === id)); drawViewer(); loadStatus();
}

/* -- zoom ------------------------------------------------------------------ */
/* From the first zoom, the original (full resolution) replaces the preview, if the key gives access to it. */
const Z = { hi: null };
$('#pz').addEventListener('panzoomchange', e => {
  const z = e.detail.scale;
  $('#pz').style.setProperty('--z', z);
  $('#frame').classList.toggle('zoomed', z > 1.001);
  anno.setStyle(a => boxStyle(a));
  syncDraw();
  if (z > 1.2) loadFull();
});
const zoomAt = (cx, cy, z) => pz.zoomToPoint(Math.min(8, Math.max(1, z)), { clientX: cx, clientY: cy }, { animate: false });
const resetZoom = () => pz.reset({ animate: false });
const zoomCenter = k => { const r = $('#frame').getBoundingClientRect(); zoomAt(r.left + r.width / 2, r.top + r.height / 2, pz.getScale() * k); };
/* Z: zoom on the selected subject (centred, it fills the view), or back to the whole photo. */
function zoomToSubject() {
  const d = curDet();
  if (zoomed() || !d) { zoomed() ? resetZoom() : zoomCenter(3); return; }
  const f = $('#pz'), w = f.offsetWidth, h = f.offsetHeight;
  pz.zoom(Math.min(8, Math.max(1.5, 0.85 / Math.max(d.w, d.h))), { animate: false });
  // Panzoom transform: scale(s) translate(x, y), origin at the centre; offset in unzoomed pixels.
  setTimeout(() => pz.pan(w / 2 - (d.x + d.w / 2) * w, h / 2 - (d.y + d.h / 2) * h, { animate: false }));
}
function loadFull() {
  const a = cur(); if (!a || Z.hi === a.asset_id || a.kind === 'VIDEO') return;
  Z.hi = a.asset_id;
  const im = new Image();
  im.onload = () => {
    const img = $('#photo');
    // Proportions different from the preview (photo cropped in Immich): the boxes would no longer fit.
    if (img.dataset.id !== a.asset_id || Math.abs(im.naturalWidth / im.naturalHeight / V.ar - 1) > 0.02) return;
    img.dataset.full = a.asset_id; img.src = im.src;
  };
  im.src = `/api/full/${a.asset_id}`;   // on failure (no permission, unreadable RAW): the preview is kept
}
$('.stage').addEventListener('wheel', e => { if (V.open) { e.preventDefault(); zoomAt(e.clientX, e.clientY, pz.getScale() * wheelFactor(e)); } }, { passive: false });
/* Double tap with a finger: ×3 zoom at that spot, or back. No dblclick event: it does not arrive
   when the tap is intercepted (drawing, moving). Return true if the tap is the second of a double. */
let lastTap = null;
function doubleTap(e) {
  const now = Date.now(), prev = lastTap;
  lastTap = { t: now, x: e.clientX, y: e.clientY };
  if (!prev || now - prev.t > 350 || Math.hypot(e.clientX - prev.x, e.clientY - prev.y) > 20) return false;
  lastTap = null;
  if (zoomed()) resetZoom(); else zoomAt(e.clientX, e.clientY, 3);
  return true;
}
