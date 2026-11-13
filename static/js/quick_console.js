/* 快速连接终端抽屉。
 *
 * 目标：从任意页面的「快速连接」按钮一步进入可执行 SQL 的终端，
 * 不需要先记住实例 id、也不用手敲 sw 切过去。
 *
 * 设计约束：
 * 1. 实例解析走服务端 /console/switch.json（与 CLI 的 sw 完全同一套规则），
 *    前端不自己拼 id 语义，避免两处行为不一致。
 * 2. 执行语句复用 /console/<id>/exec/，因此登录与 CSRF 要求、危险语句
 *    二次确认、结果截断等口径与全屏控制台一模一样。
 * 3. 面板折叠只改变可见性，不销毁状态：收起后再展开，历史与当前实例都还在。
 */
(function () {
  "use strict";

  var drawer, handle, body, input, promptEl, titleEl, collapseBtn;

  var state = {
    instance: null,
    open: false,
    busy: false,
    history: [],
    histIdx: -1,
    pendingConfirm: null,
    ready: false
  };

  function csrfToken() {
    var m = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[2]) : "";
  }

  function esc(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function write(html, cls) {
    if (!body) return;
    var div = document.createElement("div");
    div.className = "qc-line " + (cls || "");
    div.innerHTML = html;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  function writeText(text, cls) {
    write(esc(text), cls);
  }

  function echo(cmd) {
    var p = state.instance ? "[" + state.instance.id + "] db&gt;" : "db&gt;";
    write(
      '<span class="qc-prompt">' + p + "</span> " +
      '<span class="qc-cmd">' + esc(cmd) + "</span>"
    );
  }

  function renderTable(columns, rows) {
    if (!columns || !columns.length) return;
    var html = '<table class="term-table"><thead><tr>';
    columns.forEach(function (c) { html += "<th>" + esc(c) + "</th>"; });
    html += "</tr></thead><tbody>";
    rows.forEach(function (r) {
      html += "<tr>";
      r.forEach(function (v) { html += "<td>" + esc(v) + "</td>"; });
      html += "</tr>";
    });
    html += "</tbody></table>";
    write(html);
  }

  function updateChrome() {
    if (!state.instance) {
      promptEl.textContent = "db>";
      titleEl.textContent = "未连接";
      return;
    }
    promptEl.textContent = "[" + state.instance.id + "] db>";
    titleEl.textContent =
      state.instance.label + "  " + state.instance.endpoint;
  }

  // ---- 展开 / 折叠 -------------------------------------------------------

  function setOpen(open) {
    if (!drawer) return;
    state.open = open;
    drawer.classList.toggle("open", open);
    drawer.setAttribute("aria-hidden", open ? "false" : "true");
    if (handle) handle.classList.toggle("show", !open);
    document.body.classList.toggle("qc-open", open);
    if (open) {
      // 展开后把焦点放到输入框，符合"点了就能打字"的预期
      window.setTimeout(function () { if (input) input.focus(); }, 220);
    }
  }

  function collapse() {
    setOpen(false);
  }

  function toggle() {
    setOpen(!state.open);
  }

  // ---- 打开并切换实例 ----------------------------------------------------

  var HELP = [
    "可用命令：",
    "  help            显示本帮助",
    "  status          查看当前实例",
    "  verify          连一次目标库，确认账号密码可用",
    "  clear           清屏",
    "  exit            收起面板",
    "  其它输入        按 SQL 发送到当前实例执行",
    "",
    "提示：面板右上角「收起」可折叠到屏幕右侧，点边缘把手重新展开；",
    "按 Esc 也能收起。当前实例始终是按钮对应的那一个。"
  ].join("\n");

  function openInstance(target, options) {
    options = options || {};
    if (!drawer) return;

    setOpen(true);

    // 已经在这个实例上：只展开，不重复打印欢迎语
    if (state.instance && String(state.instance.id) === String(target) && !options.force) {
      if (input) input.focus();
      return;
    }

    writeText("正在切换实例 …", "qc-dim");

    fetch("/console/switch.json?target=" + encodeURIComponent(target), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) {
          writeText(d.message || "切换失败", "qc-err");
          return;
        }
        state.instance = d.instance;
        state.pendingConfirm = null;
        updateChrome();

        writeText("已连接：" + d.instance.label + "（" + d.instance.endpoint + "）", "qc-ok");
        if (!d.instance.password_set) {
          writeText("注意：该实例尚未登记密码，执行语句会失败。", "qc-warn");
        } else {
          writeText("输入 help 查看命令，其它内容按 SQL 执行。", "qc-dim");
        }
        if (input) input.focus();
      })
      .catch(function () {
        writeText("无法连接到服务，请确认程序仍在运行。", "qc-err");
      });
  }

  // ---- 命令 --------------------------------------------------------------

  function cmdStatus() {
    if (!state.instance) {
      writeText("尚未选择实例。请从实例详情页点「快速连接」。", "qc-warn");
      return;
    }
    var i = state.instance;
    renderTable(
      ["字段", "值"],
      [
        ["实例", i.label],
        ["引擎", i.engine],
        ["环境", i.env],
        ["地址", i.endpoint],
        ["账号", i.username],
        ["密码", i.password_set ? "已设置" : "未设置"],
        ["状态", (i.is_active ? "" : "已停用 ") + i.status]
      ]
    );
  }

  function cmdVerify() {
    if (!state.instance) {
      writeText("尚未选择实例。", "qc-warn");
      return Promise.resolve();
    }
    writeText("正在连接 " + state.instance.endpoint + " …", "qc-dim");
    return fetch("/console/" + state.instance.id + "/verify.json", {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) {
          writeText(
            "凭据可用（" + (d.latency_ms == null ? "-" : d.latency_ms + "ms") +
            "，版本 " + (d.server_version || "未知") + "）",
            "qc-ok"
          );
        } else {
          writeText("连接失败：" + (d.hint || d.error || "未知原因"), "qc-err");
        }
      })
      .catch(function () { writeText("验证请求失败", "qc-err"); });
  }

  function cmdClear() {
    if (body) body.innerHTML = "";
  }

  function runSql(sql, confirmed) {
    if (!state.instance) {
      writeText("尚未选择实例。请从实例详情页点「快速连接」。", "qc-warn");
      return Promise.resolve();
    }
    state.busy = true;

    var form = new URLSearchParams();
    form.set("sql", sql);
    if (confirmed) form.set("confirm", "1");

    return fetch("/console/" + state.instance.id + "/exec/", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": csrfToken(),
        "X-Requested-With": "XMLHttpRequest"
      },
      body: form.toString(),
      credentials: "same-origin"
    })
      .then(function (resp) {
        var ct = resp.headers.get("Content-Type") || "";
        if (ct.indexOf("application/json") === -1) throw new Error("NOT_JSON");
        return resp.json();
      })
      .then(function (d) {
        if (d.error === "confirm_required") {
          state.pendingConfirm = sql;
          writeText("该语句不可逆（DROP / TRUNCATE）。确认请再次输入同一条语句。", "qc-warn");
          return;
        }
        if (!d.ok) {
          writeText("失败：" + (d.hint || d.error || "未知错误"), "qc-err");
          return;
        }
        if (d.columns && d.columns.length) {
          renderTable(d.columns, d.rows);
          var summary = "返回 " + d.row_count + " 行";
          if (d.truncated) summary += "（已截断）";
          summary += "，耗时 " + d.duration_ms + "ms";
          writeText(summary, "qc-dim");
        } else {
          writeText(
            "执行成功，影响 " + (d.affected == null ? "-" : d.affected) +
            " 行，耗时 " + d.duration_ms + "ms",
            "qc-ok"
          );
        }
      })
      .catch(function (err) {
        if (err && err.message === "NOT_JSON") {
          writeText("需要登录才能执行语句。请先登录，然后重新打开本面板。", "qc-err");
        } else {
          writeText("请求失败：无法连接到服务", "qc-err");
        }
      })
      .finally(function () { state.busy = false; });
  }

  function dispatch(line) {
    var text = line.trim();
    if (!text) return Promise.resolve();

    echo(text);
    var cmd = text.split(/\s+/)[0].toLowerCase();

    if (cmd === "help" || cmd === "?") { writeText(HELP); return Promise.resolve(); }
    if (cmd === "clear" || cmd === "cls") { cmdClear(); return Promise.resolve(); }
    if (cmd === "status") { cmdStatus(); return Promise.resolve(); }
    if (cmd === "verify") { return cmdVerify(); }
    if (cmd === "exit" || cmd === "quit") { collapse(); return Promise.resolve(); }

    if (state.pendingConfirm !== null && text === state.pendingConfirm) {
      var sqlToRun = state.pendingConfirm;
      state.pendingConfirm = null;
      return runSql(sqlToRun, true);
    }
    state.pendingConfirm = null;
    return runSql(text, false);
  }

  // ---- 初始化 ------------------------------------------------------------

  function init() {
    drawer = document.getElementById("qc-drawer");
    handle = document.getElementById("qc-handle");
    body = document.getElementById("qc-body");
    input = document.getElementById("qc-input");
    promptEl = document.getElementById("qc-prompt");
    titleEl = document.getElementById("qc-title");
    collapseBtn = document.getElementById("qc-collapse");

    if (!drawer || !body || !input) return;

    drawer.setAttribute("aria-hidden", "true");

    if (collapseBtn) collapseBtn.addEventListener("click", collapse);
    if (handle) handle.addEventListener("click", function () { setOpen(true); });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        if (state.busy) return;
        var line = input.value;
        input.value = "";
        if (line.trim()) {
          state.history.push(line);
          state.histIdx = state.history.length;
        }
        dispatch(line);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!state.history.length) return;
        state.histIdx = Math.max(0, state.histIdx - 1);
        input.value = state.history[state.histIdx] || "";
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!state.history.length) return;
        state.histIdx = Math.min(state.history.length, state.histIdx + 1);
        input.value = state.history[state.histIdx] || "";
      } else if (e.key === "l" && e.ctrlKey) {
        e.preventDefault();
        cmdClear();
      }
    });

    // Esc 收起：面板盖住内容时，用户需要一个不用找按钮的退路
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && state.open) collapse();
    });

    // 点面板内空白处也能聚焦输入框
    drawer.addEventListener("click", function (e) {
      if (e.target.tagName !== "INPUT" && e.target.tagName !== "BUTTON") {
        input.focus();
      }
    });

    state.ready = true;
    updateChrome();
  }

  window.QuickConsole = {
    open: openInstance,
    collapse: collapse,
    toggle: toggle,
    isOpen: function () { return state.open; },
    current: function () { return state.instance; }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
