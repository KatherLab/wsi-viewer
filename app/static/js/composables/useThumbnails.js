// Thumbnail loading with a queue + strict viewport priority.
// Operates on the root Vue instance (`this`); depends on requestMethods.
export const thumbnailMethods = {
  loadThumbnail(slide) {
    if (!slide || slide.thumbUrl || slide.thumbError || this.loadingSlides.has(slide.id)) return;
    this.loadingSlides.add(slide.id);
    // priority based on distance to viewport center
    const slideIndex = this.visibleSlides.indexOf(slide);
    const viewportCenter = (this.visibleRange.start + this.visibleRange.end) / 2;
    const distance = Math.abs(slideIndex - viewportCenter);
    const priority = Math.max(0, 1000 - distance * 10);

    // enqueue
    this.thumbQueue.push({ slide, priority });
    // highest priority first
    this.thumbQueue.sort((a,b) => b.priority - a.priority);
    this._drainThumbQueue();
  },

  async _drainThumbQueue() {
    while (this.thumbInFlight < this.thumbSlots && this.thumbQueue.length) {
      const { slide, priority } = this.thumbQueue.shift();
      this._fetchThumb(slide, priority).catch(()=>{});
    }
  },

  async _fetchThumb(slide, priority) {
    if (!slide || slide.thumbUrl || slide.thumbError) return;
    slide.loading = true;
    this.thumbInFlight++;

    const controller = new AbortController();
    const requestId = `thumb-${slide.id}`;
    this.pendingRequests.set(requestId, {controller, priority, url: `/api/thumb/${slide.id}`});

    try {
      const timeoutId = setTimeout(() => controller.abort(), 8000);
      const resp = await fetch(`/api/thumb/${slide.id}`, {
        signal: controller.signal,
        headers: {'X-Priority': String(priority)}
      });
      clearTimeout(timeoutId);

      if (resp.ok) {
        const blob = await resp.blob();
        if (this._unmounted) {
          return;
        }
        this.revokeThumb(slide);
        slide.thumbUrl = URL.createObjectURL(blob);
      } else {
        slide.thumbError = true;
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        slide.thumbError = true;
      }
    } finally {
      slide.loading = false;
      this.pendingRequests.delete(requestId);
      this.loadingSlides.delete(slide.id);
      this.thumbInFlight = Math.max(0, this.thumbInFlight - 1); // clamp so it never goes negative
      this._drainThumbQueue();
    }
  },
};
