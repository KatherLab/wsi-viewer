// Measurement tool: pointer-driven distance drawing on a canvas overlay,
// stored-measurement hover tooltips, and unit formatting.
// Operates on the root Vue instance (`this`); OpenSeadragon is a global.
export const measureMethods = {
  toggleMeasure(){
    if (!this.hasScale) return;
    this.measuring = !this.measuring;
    if (this.measuring) this.attachMeasure();
    else this.detachMeasure();
  },
  attachMeasure(){
    if (!this.osd) return;
    const c = document.getElementById('meas-overlay');
    this.measCanvas = c;
    this.measCtx = c ? c.getContext('2d') : null;
    this.resizeMeasCanvas();
    this.redrawMeasurements();
    document.getElementById('osd-container').classList.add('meas-active');

    // Disable OSD pan + zoom-to-click while measuring so drag draws a line.
    const viewer = this.osd;
    const gs = viewer.gestureSettingsByDeviceType('mouse');
    this._measPrevGesture = {dragToPan: gs.dragToPan, clickToZoom: gs.clickToZoom, dblClickToZoom: gs.dblClickToZoom};
    gs.dragToPan = false;
    gs.clickToZoom = false;
    gs.dblClickToZoom = false;

    // Use plain DOM pointer events on the viewer canvas. Unlike OSD's
    // MouseTracker (whose e.position is in document coordinates), pointer
    // events give us offsetX/offsetY directly relative to the element, which
    // is what windowToImageCoordinates expects.
    const el = viewer.canvas;
    this._measPointerDown = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      this._measStart(this._pointerPos(ev));
      window.addEventListener('pointermove', this._measPointerMove);
      window.addEventListener('pointerup', this._measPointerUp, {once:true});
    };
    this._measPointerMove = (ev) => {
      if (!this._measCurrent) return;
      this._measDrag(this._pointerPos(ev));
    };
    this._measPointerUp = (ev) => {
      window.removeEventListener('pointermove', this._measPointerMove);
      this._measEnd(this._pointerPos(ev));
    };
    el.addEventListener('pointerdown', this._measPointerDown);
    this._measEl = el;
    window.addEventListener('resize', this._measResizeBound = this._measResizeBound || (()=>{ this.resizeMeasCanvas(); this.redrawMeasurements(); }));
  },
  detachMeasure(){
    if (this._measEl) {
      this._measEl.removeEventListener('pointerdown', this._measPointerDown);
      this._measEl = null;
    }
    window.removeEventListener('pointermove', this._measPointerMove);
    if (this.osd && this._measPrevGesture) {
      try {
        const gs = this.osd.gestureSettingsByDeviceType('mouse');
        gs.dragToPan = this._measPrevGesture.dragToPan;
        gs.clickToZoom = this._measPrevGesture.clickToZoom;
        gs.dblClickToZoom = this._measPrevGesture.dblClickToZoom;
      } catch(e){}
      this._measPrevGesture = null;
    }
    document.getElementById('osd-container')?.classList.remove('meas-active');
    this.measLive = null;
    this.redrawMeasurements();
    window.removeEventListener('resize', this._measResizeBound);
  },
  _pointerPos(ev){
    // Position relative to the OSD viewer element (CSS pixels).
    const rect = this.osd.element.getBoundingClientRect();
    return {x: ev.clientX - rect.left, y: ev.clientY - rect.top};
  },
  _hoverMeasure(ev){
    if (!this.osd || this._measCurrent || !this.measurements.length) { this.measHover = null; return; }
    const p = this._pointerPos(ev);
    const threshold = 8; // px hit radius
    let best = null;
    for (const m of this.measurements) {
      const a = this._imgToScreen({x:m.x1, y:m.y1});
      const b = this._imgToScreen({x:m.x2, y:m.y2});
      const d = this._distToSegment(p, a, b);
      if (d <= threshold && (best === null || d < best.d)) {
        best = {d, m};
      }
    }
    this.measHover = best ? {x: p.x, y: p.y, text: this.formatLength(best.m.lengthUm)} : null;
  },
  _distToSegment(p, a, b){
    const dx = b.x - a.x, dy = b.y - a.y;
    const len2 = dx*dx + dy*dy;
    if (len2 === 0) return Math.hypot(p.x - a.x, p.y - a.y);
    let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const cx = a.x + t * dx, cy = a.y + t * dy;
    return Math.hypot(p.x - cx, p.y - cy);
  },
  resizeMeasCanvas(){
    const c = this.measCanvas; if (!c || !this.osd) return;
    const r = this.osd.element.getBoundingClientRect();
    const DPR = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(r.width * DPR));
    const h = Math.max(1, Math.floor(r.height * DPR));
    // Only reassign when size changed (avoids clearing the canvas needlessly
    // and avoids a resize→redraw→resize loop).
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
      c.style.width = r.width + 'px';
      c.style.height = r.height + 'px';
    }
  },
  _screenToImage(p){
    // p is a CSS pixel relative to the viewer element; windowToImageCoordinates
    // expects document coords, so add the element's offset.
    const vp = this.osd.viewport;
    const rect = this.osd.element.getBoundingClientRect();
    return vp.windowToImageCoordinates(new OpenSeadragon.Point(p.x + rect.left, p.y + rect.top));
  },
  _imgToScreen(p){
    // imageToWindowCoordinates returns document coords; convert to coords
    // relative to the viewer element (which is the canvas origin).
    const w = this.osd.viewport.imageToWindowCoordinates(new OpenSeadragon.Point(p.x, p.y));
    const rect = this.osd.element.getBoundingClientRect();
    return {x: w.x - rect.left, y: w.y - rect.top};
  },
  _measStart(pos){
    const img = this._screenToImage(pos);
    this._measCurrent = {x1: img.x, y1: img.y, x2: img.x, y2: img.y};
    this.measLive = {x: pos.x, y: pos.y, text: this.formatLength(0)};
    this.redrawMeasurements();
  },
  _measDrag(pos){
    if (!this._measCurrent) return;
    const img = this._screenToImage(pos);
    this._measCurrent.x2 = img.x; this._measCurrent.y2 = img.y;
    const dx = this._measCurrent.x2 - this._measCurrent.x1;
    const dy = this._measCurrent.y2 - this._measCurrent.y1;
    const lenPx = Math.sqrt(dx*dx + dy*dy);
    const lenUm = lenPx * (this.mpp || 0);
    // Live readout position (CSS px, relative to viewer container origin)
    this.measLive = {x: pos.x, y: pos.y, text: this.formatLength(lenUm)};
    this.redrawMeasurements();
  },
  _measEnd(pos){
    if (!this._measCurrent) return;
    const dx = this._measCurrent.x2 - this._measCurrent.x1;
    const dy = this._measCurrent.y2 - this._measCurrent.y1;
    const lenPx = Math.sqrt(dx*dx + dy*dy);
    // Ignore trivial clicks
    if (lenPx > 2) {
      const lenUm = lenPx * (this.mpp || 0);
      this.measSeq++;
      this.measurements.push({
        id: 'm' + this.measSeq,
        lengthUm: lenUm,
        color: this.measPalette[(this.measurements.length) % this.measPalette.length],
        x1: this._measCurrent.x1, y1: this._measCurrent.y1,
        x2: this._measCurrent.x2, y2: this._measCurrent.y2,
      });
    }
    this._measCurrent = null;
    this.measLive = null;
    this.redrawMeasurements();
  },
  removeMeasurement(i){ this.measurements.splice(i,1); this.redrawMeasurements(); },
  clearMeasurements(){ this.measurements = []; this.redrawMeasurements(); },
  formatLength(um){
    if (!Number.isFinite(um)) return '–';
    if (this.measUnits === 'mm' || um >= 1000) return (um/1000).toFixed(um >= 10000 ? 1 : 3) + ' mm';
    if (um >= 1) return um.toFixed(um >= 100 ? 0 : 2) + ' µm';
    return um.toFixed(3) + ' µm';
  },
  redrawMeasurements(){
    // Lazily grab the overlay canvas so redraw works during normal
    // (non-measuring) pan/zoom, even if attachMeasure was never called.
    if (!this.measCanvas) {
      const c = document.getElementById('meas-overlay');
      this.measCanvas = c;
      this.measCtx = c ? c.getContext('2d') : null;
    }
    const ctx = this.measCtx; const c = this.measCanvas;
    if (!ctx || !c || !this.osd) return;
    // Ensure canvas is sized to the current viewer (cheap if unchanged)
    this.resizeMeasCanvas();
    const DPR = window.devicePixelRatio || 1;
    ctx.clearRect(0,0,c.width,c.height);
    const draw = (m, active) => {
      const a = this._imgToScreen({x:m.x1, y:m.y1});
      const b = this._imgToScreen({x:m.x2, y:m.y2});
      ctx.save();
      ctx.scale(DPR, DPR);
      ctx.strokeStyle = m.color;
      ctx.lineWidth = active ? 2.5 : 2;
      ctx.lineCap = 'round';
      // Shadow for contrast
      ctx.shadowColor = 'rgba(255,255,255,0.9)';
      ctx.shadowBlur = 4;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      // Endpoints
      ctx.fillStyle = m.color;
      ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.arc(a.x, a.y, 3, 0, Math.PI*2); ctx.fill();
      ctx.beginPath(); ctx.arc(b.x, b.y, 3, 0, Math.PI*2); ctx.fill();
      ctx.restore();
    };
    this.measurements.forEach(m => draw(m, false));
    if (this._measCurrent) draw({...this._measCurrent, color:'#2563eb'}, true);
    // Keep aligned with pan/zoom
  },
};
