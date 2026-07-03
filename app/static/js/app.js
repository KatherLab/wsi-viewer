import { createApp } from '/static/js/vendor/vue.esm-browser.prod.js';
import { Folder } from '/static/js/components/folder.js';
import { observeVisibleDirective } from '/static/js/directives/observeVisible.js';
import { createOsdViewer } from '/static/js/viewer.js';
import { attachRuler } from '/static/js/ruler.js';
import { requestMethods } from '/static/js/composables/useRequests.js';
import { thumbnailMethods } from '/static/js/composables/useThumbnails.js';
import { viewportMethods } from '/static/js/composables/useViewport.js';
import { measureMethods } from '/static/js/composables/useMeasure.js';
import { qptiffMethods } from '/static/js/composables/useQptiff.js';

const app = createApp({
  components: { Folder },
  data(){
    return {
      // Tree & navigation
      trees:[],
      q:"",
      selectedPath:null,

      // Slides
      slides:[],
      filteredSlides:[],
      visibleSlides:[],
      slideSearch:"",
      dirDenied: false,   // true when /api/dir returned 403 (no list permission)
      dirLoaded: false,   // true once a directory has been loaded (even if empty)

      // View modes
      viewMode:'grid',
      currentPage:1,
      itemsPerPage:50,

      // Viewer
      osd:null,
      current:null,
      mpp:null,
      hasScale:false,
      showSidebar:true,
      slideMeta:null,
      zoomFloor:0,            // min safe DZI level (>0 for single-level slides)
      associatedImages:[],
      qptiffMarkers: [],      // all available markers
      qptiffMarkerColors: {}, // active marker → color mapping
      qptiffActiveMarkers: [], // currently selected marker names (subset of qptiffMarkers)
      qptiffAllMarkers: [],   // all available markers (for checkbox rendering)
      qptiffChannelDisplays: {}, // marker -> {color, min, max, gamma, exposure}

      // UI state
      isFullscreen:false,
      logoUrl:null,
      debug:false,

      // Keyboard help overlay
      showHelp:false,

      // Whether we're restoring state from a share URL (suppresses
      // history.replaceState noise during initial load)
      _restoringFromUrl:false,

      // Measurement tool
      measuring:false,
      measurements:[],          // [{id, lengthUm, color, x1,y1,x2,y2}] in image px
      measLive:null,            // {x,y,text} live readout in screen px
      measHover:null,           // {x,y,text} tooltip when hovering a stored measurement
      measCanvas:null,
      measCtx:null,
      measUnits:'µm',
      measSeq:0,
      measPalette:['#2563eb','#ef4444','#16a34a','#d97706','#9333ea','#0891b2'],

      // Raw properties
      slideProperties:[],
      _propsLoaded:false,

      // Associated image modal
      imageModal:{open:false, index:0, zoom:1, tx:0, ty:0},

      // Toast
      toast:{show:false, msg:''},
      _toastTimer:null,

      // Request management - critical for performance
      activeRequests:0,
      pendingRequests: new Map(),
      requestQueue: [],
      loadingSlides: new Set(),

      // Auth: current logged-in user (null when auth disabled / unknown)
      currentUser: null,

      // Thumbnail queue/concurrency control
      thumbSlots: 4,
      thumbInFlight: 0,
      thumbQueue: [], // [{ slide, priority }]

      // Viewport tracking
      visibleRange:{start:0, end:0},
      lastScrollTime:0,
      scrollDebounceTimer:null,
      viewportObserver:null,

      // Elements awaiting observer creation
      _toObserve: [],

      // ✅ P3: Guard flag for blob URL leak prevention
      _unmounted: false
    }
  },

  computed:{
    resolutionSuspect(){
      // Guard: need metadata available
      const m = this.slideMeta;
      if (!m || !Number.isFinite(m?.mpp_x)) return false;

      const x = m.mpp_x;

      // Heuristics for WSI (typical: ~0.25 µm/px @40x; ~0.5 @20x; ~1.0 @10x; ~2.0 @5x)
      // Flag anything clearly out of band:
      //  - absurdly small: < 0.01 µm/px (nanoscale) -> broken
      //  - very large:     > 5 µm/px (unlikely for diagnostic WSI)
      //  - extremely large: > 50 µm/px (definitely broken)
      return (x < 0.01) || (x > 5);
    },
    resolutionWarning(){
      const m = this.slideMeta;
      if (!m || !Number.isFinite(m?.mpp_x)) return "";
      const x = m.mpp_x;

      if (x < 0.01) {
        return `Unrealistic resolution (${x.toFixed(3)} µm/px). Value is too small for WSI — metadata likely incorrect.`;
      }
      if (x > 50) {
        return `Unrealistic resolution (${x.toFixed(3)} µm/px). Value is extremely large — metadata likely incorrect.`;
      }
      if (x > 5) {
        return `Suspicious resolution (${x.toFixed(3)} µm/px). Much larger than typical WSI — metadata may be incorrect.`;
      }
      return "";
    },


    derivedMag(){
      // Infer approximate objective magnification from pixel size.
      // Reference: 0.25 µm/px ≈ 40×, 0.5 ≈ 20×, 1.0 ≈ 10× → mag = 10 / mpp
      const m = this.slideMeta;
      if (!m || !Number.isFinite(m?.mpp_x) || m.mpp_x <= 0 || m.objective_power) return null;
      const est = 10 / m.mpp_x;
      if (est < 1 || est > 100) return null;
      return Math.round(est);
    },

    canNavPrev(){ return this.current && this.slideIndexInDir > 0; },
    canNavNext(){ return this.current && this.slideIndexInDir < this.filteredSlides.length - 1; },
    slideIndexInDir(){
      if (!this.current || !this.filteredSlides.length) return -1;
      return this.filteredSlides.findIndex(s => s.id === this.current);
    },
    imageModalUrl(){ const i = this.imageModal.index; return (this.associatedImages[i] || {}).url || ''; },
    imageModalName(){ const i = this.imageModal.index; return (this.associatedImages[i] || {}).name || ''; },

    filteredTrees(){
      if(!this.q) return this.trees
      const term = this.q.toLowerCase()
      const match = (n)=> n.name.toLowerCase().includes(term) || (n.children||[]).some(match)
      const clone = (n)=> ({...n, children:(n.children||[]).map(clone).filter(match)})
      return this.trees.map(clone).filter(match)
    },

    modeClass(){ return this.current ? 'mode-viewer' : 'mode-grid' },

    paginatedSlides(){
      const start = (this.currentPage - 1) * this.itemsPerPage;
      return this.filteredSlides.slice(start, start + this.itemsPerPage);
    },

    totalPages(){
      return Math.ceil(this.filteredSlides.length / this.itemsPerPage);
    },

    pageNumbers(){
      const pages = [];
      const total = this.totalPages;
      const current = this.currentPage;

      if(total <= 7) {
        for(let i = 1; i <= total; i++) pages.push(i);
      } else {
        if(current <= 3) {
          for(let i = 1; i <= 5; i++) pages.push(i);
          pages.push('...');
          pages.push(total);
        } else if(current >= total - 2) {
          pages.push(1);
          pages.push('...');
          for(let i = total - 4; i <= total; i++) pages.push(i);
        } else {
          pages.push(1);
          pages.push('...');
          for(let i = current - 1; i <= current + 1; i++) pages.push(i);
          pages.push('...');
          pages.push(total);
        }
      }
      return pages;
    }
  },

  watch:{
    viewMode(v){ this.savePref('viewMode', v); },
    showSidebar(v){ this.savePref('showSidebar', v); },
    itemsPerPage(v){ this.savePref('itemsPerPage', v); },
  },

  methods: {
    // Auth: sign out and return to the login page.
    async logout(){
      try { await fetch('/api/logout', {method: 'POST', credentials: 'same-origin'}); }
      catch(e) { /* ignore — proceed to redirect anyway */ }
      window.location.href = '/login';
    },

    // Utility methods
    prettySize(b){
      if(!b && b!==0) return "";
      const u=["B","KB","MB","GB","TB"];
      let i=0;
      while(b>1024 && i<u.length-1){ b/=1024; i++ }
      return b.toFixed(b<10 && i>0 ? 1 : 0) + " " + u[i]
    },
    baseName(name){ const i=name.lastIndexOf("."); return i>0 ? name.slice(0,i) : name },
    extName(name){ const i=name.lastIndexOf("."); return i>0 ? name.slice(i+1) : "" },
    extUpper(name){ return this.extName(name).toUpperCase() },
    formatDate(ts){
      if(!ts) return "";
      const d = new Date(ts * 1000);
      return d.toLocaleDateString('en-US', {year:'numeric', month:'short', day:'numeric'});
    },
    cssColor(name){
      const map = { red:"#ef4444", green:"#22c55e", blue:"#3b82f6", gray:"#9ca3af",
                    yellow:"#eab308", cyan:"#06b6d4", magenta:"#d946ef", orange:"#f97316",
                    white:"#ffffff", black:"#111827" };
      return map[name] || name;
    },

    // Data loading
    async loadTrees(){
      try {
        const response = await this.makeRequest("/api/tree");
        if (response.ok) {
          this.trees = await response.json();
        }
      } catch (e) {
        console.error('Failed to load trees:', e);
      }
    },

    selectDir(path){
      // free blobs before switching
      this.visibleSlides.forEach(s => this.revokeThumb(s));
      this.cancelAllRequests(); // Cancel everything when changing dirs
      this.selectedPath = path;
      // Record the directory in the URL (clear any slide/viewport params)
      this.pushSlideUrl(null, null, false);
      this.openDir(path);
    },

    async openDir(path){
      this.cancelAllRequests();
      this.current = null;
      // free existing blobs and reset
      this.visibleSlides.forEach(s => this.revokeThumb(s));
      this.visibleSlides = [];
      this.dirDenied = false;
      this.dirLoaded = false;

      try {
        const response = await this.makeRequest("/api/dir?" + new URLSearchParams({path}));
        if (response.status === 403) {
          // Per-user ACL denial: this directory is not listable for this user.
          this.slides = [];
          this.filteredSlides = [];
          this.dirDenied = true;
          this.showToast(`No access to this directory`);
          return;
        }
        if (response.ok) {
          this.slides = await response.json();
          this.filteredSlides = [...this.slides];
          this.dirLoaded = true;
          this.currentPage = 1;
          this.slideSearch = "";
          this.dirDenied = false;
          this.initializeView();
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          console.error('Failed to load directory:', e);
        }
      }
    },

    filterSlides(){
      this.cancelAllRequests();
      // revoke previous page blobs
      this.visibleSlides.forEach(s => this.revokeThumb(s));
      const term = this.slideSearch.toLowerCase();
      if(!term) {
        this.filteredSlides = [...this.slides];
      } else {
        this.filteredSlides = this.slides.filter(s =>
          s.name.toLowerCase().includes(term)
        );
      }
      this.currentPage = 1;
      this.initializeView();
    },

    initializeView(){
      if (this.viewMode === 'grid') {
        // Rebuild slide list
        this.visibleSlides = this.filteredSlides.map(s => ({
          ...s,
          loading: false,
          thumbUrl: null,
          thumbError: false
        }));

        // Do the work after DOM is rendered
        this.$nextTick(async () => {
          const ok = await this.waitForGridRoot();
          if (!ok) {
            console.debug('[WSI] gridContainer ref not ready after retries; forcing prime as fallback');
            this.forcePrimeThumbnails();
            return;
          }

          // Now we definitely have the current grid root
          this.ensureObserver();

          // Two passes: immediate + post-layout
          this.updateViewport();
          setTimeout(() => this.updateViewport(), 0);

          // Safety net: if nothing in flight, prime a few
          if (this.thumbInFlight === 0 && this.pendingRequests.size === 0) {
            this.forcePrimeThumbnails();
          }
        });
      } else {
        // List view - build visibleSlides from current page so thumbnails
        // are loaded onto the same objects Vue is rendering.
        this.visibleSlides = this.paginatedSlides.map(s => ({
          ...s,
          loading: false,
          thumbUrl: null,
          thumbError: false
        }));

        this.$nextTick(() => {
          this.visibleSlides.forEach(slide => {
            if (!slide.thumbUrl && !slide.thumbError) {
              this.loadThumbnail(slide);
            }
          });
        });
      }
    },

    switchViewMode(mode){
      // revoke existing blobs & cancel in-flight
      this.visibleSlides.forEach(s => this.revokeThumb(s));
      this.cancelAllRequests();
      this.viewMode = mode;
      this.initializeView();
    },


    goToPage(page){
      // revoke blobs from current page
      this.paginatedSlides.forEach(s => this.revokeThumb(s));
      this.cancelAllRequests();
      this.currentPage = page;
      this.initializeView();
    },

    // Viewer methods
    async view(id){
      // Cancel thumbnails when opening viewer
      this.cancelAllRequests();
      this.current = id;
      // Record a fresh history entry for this slide (clears any prior viewport params)
      this.pushSlideUrl(id, null, false);

      try {
        const metaResp = await this.makeRequest("/api/meta/" + id, 1000); // High priority
        if (metaResp.ok) {
          this.slideMeta = await metaResp.json();
          this.hasScale = Number.isFinite(this.slideMeta?.mpp_x) && this.slideMeta.mpp_x > 0;
          this.mpp = this.hasScale ? this.slideMeta.mpp_x : null;
        } else {
          this.hasScale = false;
          this.mpp = null;
        }
      } catch (e) {
        console.error('Failed to load metadata:', e);
        this.hasScale = false;
        this.mpp = null;
      }


      this.associatedImages = [];
      this.qptiffMarkers = [];
      this.qptiffMarkerColors = {};
      this.qptiffActiveMarkers = [];
      this.qptiffAllMarkers = [];
      this.qptiffChannelDisplays = {};
      // Reset per-slide state
      this.slideProperties = [];
      this._propsLoaded = false;
      this.measurements = [];
      this.measLive = null;
      this._measCurrent = null;
      if (this.measuring) this.detachMeasure();

      // Load QPTIFF marker info (only for multiplex TIFFs)
      try {
        const markerResp = await this.makeRequest("/api/markers/" + id, 800);
        if (markerResp.ok) {
          const markerData = await markerResp.json();
          if (markerData.backend === "mxtifffile" && markerData.markers && markerData.markers.length) {
            this.qptiffMarkers = markerData.markers;
            this.qptiffMarkerColors = markerData.marker_colors || {};
            this.qptiffAllMarkers = markerData.markers;
            // Initial active set from default_markers (up to 3), or fall back to all markers
            if (markerData.default_markers && markerData.default_markers.length) {
              this.qptiffActiveMarkers = [...markerData.default_markers];
            } else {
              this.qptiffActiveMarkers = Object.keys(markerData.marker_colors || {});
            }
            // Initialize channel display settings
            const displays = markerData.channel_displays || {};
            for (const marker of markerData.markers) {
              const d = displays[marker] || { color: '#ffffff', min: 0, max: 1, gamma: 1.0 };
              this.qptiffChannelDisplays[marker] = {
                color: d.color,
                min: d.min,
                max: d.max,
                gamma: d.gamma,
                baseMin: d.min,
                baseMax: d.max,
                exposure: 0,
              };
            }
          }
        }
      } catch(e) {
        // Not a QPTIFF or request failed — that's fine
      }

      // Load associated images
      try {
        const assocResp = await this.makeRequest("/api/associated/" + id, 500);
        if(assocResp.ok) {
          const assocList = await assocResp.json();
          this.associatedImages = assocList.map(name => ({
            name: name,
            url: `/api/associated/${id}/${name}`
          }));
        }
      } catch(e) {
        console.log("Could not load associated images:", e);
      }

      // Determine if this is a QPTIFF slide (has markers)
      const isQptiff = this.qptiffAllMarkers.length > 0;

      if(this.osd){ this.osd.destroy(); this.osd=null }

      // Single-level / shallow-pyramid slides: the backend reports the coarsest
      // safe DZI level. Coarser tiles would decode gigabytes and OOM, so we
      // forbid zooming out below it.
      const minLevel = this.slideMeta?.min_safe_level || 0;
      this.zoomFloor = minLevel;

      if (isQptiff) {
        // For QPTIFF, use custom tile source with channel query params
        const tileSource = await this._buildQptiffTileSource(id);
        this.osd = createOsdViewer(tileSource, { minLevel });
      } else {
        this.osd = createOsdViewer("/dzi/" + id + ".dzi", { minLevel });
      }

      // Attach the ruler only if slide scale is known
      this.$nextTick(() => {
        const canvas = document.getElementById('ruler');
        // Clean any previous hooks just in case
        if (this._rulerDetach) { try { this._rulerDetach(); } catch(e) {} this._rulerDetach = null; }
        if (this._measViewportHandler) { try { this.osd && this.osd.removeHandler('update-viewport', this._measViewportHandler); } catch(e){} }
        this._measViewportHandler = () => { if (this.measuring || this.measurements.length) this.redrawMeasurements(); };
        if (this.osd) this.osd.addHandler('update-viewport', this._measViewportHandler);

        // Debounced reflect of pan/zoom into the URL (replaceState, no history spam)
        if (this._urlViewportTimer) { clearTimeout(this._urlViewportTimer); this._urlViewportTimer = null; }
        this._urlViewportHandler = () => {
          if (this._urlViewportTimer) clearTimeout(this._urlViewportTimer);
          this._urlViewportTimer = setTimeout(() => this.updateViewportInUrl(), 500);
        };
        if (this.osd) this.osd.addHandler('viewport-changed', this._urlViewportHandler);

        // Hover tooltip over stored measurements (works in normal view too)
        if (this._measHoverEl) { try { this._measHoverEl.removeEventListener('pointermove', this._measHoverHandler); } catch(e){} this._measHoverEl = null; }
        this._measHoverHandler = (ev) => this._hoverMeasure(ev);
        this._measHoverLeave = () => { this.measHover = null; };
        this._measHoverEl = this.osd.canvas;
        this._measHoverEl.addEventListener('pointermove', this._measHoverHandler);
        this._measHoverEl.addEventListener('pointerleave', this._measHoverLeave);

        if (this.hasScale && this.mpp) {
          if (canvas) canvas.style.display = '';
          const detach = attachRuler(this.osd, this.mpp);
          this._rulerDetach = detach; // save disposer for cleanup
        } else {
          if (canvas) {
            const ctx = canvas.getContext('2d');
            ctx && ctx.clearRect(0,0,canvas.width,canvas.height);
            canvas.style.display = 'none';
          }
        }

        // Restore zoom/center if navigated from prev/next, or from a share URL
        const pv = this._pendingNav || this._shareViewport || null;
        this._pendingNav = null;
        this._shareViewport = null;
        if (pv && this.osd) {
          if (pv.zoom != null) this.osd.viewport.zoomTo(pv.zoom, null, true);
          const cx = pv.x != null ? pv.x : (pv.center ? pv.center.x : null);
          const cy = pv.y != null ? pv.y : (pv.center ? pv.center.y : null);
          if (cx != null && cy != null) this.osd.viewport.panTo(new OpenSeadragon.Point(cx, cy), true);
        }
      });

    },

    closeViewer(){
      this.cancelAllRequests();
      this.detachMeasure();
      if (this._measHoverEl) {
        try { this._measHoverEl.removeEventListener('pointermove', this._measHoverHandler); } catch(e){}
        try { this._measHoverEl.removeEventListener('pointerleave', this._measHoverLeave); } catch(e){}
        this._measHoverEl = null;
      }
      this.measHover = null;
      this.measuring = false;
      this.measurements = [];
      if (this._rulerDetach) { try { this._rulerDetach(); } catch(e) {} this._rulerDetach = null; }
      if (this._measViewportHandler) { this._measViewportHandler = null; }
      if (this._urlViewportHandler) { this._urlViewportHandler = null; }
      if (this._urlViewportTimer) { clearTimeout(this._urlViewportTimer); this._urlViewportTimer = null; }
      if (this.osd) { this.osd.destroy(); this.osd = null; }
      this.current = null;
      this.slideMeta = null;
      this.associatedImages = [];
      this.qptiffMarkers = [];
      this.qptiffMarkerColors = {};
      this.qptiffActiveMarkers = [];
      this.qptiffAllMarkers = [];
      this.qptiffChannelDisplays = {};
      this.hasScale = false;
      this.mpp = null;

      const canvas = document.getElementById('ruler');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        ctx && ctx.clearRect(0,0,canvas.width,canvas.height);
        canvas.style.display = 'none';
      }

      this.isFullscreen = false;
      if (document.fullscreenElement) {
        document.exitFullscreen();
      }

      // Clear the shared slide from the URL
      this.pushSlideUrl(null, null, false);

      // Rebuild grid after the viewer has fully unmounted
      this.$nextTick(async () => {
        // Wait for the grid DOM to exist again
        const ok = await this.waitForGridRoot();
        if (!ok) console.debug('[WSI] gridContainer ref not ready after closing viewer');
        this.initializeView(); // initializeView itself will ensureObserver + updateViewport + prime
      });
    },


    resetView(){
      if(!this.osd) return;
      this.osd.viewport.setRotation(0);
      this.osd.viewport.goHome(true);
    },

    toggleFS(){
      const viewer = document.getElementById('viewer');
      if (!document.fullscreenElement) {
        viewer.requestFullscreen();
        this.isFullscreen = true;
      } else {
        document.exitFullscreen();
        this.isFullscreen = false;
      }
    },

    handleKey(e){
      // Don't hijack typing in inputs / search fields
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      // Help overlay (? / Escape) works everywhere
      if (this.showHelp) {
        if (e.key === 'Escape' || e.key === '?') { this.closeHelp(); e.preventDefault(); }
        return;
      }
      if (e.key === '?' || (e.key === '/' && e.shiftKey)) { this.toggleHelp(); e.preventDefault(); return; }
      // Associated-image modal takes precedence
      if (this.imageModal.open) {
        if (e.key === 'Escape') this.closeImageModal();
        else if (e.key === 'ArrowLeft') this.navImageModal(-1);
        else if (e.key === 'ArrowRight') this.navImageModal(1);
        return;
      }
      if (!this.current) return; // shortcuts apply in viewer
      switch(e.key){
        case 'Escape': this.closeViewer(); break;
        case '+': case '=': if(this.osd) this.osd.viewport.zoomBy(1.2); break;
        case '-': case '_': if(this.osd) this.osd.viewport.zoomBy(1/1.2); break;
        case '0': this.resetView(); break;
        case 'f': case 'F': this.toggleFS(); break;
        case 'm': case 'M': this.toggleMeasure(); break;
        case '[': this.navSlide(-1); break;
        case ']': this.navSlide(1); break;
        case 'ArrowLeft': if(e.shiftKey) this.navSlide(-1); break;
        case 'ArrowRight': if(e.shiftKey) this.navSlide(1); break;
        default: return;
      }
      e.preventDefault();
    },

    viewFullImage(url){ this.openImageModal(0); },

    // ---------- Associated image modal ----------
    openImageModal(i){
      this.imageModal = {open:true, index:i, zoom:1, tx:0, ty:0};
    },
    closeImageModal(){ this.imageModal.open = false; },
    navImageModal(d){
      if (this.associatedImages.length <= 1) return;
      const n = this.associatedImages.length;
      this.imageModal.index = (this.imageModal.index + d + n) % n;
      this.imageModal.zoom = 1; this.imageModal.tx = 0; this.imageModal.ty = 0;
    },
    zoomImageModal(e){
      const z = this.imageModal.zoom * (e.deltaY < 0 ? 1.15 : 1/1.15);
      this.imageModal.zoom = Math.max(1, Math.min(8, z));
    },

    // ---------- Prev / Next slide ----------
    navSlide(d){
      const idx = this.slideIndexInDir;
      if (idx < 0) return;
      const next = idx + d;
      if (next < 0 || next >= this.filteredSlides.length) return;
      // Remember position to try restoring after load
      this._pendingNav = {zoom: this.osd ? this.osd.viewport.getZoom(true) : null,
                          center: this.osd ? this.osd.viewport.getCenter(true) : null};
      this.view(this.filteredSlides[next].id);
    },

    // ---------- Properties ----------
    async toggleProperties(){
      if (this._propsLoaded || this.slideProperties.length) return;
      try {
        const resp = await this.makeRequest('/api/properties/' + this.current, 300);
        if (resp.ok) {
          const data = await resp.json();
          this.slideProperties = Object.entries(data.properties || {}).sort((a,b)=>a[0].localeCompare(b[0]));
          this._propsLoaded = true;
        }
      } catch(e) { console.error('Properties load failed', e); }
    },

    // ---------- Copy / Toast ----------
    async copyText(text, evt){
      // navigator.clipboard is unavailable on non-secure (http) origins —
      // falls back to a hidden textarea + execCommand('copy').
      const ok = await this._copyTextRaw(text);
      this.showToast(ok ? 'Copied' : 'Copy failed');
      if (ok && evt && evt.currentTarget){ evt.currentTarget.classList.add('copied'); setTimeout(()=>evt.currentTarget.classList.remove('copied'), 900); }
    },
    showToast(msg){
      this.toast = {show:true, msg};
      if (this._toastTimer) clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(()=>{ this.toast.show = false; }, 1600);
    },

    shortPath(path){
      if (!path) return '';
      // Show last 2 segments + ellipsis if long
      const parts = path.split('/');
      if (path.length < 48) return path;
      return '…/' + parts.slice(-2).join('/');
    },
    shortHash(h){
      if (!h) return '';
      return h.length > 16 ? h.slice(0, 8) + '…' + h.slice(-8) : h;
    },

    // ---------- Share links / URL state ----------
    // Reflect the current directory (?d=<path>) and slide (?s=<id>, plus
    // optional viewport ?z/&x/&y) in the URL so it can be bookmarked / shared.
    // slide_id is a deterministic hash of the absolute path, so no extra
    // backend support is needed.
    buildShareUrl(slideId, viewport){
      const u = new URL(window.location.href);
      // Current directory (absolute path; URLSearchParams encodes it)
      if (this.selectedPath) u.searchParams.set('d', this.selectedPath);
      else u.searchParams.delete('d');
      // Slide + viewport
      if (slideId) {
        u.searchParams.set('s', slideId);
        if (viewport && viewport.zoom != null) u.searchParams.set('z', viewport.zoom.toFixed(4));
        if (viewport && viewport.x != null) u.searchParams.set('x', viewport.x.toFixed(4));
        if (viewport && viewport.y != null) u.searchParams.set('y', viewport.y.toFixed(4));
      } else {
        for (const k of ['s','z','x','y']) u.searchParams.delete(k);
      }
      return u;
    },
    // Update the URL bar without polluting history on every zoom/pan.
    // A single history entry per slide; subsequent viewport tweaks replaceState.
    pushSlideUrl(slideId, viewport, replace){
      if (this._restoringFromUrl) return;
      const u = this.buildShareUrl(slideId, viewport);
      const href = u.toString();
      if (href === window.location.href) return;
      try {
        if (replace) window.history.replaceState({}, '', href);
        else window.history.pushState({}, '', href);
      } catch(e) { /* security-restricted origin (file://) — ignore */ }
    },
    updateViewportInUrl(){
      if (!this.current || !this.osd) return;
      const vp = this.osd.viewport;
      this.pushSlideUrl(this.current, {
        zoom: vp.getZoom(true),
        x: vp.getCenter(true).x,
        y: vp.getCenter(true).y,
      }, true);
    },
    async copyShareLink(evt){
      const url = this.buildShareUrl(this.current, null).toString();
      const ok = await this._copyTextRaw(url);
      this.showToast(ok ? 'Share link copied' : 'Copy failed');
      if (ok && evt && evt.currentTarget){ evt.currentTarget.classList.add('copied'); setTimeout(()=>evt.currentTarget.classList.remove('copied'), 900); }
    },
    async _copyTextRaw(text){
      // Shared clipboard helper (with http-origin fallback). Kept separate so
      // copyShareLink can report success without double-firing the toast.
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          return true;
        }
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed'; ta.style.left = '-9999px';
        ta.setAttribute('readonly', '');
        document.body.appendChild(ta); ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch(e) { return false; }
    },
    readShareParams(){
      const p = new URLSearchParams(window.location.search);
      const dir = p.get('d');
      const s = p.get('s');
      if (!dir && (!s || !/^[0-9a-f]{16}$/.test(s))) return null;
      const vp = {};
      const z = parseFloat(p.get('z')), x = parseFloat(p.get('x')), y = parseFloat(p.get('y'));
      if (Number.isFinite(z)) vp.zoom = z;
      if (Number.isFinite(x)) vp.x = x;
      if (Number.isFinite(y)) vp.y = y;
      return { dir, slideId: s || null, viewport: vp };
    },

    // ---------- localStorage preferences ----------
    _prefsKey:'wsi-prefs',
    loadPrefs(){
      try {
        const raw = localStorage.getItem(this._prefsKey);
        if (!raw) return {};
        return JSON.parse(raw);
      } catch(e) { return {}; }
    },
    savePref(key, val){
      try {
        const prefs = this.loadPrefs();
        prefs[key] = val;
        localStorage.setItem(this._prefsKey, JSON.stringify(prefs));
      } catch(e) { /* private mode / disabled storage — ignore */ }
    },

    // ---------- Keyboard help overlay ----------
    toggleHelp(){ this.showHelp = !this.showHelp; },
    closeHelp(){ this.showHelp = false; },

    // Composable method groups (request mgmt, thumbnails, viewport,
    // measurements, QPTIFF channels) — spread in below.
    ...requestMethods,
    ...thumbnailMethods,
    ...viewportMethods,
    ...measureMethods,
    ...qptiffMethods,
  },

  mounted(){
    window.vueApp = this;

    // Fetch the logged-in user for the header sign-out button.
    fetch('/api/me', {credentials: 'same-origin'})
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d && d.username) this.currentUser = d.username; })
      .catch(() => {});

    // Restore persisted UI preferences (view mode, sidebar, page size)
    const prefs = this.loadPrefs();
    if (prefs.viewMode === 'grid' || prefs.viewMode === 'list') this.viewMode = prefs.viewMode;
    if (typeof prefs.showSidebar === 'boolean') this.showSidebar = prefs.showSidebar;
    if (Number.isFinite(prefs.itemsPerPage) && prefs.itemsPerPage > 0) this.itemsPerPage = prefs.itemsPerPage;

    // Load the tree first, THEN restore from a share URL. We must await
    // loadTrees() because openDir() (called during restore) cancels all
    // in-flight requests — if /api/tree is still pending it gets aborted and
    // the tree never renders.
    (async () => {
      await this.loadTrees();

      // Restore directory + slide + viewport from a share URL once trees load
      const share = this.readShareParams();
      if (share) {
        this._restoringFromUrl = true;
        this._shareViewport = share.viewport;
        try {
          if (share.dir) {
            // Open the directory first (loads slide list). Set selectedPath so
            // the tree highlights it and the URL round-trips correctly.
            this.selectedPath = share.dir;
            await this.openDir(share.dir);
          }
          if (share.slideId) {
            await this.view(share.slideId);
          }
        } catch(e) { console.warn('Failed to restore shared location', e); }
        finally { this._restoringFromUrl = false; }
      }
    })();

    // Keyboard shortcuts (viewer only; ignore when typing in inputs)
    window.addEventListener('keydown', (e) => this.handleKey(e));

    // Browser back/forward: match directory + slide to the URL
    window.addEventListener('popstate', () => {
      const sp = this.readShareParams();
      if (sp) {
        // Re-select the directory if it changed
        if (sp.dir && sp.dir !== this.selectedPath) {
          this.selectDir(sp.dir);
        }
        if (sp.slideId && sp.slideId !== this.current) {
          this._shareViewport = sp.viewport;
          this.view(sp.slideId);
        } else if (!sp.slideId && this.current) {
          // URL has a directory but no slide — drop back to the grid
          this.closeViewer();
        }
      } else if (this.current || this.selectedPath) {
        // No params at all — return to the root tree view
        if (this.current) this.closeViewer();
        this.selectedPath = null;
      }
    });

    // Listen for fullscreen changes
    document.addEventListener('fullscreenchange', () => {
      this.isFullscreen = !!document.fullscreenElement;
    });

    // Cancel all requests on page unload
    window.addEventListener('beforeunload', () => {
      this.cancelAllRequests();
    });

    // Setup IntersectionObserver for grid container
    // this.ensureObserver();

    // initial viewport compute
    this.$nextTick(() => this.updateViewport());
  },

  beforeUnmount() {
    // ✅ P3: Set guard flag FIRST so in-flight fetches don't create new blob URLs
    this._unmounted = true;
    this.cancelAllRequests();
    if (this.viewportObserver) {
      this.viewportObserver.disconnect();
    }
    // Revoke any allocated blobs
    this.visibleSlides.forEach(s => this.revokeThumb(s));
  }
});

app.directive('observe-visible', observeVisibleDirective);
app.mount("#app");
