// OpenSeadragon construction helper. The two call sites in view() differed
// only in tileSources, so the common options live here.
export function createOsdViewer(tileSources) {
  return OpenSeadragon({
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
}
