// Viewport tracking: IntersectionObserver lifecycle + scroll-driven virtual range.
// Operates on the root Vue instance (`this`).
export const viewportMethods = {
  ensureObserver() {
    // Rebuild the IntersectionObserver with the CURRENT grid root
    if (this.viewportObserver) {
      try { this.viewportObserver.disconnect(); } catch(e) {}
      this.viewportObserver = null;
    }
    const root = this.$refs.gridContainer || null;

    if ('IntersectionObserver' in window) {
      this.viewportObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            const slideId = entry.target.dataset.slideId;
            const slide = this.visibleSlides.find(s => s.id === slideId);
            if (slide && !slide.thumbUrl && !slide.thumbError) {
              this.loadThumbnail(slide);
            }
          }
        });
      }, {
        root,
        rootMargin: '200px',
        threshold: 0.01,
      });

      if (this._toObserve.length) {
        this._toObserve.forEach(el => this.viewportObserver.observe(el));
        this._toObserve = [];
      }
    }
  },

  // Smart viewport-based loading
  updateViewport() {
    if (this.viewMode !== 'grid' || !this.$refs.gridContainer) return;

    const container = this.$refs.gridContainer;
    const scrollTop = container.scrollTop;
    const clientHeight = container.clientHeight;

    // Calculate visible range
    const itemsPerRow = Math.floor(container.clientWidth / 260) || 4;
    const rowHeight = 280;
    const firstRow = Math.floor(scrollTop / rowHeight);
    const lastRow = Math.ceil((scrollTop + clientHeight) / rowHeight);

    const newStart = Math.max(0, firstRow * itemsPerRow);
    const newEnd = Math.min(this.visibleSlides.length, (lastRow + 1) * itemsPerRow);

    // Only update if range changed significantly
    if (Math.abs(newStart - this.visibleRange.start) > 2 ||
        Math.abs(newEnd - this.visibleRange.end) > 2) {
      this.visibleRange = {start: newStart, end: newEnd};

      // Cancel requests outside new viewport
      this.cancelRequestsOutsideViewport();

      // Hint-load the currently visible thumbnails (IntersectionObserver will also trigger)
      for (let i = newStart; i < newEnd; i++) {
        const slide = this.visibleSlides[i];
        if (slide && !slide.thumbUrl && !slide.thumbError) {
          this.loadThumbnail(slide);
        }
      }
    }
  },

  handleScroll(event) {
    if (this.viewMode !== 'grid') return;

    // Immediate viewport update
    this.updateViewport();

    // Debounced actions
    if (this.scrollDebounceTimer) {
      clearTimeout(this.scrollDebounceTimer);
    }

    this.scrollDebounceTimer = setTimeout(() => {
      const container = event.target;
      const scrollHeight = container.scrollHeight;
      const scrollTop = container.scrollTop;
      const clientHeight = container.clientHeight;

      // Load more slides if near bottom
      if (scrollTop + clientHeight > scrollHeight - 200) {
        this.loadMoreSlides();
      }
    }, 100);
  },

  async waitForGridRoot(retries = 20) {
    // Wait until this.$refs.gridContainer is available after view switches.
    // 20 * 16ms ≈ ~320ms worst-case.
    while (retries-- > 0) {
      await this.$nextTick();
      if (this.$refs.gridContainer) return true;
      await new Promise(r => setTimeout(r, 16));
    }
    return !!this.$refs.gridContainer;
  },

  forcePrimeThumbnails(count = 24) {
    // Fire some initial requests even if the observer or viewport math lags
    const end = Math.min(this.visibleSlides.length, count);
    for (let i = 0; i < end; i++) {
      const s = this.visibleSlides[i];
      if (s && !s.thumbUrl && !s.thumbError) this.loadThumbnail(s);
    }
  },

  loadMoreSlides(){
    // Grid view handles all slides at once
    // Just update viewport
    this.updateViewport();
  },
};
