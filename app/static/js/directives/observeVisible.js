// Custom directive to hook IntersectionObserver to grid cards.
// Operates against the root Vue instance (binding.instance), which owns the
// shared `viewportObserver` and the `_toObserve` pending-queue.
export const observeVisibleDirective = {
  mounted(el, binding) {
    const vm = binding.instance;
    if (!vm.viewportObserver) {
      vm._toObserve.push(el);
      return;
    }
    vm.viewportObserver.observe(el);
  },
  updated(el, binding) {
    const vm = binding.instance;
    if (vm && vm.viewportObserver) {
      vm.viewportObserver.observe(el);
    }
  },
  unmounted(el, binding) {
    const vm = binding.instance;
    if (vm && vm.viewportObserver) {
      vm.viewportObserver.unobserve(el);
    }
  }
};
