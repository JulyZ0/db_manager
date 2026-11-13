/* 集群拓扑画布。
 *
 * 数据来源：页面里 <script id="topology-data" type="application/json"> 内联的
 * build_topology() 结果（与 /api/v1/clusters/{id}/topology 同源）。
 * 内联而不是再发一次请求：首屏就能画出来，少一次 RTT，也不会出现"先空一下"。
 *
 * 画布用本地打包的 cytoscape（static/js/vendor/），不依赖任何 CDN ——
 * 目标机器可能完全无外网。
 */
(function () {
  "use strict";

  var ROLE_LABEL = { master: "主库", slave: "从库", proxy: "代理" };

  function readData() {
    var el = document.getElementById("topology-data");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  function showFallback(message) {
    var canvas = document.getElementById("topology-canvas");
    var fallback = document.getElementById("topology-fallback");
    if (canvas) canvas.hidden = true;
    if (fallback) {
      fallback.hidden = false;
      if (message) {
        var p = document.createElement("p");
        p.className = "muted";
        p.textContent = message;
        fallback.appendChild(p);
      }
    }
  }

  /* 节点配色：状态优先于角色 —— 一台离线的从库，运维最先要知道的是"它挂了" */
  function nodeClass(node) {
    if (!node.is_active) return "offline";
    if (node.status === "offline") return "offline";
    if (node.role === "master") return "master";
    if (node.role === "slave") return "slave";
    return "proxy";
  }

  function buildElements(data) {
    var nodes = data.nodes.map(function (n) {
      return {
        data: {
          id: String(n.id),
          label: n.name,
          sub: n.host + ":" + n.port,
          role: n.role,
          roleLabel: ROLE_LABEL[n.role] || n.role,
          status: n.status,
          isActive: n.is_active,
          passwordSet: n.password_set,
          hasUpstream: n.has_upstream
        },
        classes: nodeClass(n)
      };
    });

    var edges = data.edges.map(function (e, index) {
      return {
        data: {
          id: "e" + index,
          source: String(e.source),
          target: String(e.target)
        }
      };
    });

    return nodes.concat(edges);
  }

  /* 布局根节点 = 没有上游的实例。主从/级联都从它往下铺开；
     双主互指时没有根，退化成环形布局。 */
  function layoutOptions(data) {
    var roots = data.nodes
      .filter(function (n) { return !n.has_upstream; })
      .map(function (n) { return String(n.id); });

    if (!roots.length) {
      return {
        name: "circle",
        padding: 40,
        avoidOverlap: true,
        animate: false
      };
    }

    return {
      name: "breadthfirst",
      directed: true,
      roots: roots,
      padding: 40,
      spacingFactor: 1.5,
      animate: false
    };
  }

  function init() {
    var data = readData();
    if (!data || !data.nodes || !data.nodes.length) return;

    if (typeof window.cytoscape !== "function") {
      showFallback("cytoscape 未能加载（静态文件缺失或被浏览器拦截）。");
      return;
    }

    var canvas = document.getElementById("topology-canvas");
    if (!canvas) return;

    var cy;
    try {
      cy = window.cytoscape({
        container: canvas,
        elements: buildElements(data),
        layout: layoutOptions(data),
        minZoom: 0.4,
        maxZoom: 2.5,
        wheelSensitivity: 0.2,
        style: [
          {
            selector: "node",
            style: {
              "background-color": "#eef1f5",
              "border-width": 2,
              "border-color": "#9aa4b2",
              label: "data(label)",
              "text-valign": "center",
              "text-halign": "center",
              "font-size": 12,
              "font-family": "Microsoft YaHei, PingFang SC, sans-serif",
              color: "#1f2329",
              width: 96,
              height: 46,
              shape: "round-rectangle",
              "text-wrap": "ellipsis",
              "text-max-width": 84
            }
          },
          {
            selector: "node.master",
            style: {
              "background-color": "#dbeafe",
              "border-color": "#2563eb",
              color: "#1d4ed8"
            }
          },
          {
            selector: "node.slave",
            style: {
              "background-color": "#dcfce7",
              "border-color": "#15803d",
              color: "#15803d"
            }
          },
          {
            selector: "node.proxy",
            style: {
              "background-color": "#eef1f5",
              "border-color": "#6b7280",
              color: "#374151"
            }
          },
          {
            selector: "node.offline",
            style: {
              "background-color": "#fee2e2",
              "border-color": "#b91c1c",
              color: "#b91c1c",
              "border-style": "dashed"
            }
          },
          {
            selector: "node:selected",
            style: {
              "border-width": 3,
              "border-color": "#b45309"
            }
          },
          {
            selector: "edge",
            style: {
              width: 2,
              "line-color": "#9aa4b2",
              "target-arrow-color": "#9aa4b2",
              "target-arrow-shape": "triangle",
              "curve-style": "bezier",
              "arrow-scale": 1.1
            }
          },
          {
            // 单击节点时把无关元素淡出，密集图里更容易看清一条链路
            selector: ".faded",
            style: {
              opacity: 0.25
            }
          }
        ]
      });
    } catch (err) {
      showFallback("画布初始化失败：" + (err && err.message ? err.message : "未知错误"));
      return;
    }

    // 双击节点 → 打开终端面板（复用实例详情页那套快速连接）
    cy.on("dbltap", "node", function (evt) {
      var node = evt.target;
      var id = node.data("id");
      if (!node.data("isActive")) {
        return;
      }
      if (window.QuickConsole && typeof window.QuickConsole.open === "function") {
        window.QuickConsole.open(id);
      }
    });

    // 单击高亮它和它的直接上下游，方便在密集图里看清一条链路
    cy.on("tap", "node", function (evt) {
      var node = evt.target;
      cy.elements().removeClass("faded");
      var related = node.connectedEdges().connectedNodes().union(node.connectedEdges());
      cy.elements().difference(related).addClass("faded");
      node.select();
    });

    cy.on("tap", function (evt) {
      if (evt.target === cy) {
        cy.elements().removeClass("faded");
        cy.$(":selected").unselect();
      }
    });

    // 节点清单里的"终端"按钮走同一条路径
    var buttons = document.querySelectorAll("[data-terminal-instance]");
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener("click", function () {
        if (!window.QuickConsole) return;
        window.QuickConsole.open(btn.getAttribute("data-terminal-instance"));
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
