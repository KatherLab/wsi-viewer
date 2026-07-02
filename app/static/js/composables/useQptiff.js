// QPTIFF (multiplex TIFF) channel selection: toggle channels, exposure
// debouncing, DZI tile-source construction, and crossfade rebuild.
// Operates on the root Vue instance (`this`).
export const qptiffMethods = {
  toggleQptiffChannel(marker) {
    const idx = this.qptiffActiveMarkers.indexOf(marker);
    if (idx >= 0) {
      this.qptiffActiveMarkers.splice(idx, 1);
    } else {
      this.qptiffActiveMarkers = [...this.qptiffActiveMarkers, marker];
    }
    this.rebuildOsdTileSource();
  },

  onExposureChange() {
    // Debounced: rebuild tiles after a short delay
    if (this._exposureTimer) clearTimeout(this._exposureTimer);
    this._exposureTimer = setTimeout(() => {
      this._exposureTimer = null;
      this.rebuildOsdTileSource();
    }, 300);
  },

  formatExposure(val) {
    if (val === 0) return '0';
    return (val > 0 ? '+' : '') + val.toFixed(1);
  },

  async _buildQptiffTileSource(id) {
    // Build query string from active channels with display settings
    const active = this.qptiffActiveMarkers;
    const colors = active.map(m => this.qptiffChannelDisplays[m]?.color || '#ffffff');
    const mins = active.map(m => {
      const d = this.qptiffChannelDisplays[m];
      return d ? d.baseMin : 0;
    });
    const maxs = active.map(m => {
      const d = this.qptiffChannelDisplays[m];
      return d ? d.baseMin + (d.baseMax - d.baseMin) / Math.pow(2, d.exposure || 0) : 1;
    });
    const gammas = active.map(m => {
      const d = this.qptiffChannelDisplays[m];
      return d ? d.gamma : 1.0;
    });

    const qs = 'channels=' + encodeURIComponent(active.join(',')) +
               '&colors=' + encodeURIComponent(colors.join(',')) +
               '&mins=' + encodeURIComponent(mins.join(',')) +
               '&maxs=' + encodeURIComponent(maxs.join(',')) +
               '&gammas=' + encodeURIComponent(gammas.join(','));

    // Fetch DZI XML
    const resp = await fetch('/dzi/' + id + '.dzi?' + qs);
    const xmlText = await resp.text();
    const parser = new DOMParser();
    const xml = parser.parseFromString(xmlText, 'text/xml');
    const image = xml.querySelector('Image');
    const format = image.getAttribute('Format');
    const tileSize = parseInt(image.getAttribute('TileSize'));
    const overlap = parseInt(image.getAttribute('Overlap'));
    const size = xml.querySelector('Size');

    const maxLevel = this.slideMeta ? this.slideMeta.level_count - 1 : 0;

    const baseUrl = '/dzi/' + id + '_files/';
    const self = this;

    function buildQueryString() {
      const a = self.qptiffActiveMarkers;
      const cols = a.map(m => self.qptiffChannelDisplays[m]?.color || '#ffffff');
      const mi = a.map(m => {
        const d = self.qptiffChannelDisplays[m];
        return d ? d.baseMin : 0;
      });
      const ma = a.map(m => {
        const d = self.qptiffChannelDisplays[m];
        return d ? d.baseMin + (d.baseMax - d.baseMin) / Math.pow(2, d.exposure || 0) : 1;
      });
      const ga = a.map(m => {
        const d = self.qptiffChannelDisplays[m];
        return d ? d.gamma : 1.0;
      });
      return 'channels=' + encodeURIComponent(a.join(',')) +
             '&colors=' + encodeURIComponent(cols.join(',')) +
             '&mins=' + encodeURIComponent(mi.join(',')) +
             '&maxs=' + encodeURIComponent(ma.join(',')) +
             '&gammas=' + encodeURIComponent(ga.join(','));
    }

    return {
      width: parseInt(size.getAttribute('Width')),
      height: parseInt(size.getAttribute('Height')),
      tileSize: tileSize,
      tileOverlap: overlap,
      minLevel: 0,
      maxLevel: maxLevel,
      getTileUrl: function(level, x, y) {
        return baseUrl + level + '/' + x + '_' + y + '.' + format + '?' + buildQueryString();
      }
    };
  },

  async rebuildOsdTileSource() {
    if (!this.osd || !this.current) return;

    // Sequence counter so older async rebuilds don't win after rapid changes
    const seq = (this._qptiffRebuildSeq || 0) + 1;
    this._qptiffRebuildSeq = seq;

    const viewer = this.osd;
    const viewport = viewer.viewport;

    const currentZoom = viewport.getZoom(true);
    const currentCenter = viewport.getCenter(true);

    // Save old items before building new source
    const oldItems = [];
    for (let i = 0; i < viewer.world.getItemCount(); i++) {
      oldItems.push(viewer.world.getItemAt(i));
    }

    const tileSource = await this._buildQptiffTileSource(this.current);

    if (!this.osd || seq !== this._qptiffRebuildSeq) return;

    viewer.addTiledImage({
      tileSource,
      preload: true,
      opacity: 0,
      success: (event) => {
        if (!this.osd || seq !== this._qptiffRebuildSeq) {
          try { viewer.world.removeItem(event.item); } catch (e) {}
          return;
        }

        const newItem = event.item;

        // Restore viewport immediately
        viewport.zoomTo(currentZoom, null, true);
        viewport.panTo(currentCenter, true);

        // Swap on first tile-drawn event, or fallback after 500ms
        let swapped = false;
        let timer = null;
        let raf = null;

        const swap = () => {
          if (swapped) return;
          if (!this.osd || seq !== this._qptiffRebuildSeq) return;
          swapped = true;
          if (timer) clearTimeout(timer);
          try { viewer.removeHandler("tile-drawn", onDraw); } catch (e) {}

          // Quick fade-in (80ms)
          const fadeStart = performance.now();
          const step = (now) => {
            if (!this.osd || seq !== this._qptiffRebuildSeq) return;
            const t = Math.min(1, (now - fadeStart) / 80);
            newItem.setOpacity(t);
            if (t < 1) { raf = requestAnimationFrame(step); return; }
            oldItems.forEach(item => {
              if (item && item !== newItem) try { viewer.world.removeItem(item); } catch (e) {}
            });
            newItem.setOpacity(1);
          };
          raf = requestAnimationFrame(step);
        };

        // tile-drawn is only available with Canvas drawer (not WebGL), but try it
        // as an optimization for faster swaps. The 500ms fallback handles WebGL.
        const onDraw = (ev) => {
          if (ev.tiledImage === newItem) swap();
        };

        try { viewer.addHandler("tile-drawn", onDraw); } catch (e) {}

        // Fallback: swap after 500ms even if no tiles drawn yet
        timer = setTimeout(swap, 500);
      }
    });
  },
};
