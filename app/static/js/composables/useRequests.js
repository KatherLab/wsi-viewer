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
        credentials: 'same-origin',  // send the signed session cookie
        headers: {'X-Priority': priority.toString()}
      });

      // Auth: a 401 means no/invalid session — bounce to the login page.
      // We do this only for API/data calls, not for the index page itself.
      if (response.status === 401 && !url.startsWith('/login')) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = '/login?next=' + next + '&reason=auth';
        // Throw so callers stop processing a response they'll never read.
        throw new Error('unauthorized');
      }

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
