/* 轻量时序折线图：自绘 SVG，无第三方依赖、无 CDN。
 *
 * 为什么不引图表库：本项目要能离线内网运行，vendor 一个 300KB+ 的图表库
 * 只为了画折线并不划算，而折线图所需的计算（比例映射、路径拼接）本身
 * 不到百行。这里只实现趋势图真正需要的能力：多曲线、坐标轴、悬停提示。
 *
 * 用法：
 *   const chart = new TrendChart(document.querySelector('#chart'), {
 *     series: [{ name: 'mysql.qps', points: [{t, v}, ...] }],
 *     unit: '次/秒',
 *   });
 *   chart.render();
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var PADDING = { top: 16, right: 16, bottom: 28, left: 56 };
  // 曲线配色：避开纯红/纯绿，防止与"异常/正常"的语义色混淆
  var COLORS = ['#2563eb', '#0891b2', '#7c3aed', '#b45309', '#15803d', '#be123c'];

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    return node;
  }

  function fmtNumber(value) {
    if (!isFinite(value)) return '-';
    var abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(1) + 'G';
    if (abs >= 1e6) return (value / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + 'k';
    if (abs >= 1) return value.toFixed(1);
    if (abs === 0) return '0';
    return value.toFixed(3);
  }

  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function TrendChart(container, options) {
    this.container = container;
    this.options = options || {};
    this.series = this.options.series || [];
  }

  /* 计算 Y 轴范围。
   *
   * 关键决策：**从 0 开始**，不做"缩放放大局部波动"。
   * 自动缩放会让一条在 99%~100% 之间抖动的命中率曲线看起来像断崖，
   * 而运维看到图表的第一反应是"真的出事了"。宁可曲线平一些。
   * 例外：全部数据都为负时不强制含 0（这类指标本身以负数为主）。
   */
  TrendChart.prototype._range = function () {
    var min = 0;
    var max = 0;
    var any = false;
    this.series.forEach(function (s) {
      (s.points || []).forEach(function (p) {
        if (typeof p.v !== 'number' || !isFinite(p.v)) return;
        any = true;
        if (p.v < min) min = p.v;
        if (p.v > max) max = p.v;
      });
    });
    if (!any) return { min: 0, max: 1 };
    if (max === min) max = min + 1;
    // 上浮 5% 留白，避免曲线贴顶
    return { min: min, max: max + (max - min) * 0.05 };
  };

  TrendChart.prototype.render = function () {
    var box = this.container;
    if (!box) return;
    box.innerHTML = '';

    var width = box.clientWidth || 720;
    var height = this.options.height || 260;
    var svg = el('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      width: '100%',
      height: height,
      role: 'img',
    });

    var range = this._range();
    var plotW = Math.max(1, width - PADDING.left - PADDING.right);
    var plotH = Math.max(1, height - PADDING.top - PADDING.bottom);

    var xAt = function (index, total) {
      if (total <= 1) return PADDING.left;
      return PADDING.left + (plotW * index) / (total - 1);
    };
    var yAt = function (value) {
      var ratio = (value - range.min) / (range.max - range.min || 1);
      return PADDING.top + plotH - plotH * ratio;
    };

    // --- 网格与 Y 轴刻度（4 段）---
    for (var i = 0; i <= 4; i++) {
      var y = PADDING.top + (plotH * i) / 4;
      svg.appendChild(
        el('line', {
          x1: PADDING.left, y1: y, x2: width - PADDING.right, y2: y,
          stroke: '#eef1f5', 'stroke-width': 1,
        })
      );
      var label = range.max - ((range.max - range.min) * i) / 4;
      var text = el('text', {
        x: PADDING.left - 8, y: y + 4,
        'text-anchor': 'end', 'font-size': 11, fill: '#6b7280',
      });
      text.textContent = fmtNumber(label);
      svg.appendChild(text);
    }

    // --- 曲线 ---
    var allTimes = [];
    this.series.forEach(function (s) {
      (s.points || []).forEach(function (p) {
        if (allTimes.indexOf(p.t) === -1) allTimes.push(p.t);
      });
    });
    allTimes.sort();

    var self = this;
    this.series.forEach(function (s, index) {
      var points = (s.points || []).filter(function (p) {
        return typeof p.v === 'number' && isFinite(p.v);
      });
      if (!points.length) return;

      var d = '';
      points.forEach(function (p, k) {
        var x = allTimes.length > 1
          ? xAt(allTimes.indexOf(p.t), allTimes.length)
          : PADDING.left + plotW / 2;
        d += (k === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + yAt(p.v).toFixed(1) + ' ';
      });

      svg.appendChild(
        el('path', {
          d: d.trim(),
          fill: 'none',
          stroke: COLORS[index % COLORS.length],
          'stroke-width': 2,
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
        })
      );

      // 悬停圆点：每点一个透明大热区，鼠标进入时显示数值
      points.forEach(function (p) {
        var x = allTimes.length > 1
          ? xAt(allTimes.indexOf(p.t), allTimes.length)
          : PADDING.left + plotW / 2;
        var cy = yAt(p.v);
        var dot = el('circle', {
          cx: x, cy: cy, r: 2.5,
          fill: COLORS[index % COLORS.length],
        });
        dot.style.opacity = points.length > 60 ? '0' : '1';
        var tip = el('title', {});
        tip.textContent = fmtTime(p.t) + '  ' + p.v + (self.options.unit ? ' ' + self.options.unit : '');
        dot.appendChild(tip);
        svg.appendChild(dot);
      });
    });

    // --- 图例 ---
    if (this.series.length > 1) {
      var legend = document.createElement('div');
      legend.className = 'chart-legend';
      this.series.forEach(function (s, index) {
        var item = document.createElement('span');
        var swatch = document.createElement('i');
        swatch.style.background = COLORS[index % COLORS.length];
        item.appendChild(swatch);
        item.appendChild(document.createTextNode(s.name));
        legend.appendChild(item);
      });
      box.appendChild(legend);
    }

    box.appendChild(svg);
  };

  /* 拉数据并渲染。失败时在容器里显示原因，而不是留一片空白。 */
  TrendChart.prototype.load = function (url) {
    var self = this;
    var box = this.container;
    if (box) box.textContent = '加载中…';

    return fetch(url, { credentials: 'same-origin' })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var grouped = {};
        (data.series || []).forEach(function (row) {
          var key = row.metric;
          if (!grouped[key]) {
            grouped[key] = { name: key + ' / ' + row.instance_name, points: [] };
          }
          grouped[key].points = grouped[key].points.concat(row.points || []);
        });
        self.series = Object.keys(grouped).map(function (k) { return grouped[k]; });
        self.render();
        return data;
      })
      .catch(function (err) {
        if (box) {
          box.innerHTML = '';
          var note = document.createElement('div');
          note.className = 'chart-empty';
          note.textContent = '指标加载失败：' + err.message;
          box.appendChild(note);
        }
      });
  };

  global.TrendChart = TrendChart;
})(window);
