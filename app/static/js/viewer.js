// OpenSeadragon construction helper. The two call sites in view() differed
// only in tileSources, so the common options live here.
//
// opts.minLevel: coarsest DZI level permitted. Set for single-level /
//   shallow-pyramid slides whose coarser tiles would decode gigabytes and
//   OOM the backend. OpenSeadragon won't request tiles below this level.
//   We also snap the initial viewport to it so the slide opens zoomed-in
//   rather than at a forbidden (blank) coarse level.
export function createOsdViewer(tileSources, opts = {}) {
  const minLevel = opts.minLevel || 0;

  const viewer = OpenSeadragon({
    id: "osd",
    tileSources,
    showNavigator: true,
    navigatorPosition: 'BOTTOM_RIGHT',
    navigatorSizeRatio: 0.15,
    showZoomControl: false,
    showHomeControl: false,
    showFullPageControl: false,
    visibilityRatio: 1,
    minZoomLevel: 0.001,
    maxZoomLevel: 40,
    background: "#ffffff",
    maxImageCacheCount: 100,
    imageLoaderLimit: 4
  });

  // Tiles load as <img> requests (same-origin, cookie sent automatically). If
  // the session expired mid-view, tile fetches 401 — redirect to login instead
  // of silently rendering broken tiles.
  viewer.addHandler('openTileFailed', (evt) => {
    const status = evt?.tile?.request?.status ?? evt?.event?.target?.status;
    if (status === 401) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = '/login?next=' + next;
    }
  });

  if (minLevel > 0) {
    viewer.addHandler('open', () => {
      try {
        const tiled = viewer.world.getItemAt(0);
        // Forbid requesting coarser tiles than the safe floor.
        if (typeof tiled.setMinLevel === 'function') {
          tiled.setMinLevel(minLevel);
        } else {
          tiled.minLevel = minLevel;
        }
        // Snap to the floor so the user lands on a rendered view rather than
        // the blank coarse level. imageToViewportZoom converts an image-space
        // zoom (1 = 1:1 pixels) to the viewport zoom OSD uses.
        // At DZI level L, image zoom ≈ 2^(L - maxLevel). Clamp via the tiled image.
        const maxLevel = tiled.maxLevel || (tiled.source ? tiled.source.maxLevel : 0);
        const imgZoom = Math.pow(2, minLevel - maxLevel);
        const vpZoom = tiled.imageToViewportZoom(imgZoom);
        viewer.viewport.zoomTo(vpZoom, null, true);
      } catch (e) {
        // Non-fatal: worst case the user just starts at the default zoom.
        console.warn('minLevel open handler failed:', e);
      }
    });
  }

  return viewer;
}
