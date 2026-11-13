/* 角色下拉按集群架构实时过滤。
 *
 * 架构是集群级决策，实例只能在架构允许的范围内选角色。
 * 集群下拉的每个 option 带 data-topology，角色下拉的每个 option 带
 * data-allow（逗号分隔的架构列表），两边都由 forms.py 的 widget 渲染出来，
 * 前端只做"隐藏不合法项"，不改写任何数据。
 *
 * 这层只是让界面不给出错误选项；真正的把关在 InstanceForm.clean()
 * 与 Instance.clean()，绕过前端提交同样会被拒绝。
 */
(function () {
  "use strict";

  function init() {
    var clusterSelect = document.querySelector('select[name="cluster"]');
    var roleSelect = document.querySelector('select[name="role"]');
    if (!clusterSelect || !roleSelect) return;

    // 记下每个 option 的原始可见性，避免反复切换后状态丢失
    var options = Array.prototype.slice.call(roleSelect.options);

    function topologyOf(select) {
      var opt = select.options[select.selectedIndex];
      return opt ? opt.getAttribute("data-topology") : null;
    }

    function allowOf(option) {
      var raw = option.getAttribute("data-allow");
      return raw ? raw.split(",") : null;
    }

    function refresh() {
      var topology = topologyOf(clusterSelect);

      // 没选集群（或该 option 没有标注）时不过滤，让用户先选集群
      if (!topology) {
        options.forEach(function (opt) {
          opt.hidden = false;
          opt.disabled = false;
        });
        return;
      }

      var firstAllowed = null;
      options.forEach(function (opt) {
        var allow = allowOf(opt);
        // 没有 data-allow 的是空选项占位，保持可见
        var ok = !allow || allow.indexOf(topology) !== -1;
        opt.hidden = !ok;
        opt.disabled = !ok;
        if (ok && firstAllowed === null) firstAllowed = opt;
      });

      // 当前选中项被隐藏时，自动落到第一个合法项，避免提交非法值
      var current = roleSelect.options[roleSelect.selectedIndex];
      if (current && current.disabled && firstAllowed) {
        roleSelect.value = firstAllowed.value;
      }
    }

    clusterSelect.addEventListener("change", refresh);
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
