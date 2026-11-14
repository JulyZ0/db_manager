/* 字节数友好显示：把 12345678901 变成 11.5 GB。
 *
 * 为什么放在前端而不是后端格式化成字符串：后端返回的是原始数值，
 * 排序、作图、导出都要用；只有最终呈现给人才需要单位换算。
 * 带 data-bytes 属性的元素在加载后自动替换文本。
 */
(function () {
  'use strict';

  var UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

  function formatBytes(value) {
    var num = Number(value);
    if (!isFinite(num) || num < 0) return '-';
    var index = 0;
    while (num >= 1024 && index < UNITS.length - 1) {
      num /= 1024;
      index += 1;
    }
    var digits = index === 0 ? 0 : (num >= 100 ? 1 : 2);
    return num.toFixed(digits) + ' ' + UNITS[index];
  }

  function apply() {
    var nodes = document.querySelectorAll('[data-bytes]');
    for (var i = 0; i < nodes.length; i++) {
      var raw = nodes[i].getAttribute('data-bytes');
      if (raw === null || raw === '') continue;
      nodes[i].textContent = formatBytes(raw);
      nodes[i].setAttribute('title', raw + ' 字节');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }

  window.formatBytes = formatBytes;
})();
