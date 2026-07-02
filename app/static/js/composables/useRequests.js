// Request management: AbortController-based fetch wrapper, cancellation,
// and blob URL lifecycle. All methods operate on the root Vue instance (`this`).
export const requestMethods = {
  async makeRequest(url, priority = 0) {
    const controller = new AbortController();
    const requestId = Math.random().toString(36);

    this.pendingRequests.set(requestId, {controller, priority, url});
    this.activeRequests++;

    try {
      const response = await fetch(url, {
        signal: controller.signal,
        headers: {'X-Priority': priority.toString()}
      });
      return response;
    } finally {
      this.pendingRequests.delete(requestId);
      this.activeRequests--;
    }
  },

  cancelAllRequests() {
    for (const [id, req] of this.pendingRequests) {
      req.controller.abort();
    }
    this.pendingRequests.clear();
    this.requestQueue = [];
    this.loadingSlides.clear();
    this.activeRequests = 0;

    // Also clear queued thumbs and reset counters to avoid a stuck queue
    this.thumbQueue = [];
    this.thumbInFlight = 0; // <— important reset
  },

  cancelRequestsOutsideViewport() {
    // Cancel requests for items not in current viewport
    const inViewport = new Set(
      this.visibleSlides
        .slice(this.visibleRange.start, this.visibleRange.end)
        .map(s => s.id)
    );

    for (const [id, req] of this.pendingRequests) {
      if (req.url.includes('/api/thumb/')) {
        const slideId = req.url.split('/api/thumb/')[1];
        if (!inViewport.has(slideId)) {
          req.controller.abort();
          this.pendingRequests.delete(id);
          this.loadingSlides.delete(slideId);
        }
      }
    }
  },

  // Blob URL cleanup
  revokeThumb(slide){
    if (slide && slide.thumbUrl) {
      URL.revokeObjectURL(slide.thumbUrl);
      slide.thumbUrl = null;
    }
  },
};
