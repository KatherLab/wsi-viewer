// Recursive folder tree component. Supports auto-expanding toward a target
// path (passed as `expandPath`) so that a shared directory URL reveals its
// location in the tree on load.
export const Folder = {
  name: 'Folder',
  props: ["node", "selectedPath", "expandPath"],
  emits: ["select-dir"],
  data(){
    return {
      open: false,
      loading: false,
      childrenLoaded: false,
      localChildren: null,
      denied: false   // set when /api/expand returned 403 (no list permission)
    }
  },
  methods:{
    async loadChildren(){
      if (this.childrenLoaded || !this.node.has_children) return;
      this.loading = true;
      try {
        const response = await fetch('/api/expand?' + new URLSearchParams({path: this.node.path}),
                                     {credentials: 'same-origin'});
        if (response.status === 403) {
          // Per-user ACL denial: this directory is not listable for this user.
          this.denied = true;
          this.$root.showToast(`No access to "${this.node.name}"`);
          return;
        }
        if (response.ok) {
          this.localChildren = await response.json();
          this.childrenLoaded = true;
        }
      } catch (e) {
        console.error('Failed to load children:', e);
      } finally {
        this.loading = false;
      }
    },
    async toggle(){
      if (this.node.locked) return;  // access-denied root: not expandable
      if (!this.open && !this.childrenLoaded && this.node.has_children) {
        await this.loadChildren();
      }
      this.open = !this.open;
      this.$emit("select-dir", this.node.path);
    },
    // If expandPath is set and this node is an ancestor of it, open + load
    // children so the target reveals itself down the chain.
    async maybeAutoExpand(){
      const target = this.expandPath;
      if (!target || this.node.path === target) return;
      if (target.startsWith(this.node.path + '/')) {
        if (!this.childrenLoaded && this.node.has_children) await this.loadChildren();
        if (!this.open) this.open = true;
      }
    }
  },
  watch:{
    expandPath:{ immediate:true, handler(){ this.maybeAutoExpand(); } }
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
      <div class="dir" @click="toggle" :aria-expanded="open" :class="{selected: isSelected, locked: node.locked || denied}">
        <div>
          <strong>{{ node.name }}</strong>
          <small v-if="slideCountText" style="color:var(--muted)"> · {{ slideCountText }}</small>
          <span v-if="loading" style="margin-left:8px;color:var(--muted)">(loading...)</span>
          <span v-if="node.locked || denied" style="margin-left:8px;color:#dc2626" title="You don't have access to this directory">🔒 no access</span>
        </div>
        <div class="right">
          <span v-if="node.locked || denied" style="color:#dc2626"></span>
          <span v-else-if="hasChildren" style="color:var(--muted)">{{ open ? "▾" : "▸" }}</span>
        </div>
      </div>
      <div class="children" v-if="open && children.length > 0">
        <folder v-for="c in children" :key="c.id" :node="c" :selected-path="selectedPath" :expand-path="expandPath" @select-dir="$emit('select-dir',$event)"></folder>
      </div>
    </div>`
};
