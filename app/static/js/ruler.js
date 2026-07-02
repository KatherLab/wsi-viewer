// Ruler function (with bottom positioning) + resolution warning + cleanup support.
// Moved verbatim from the original inline <script>.
export function attachRuler(viewer, mppX, opts = {}) {
  const canvas = document.getElementById("ruler");
  const options = {
    targetCssWidth: 220,
    majorTicks: 3,
    tickLenCss: 5,
    barThicknessCss: 2,
    labelFontSizeCss: 12,
    pillPaddingCss: 12,
    minPillWidthCss: 160,
    gapLabelToBarCss: 6,
    safeBottomMarginCss: 2,
    pillBgRGBA: "rgba(255,255,255,0.8)",
    pillBorderRGBA: "rgba(17,24,39,0.10)",
    strokeRGBA: "#111827",
    ...opts
  };

  // No viewer? bail early
  if (!viewer || !canvas) {
    return () => {};
  }

  function resize() {
    if (!viewer || !viewer.element) return;
    const DPR = window.devicePixelRatio || 1;
    const rect = viewer.element.getBoundingClientRect();
    canvas.width  = Math.max(1, Math.floor(rect.width * DPR));
    canvas.height = Math.max(1, Math.floor(rect.height * DPR));
    canvas.style.width  = rect.width + "px";
    canvas.style.height = rect.height + "px";
  }

  function nice(v) {
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const d = v / p;
    const step = d < 2 ? 1 : d < 5 ? 2 : 5;
    return step * p;
  }

  function redraw() {
    if (!viewer || !viewer.element) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const DPR = window.devicePixelRatio || 1;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const vp = viewer.viewport;
    const zoom = vp.viewportToImageZoom(vp.getZoom(true));

    // Figure out a "nice" total length for the bar
    const rect = viewer.element.getBoundingClientRect();
    const imgPx = options.targetCssWidth / zoom;
    const um = imgPx * mppX;
    const niceUm = nice(um);
    const barCss = (niceUm / mppX) * zoom;
    const barPx  = barCss * DPR;

    const labelText = niceUm >= 1000
      ? `${(niceUm/1000).toFixed(niceUm % 1000 === 0 ? 0 : 1)} mm`
      : `${Math.round(niceUm)} µm`;

    // Layout constants (device px)
    const tickLen = options.tickLenCss * DPR;
    const barTh   = options.barThicknessCss * DPR;
    const pad     = options.pillPaddingCss * DPR;
    const minPillW= options.minPillWidthCss * DPR;
    const gapLbl2Bar = options.gapLabelToBarCss * DPR;
    const safeBottomMargin = options.safeBottomMarginCss * DPR;

    // Measure label width
    const fontPx = options.labelFontSizeCss * DPR;
    ctx.font = `${fontPx}px system-ui,-apple-system,Segoe UI,Roboto,Arial`;
    const labelW = ctx.measureText(labelText).width;

    // Pill sizing
    const pillInnerW = Math.max(labelW, barPx);
    const pillW = Math.max(minPillW, pillInnerW + pad * 2);
    const pillH = Math.max(36 * DPR, fontPx + gapLbl2Bar + barTh + tickLen + pad * 2);

    // Place pill near bottom-left
    const screenPad = 12 * DPR;
    let pillX = 30 * DPR;
    let pillY = canvas.height - (100 * DPR) - pillH;
    pillX = Math.max(screenPad, Math.min(pillX, canvas.width  - pillW - screenPad));
    pillY = Math.max(screenPad, Math.min(pillY, canvas.height - pillH - screenPad));

    // Draw pill background
    ctx.save();
    const rx = 12 * DPR;
    ctx.fillStyle = options.pillBgRGBA;
    ctx.strokeStyle = options.pillBorderRGBA;
    ctx.lineWidth = 1 * DPR;
    ctx.beginPath();
    // rounded rect (manual for wider support)
    ctx.moveTo(pillX + rx, pillY);
    ctx.arcTo(pillX + pillW, pillY, pillX + pillW, pillY + pillH, rx);
    ctx.arcTo(pillX + pillW, pillY + pillH, pillX, pillY + pillH, rx);
    ctx.arcTo(pillX, pillY + pillH, pillX, pillY, rx);
    ctx.arcTo(pillX, pillY, pillX + pillW, pillY, rx);
    ctx.closePath();
    ctx.fill(); ctx.stroke();
    ctx.restore();

    // Content frame
    const contentX = pillX + (pillW - pillInnerW) / 2;
    const contentTop = pillY + pad;
    const safeTop = pillY + pad;
    const safeBottom = pillY + pillH - pad - safeBottomMargin;

    // Vertical positions
    let labelY = contentTop + fontPx;
    let yRule  = labelY + gapLbl2Bar + barTh / 2;
    let tickBottom = yRule + tickLen;

    if (tickBottom > safeBottom) {
      const shiftUp = tickBottom - safeBottom;
      labelY -= shiftUp;
      yRule  -= shiftUp;
      tickBottom -= shiftUp;
    }
    if (labelY - fontPx < safeTop) {
      const shiftDown = safeTop - (labelY - fontPx);
      labelY += shiftDown;
      yRule  += shiftDown;
    }

    // Center bar horizontally within inner content
    const barX = contentX + (pillInnerW - barPx) / 2;

    // Label
    ctx.fillStyle = options.strokeRGBA;
    ctx.textBaseline = "alphabetic";
    ctx.fillText(labelText, pillX + pillW / 2 - labelW / 2, labelY);

    // Baseline
    ctx.strokeStyle = options.strokeRGBA;
    ctx.lineWidth = barTh;
    ctx.beginPath();
    ctx.moveTo(barX, yRule);
    ctx.lineTo(barX + barPx, yRule);
    ctx.stroke();

    // Ticks
    const ticks = Math.max(2, Math.floor(options.majorTicks));
    const segments = ticks - 1;
    for (let i = 0; i < ticks; i++) {
      const t = i / segments; // 0..1
      const x = barX + t * barPx;
      ctx.beginPath();
      ctx.moveTo(x, yRule);
      ctx.lineTo(x, Math.min(yRule + tickLen, safeBottom));
      ctx.stroke();
    }

    // --- NEW: Minimal warning icon anchored to the ruler pill (top-right corner) ---
    const vue = window.vueApp;
    if (vue && vue.resolutionSuspect) {
      const tri = 16 * DPR;          // triangle size
      const inset = 6 * DPR;         // distance from pill edges

      // Anchor to the ruler pill's top-right corner
      const x0 = pillX + pillW - tri - inset;
      const y0 = pillY + inset;

      ctx.save();
      ctx.strokeStyle = "#d97706"; // amber-600
      ctx.fillStyle = "#fef3c7";   // amber-50
      ctx.lineWidth = 2 * DPR;

      // equilateral triangle
      ctx.beginPath();
      ctx.moveTo(x0 + tri / 2, y0);
      ctx.lineTo(x0 + tri, y0 + tri);
      ctx.lineTo(x0, y0 + tri);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      // exclamation
      ctx.fillStyle = "#92400e"; // amber-900
      ctx.font = `${11 * DPR}px system-ui,-apple-system,Segoe UI,Roboto,Arial`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("!", x0 + tri / 2, y0 + tri * 0.62);
      ctx.restore();
    }
  }

  // Wire handlers with cleanup
  const onResize = () => { resize(); redraw(); };
  const onViewport = () => redraw();
  const onFull = () => { setTimeout(onResize, 100); };

  // Initial setup
  resize();
  viewer.addHandler("update-viewport", onViewport);
  viewer.addHandler("full-screen", onFull);
  viewer.addOnceHandler("open", onResize);
  window.addEventListener("resize", onResize);

  // Return a disposer so callers can clean up
  return () => {
    try { window.removeEventListener("resize", onResize); } catch(e) {}
    try { viewer && viewer.removeHandler("update-viewport", onViewport); } catch(e) {}
    try { viewer && viewer.removeHandler("full-screen", onFull); } catch(e) {}
  };
}
