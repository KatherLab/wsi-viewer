// Recursive folder tree component. Extracted verbatim from the original
// inline template; behavior unchanged.
export const Folder = {
  name: 'Folder',
  props: ["node", "selectedPath"],
  emits: ["select-dir"],
  data(){
    return {
      open: false,
      loading: false,
      childrenLoaded: false,
      localChildren: null
    }
  },
  methods:{
    async toggle(){
      if (!this.open && !this.childrenLoaded && this.node.has_children) {
        this.loading = true;
        try {
          const response = await fetch('/api/expand?' + new URLSearchParams({path: this.node.path}));
          if (response.ok) {
            this.localChildren = await response.json();
            this.childrenLoaded = true;
          }
        } catch (e) {
          console.error('Failed to load children:', e);
        } finally {
          this.loading = false;
        }
      }

      this.open = !this.open;
      this.$emit("select-dir", this.node.path);
    }
  },
  computed:{
    hasChildren(){
      return this.node.has_children || (this.node.children && this.node.children.length > 0);
    },
    children() {
      return this.localChildren || this.node.children || [];
    },
    isSelected(){
      return this.selectedPath === this.node.path;
    },
    slideCountText() {
      if (!this.node.slide_count || this.node.slide_count === 0) return '';
      return this.node.slide_count === 1 ? '1 slide' : `${this.node.slide_count} slides`;
    }
  },
  template:`
    <div>
      <div class="dir" @click="toggle" :aria-expanded="open" :class="{selected: isSelected}">
        <div>
          <strong>{{ node.name }}</strong>
          <small v-if="slideCountText" style="color:var(--muted)"> · {{ slideCountText }}</small>
          <span v-if="loading" style="margin-left:8px;color:var(--muted)">(loading...)</span>
        </div>
        <div class="right">
          <span v-if="hasChildren" style="color:var(--muted)">{{ open ? "▾" : "▸" }}</span>
        </div>
      </div>
      <div class="children" v-if="open && children.length > 0">
        <folder v-for="c in children" :key="c.id" :node="c" :selected-path="selectedPath" @select-dir="$emit('select-dir',$event)"></folder>
      </div>
    </div>`
};
