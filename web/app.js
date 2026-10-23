(function () {
  "use strict";

  var TOOL_LABELS = {
    get_state: "Состояние слайсера", get_settings: "Чтение настроек", search_settings: "Поиск настроек",
    set_settings: "Изменение настроек", reset_settings: "Сброс настроек",
    list_quality_profiles: "Профили качества", set_quality_profile: "Выбор профиля",
    list_materials: "Материалы", set_material: "Выбор материала", save_profile: "Сохранение профиля",
    get_print_mode: "Режим печати", set_print_mode: "Переключение 3D/5D",
    load_model: "Загрузка модели", list_objects: "Объекты сцены", get_object_dimensions: "Габариты",
    move_object: "Перемещение", center_object: "Центрирование", rotate_object: "Поворот",
    scale_object: "Масштаб", arrange_all: "Авторасстановка", delete_object: "Удаление",
    analyze_geometry: "Анализ геометрии", suggest_orientations: "Подбор ориентации",
    apply_orientation: "Применение ориентации", start_slicing: "Слайсинг",
    get_slicing_status: "Статус слайсинга", get_slicing_result: "Результат слайсинга",
    export_gcode: "Экспорт G-code", save_project_3mf: "Сохранение 3MF", export_results: "Сохранение результатов"
  };
  var STATE_REFRESH_TOOLS = /^(set_|load_|move_|center_|rotate_|scale_|arrange_|delete_|apply_|reset_|start_)/;

  var $ = function (id) { return document.getElementById(id); };
  var seq = 0;
  var toolCards = {};
  var attachments = [];
  var busy = false;
  var stateTimer = null;
  var WELCOME_HTML = $("messages").innerHTML;

  // ---------------------------------------------------------------- helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "да" : "нет";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
  function api(path, body) {
    var opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    };
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }
  // прокручиваем вниз, только если пользователь сам не ушёл читать историю
  var stickToBottom = true;
  $("scroll").addEventListener("scroll", function () {
    var s = $("scroll");
    stickToBottom = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
  });
  function append(el) {
    var w = $("welcome");
    if (w) w.remove();
    $("messages").appendChild(el);
    if (stickToBottom) $("scroll").scrollTop = $("scroll").scrollHeight;
    return el;
  }
  function div(cls, html) {
    var d = document.createElement("div");
    d.className = cls;
    if (html !== undefined) d.innerHTML = html;
    return d;
  }

  // ---------------------------------------------------------------- markdown
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  }
  function markdown(src) {
    var blocks = [];
    src = esc(src).replace(/```[a-z]*\n?([\s\S]*?)```/g, function (_, code) {
      blocks.push("<pre>" + code + "</pre>");
      return "\u0000" + (blocks.length - 1) + "\u0000";
    });
    var lines = src.split("\n"), out = [], i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (/^\s*\|/.test(line)) {
        var rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
        var cells = function (r) { return r.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return inline(c.trim()); }); };
        var html = "<table>";
        rows.forEach(function (r, idx) {
          if (/^\s*\|?[\s:\-|]+\|?\s*$/.test(r)) return;
          var tag = idx === 0 ? "th" : "td";
          html += "<tr>" + cells(r).map(function (c) { return "<" + tag + ">" + c + "</" + tag + ">"; }).join("") + "</tr>";
        });
        out.push(html + "</table>");
        continue;
      }
      var h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) { out.push("<h" + (h[1].length + 1) + ">" + inline(h[2]) + "</h" + (h[1].length + 1) + ">"); i++; continue; }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line), items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          items.push("<li>" + inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "")) + "</li>"); i++;
        }
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(\||#|[-*]\s|\d+\.\s)/.test(lines[i])) { para.push(lines[i]); i++; }
      if (!para.length) { para.push(line); i++; }
      out.push("<p>" + inline(para.join("<br>")) + "</p>");
    }
    return out.join("").replace(/\u0000(\d+)\u0000/g, function (_, n) { return blocks[+n]; });
  }

  // ---------------------------------------------------------------- rendering
  function shortArgs(args) {
    if (!args) return "";
    var parts = [];
    Object.keys(args).forEach(function (k) {
      var v = args[k];
      if (k === "reason") return;
      if (typeof v === "object") v = JSON.stringify(v);
      v = String(v);
      if (k === "path") v = v.split(/[\\/]/).pop();
      parts.push(k + "=" + (v.length > 60 ? v.slice(0, 57) + "…" : v));
    });
    return parts.join(", ");
  }

  function renderToolCall(ev) {
    var d = document.createElement("details");
    d.className = "tool run";
    d.innerHTML =
      '<summary><span class="t-icon"><span class="spin"></span></span>' +
      '<span class="t-name">' + esc(TOOL_LABELS[ev.name] || ev.name) + "</span>" +
      (ev.source === "mcp" ? ' <span class="badge">MCP</span>' : "") +
      '<span class="t-args">' + esc(shortArgs(ev.args)) + "</span>" +
      '<span class="t-meta"></span></summary>' +
      '<div class="t-body"><div class="t-label">' + esc(ev.name) + " · аргументы</div><pre>" +
      esc(JSON.stringify(ev.args, null, 2)) + '</pre><div class="t-result"></div></div>';
    toolCards[ev.call_id] = d;
    append(d);
  }

  function renderToolResult(ev) {
    var d = toolCards[ev.call_id];
    if (!d) { renderToolCall({ call_id: ev.call_id, name: ev.name, args: {}, source: ev.source }); d = toolCards[ev.call_id]; }
    d.className = "tool " + (ev.ok ? "ok" : "fail");
    d.querySelector(".t-icon").textContent = ev.ok ? "✓" : "✕";
    d.querySelector(".t-meta").textContent = ev.duration != null ? ev.duration + " с" : "";
    var body = ev.ok ? JSON.stringify(ev.result, null, 2) : ev.error;
    if (body && body.length > 20000) body = body.slice(0, 20000) + "\n…";
    d.querySelector(".t-result").innerHTML =
      '<div class="t-label">' + (ev.ok ? "результат" : "ошибка") + "</div><pre>" + esc(body) + "</pre>";
    if (!ev.ok) d.open = true;
    if (STATE_REFRESH_TOOLS.test(ev.name)) refreshStateSoon();
  }

  function renderChanges(ev) {
    var rows = (ev.changes || []).map(function (c) {
      return "<tr><td><b>" + esc(c.label || c.key) + '</b> <span class="muted small">' + esc(c.key) + "</span></td>" +
        '<td><span class="old">' + esc(fmt(c.old)) + "</span> → <b>" + esc(fmt(c.new)) + "</b></td></tr>";
    }).join("");
    append(div("card changes",
      '<div class="c-title">✎ Изменены настройки</div>' + (rows ? "<table>" + rows + "</table>" : "") +
      (ev.reason ? '<div class="reason">' + esc(ev.reason) + "</div>" : "")));
  }

  function renderSliceDone(result) {
    result = result || {};
    append(div("card result",
      '<div class="c-title">✓ Слайсинг завершён</div><div class="stats">' +
      "<span>⏱ " + esc(result.print_time || "—") + "</span>" +
      "<span>🧵 " + esc(result.material_g != null ? result.material_g + " г" : "—") + "</span>" +
      "<span>📏 " + esc(result.material_m != null ? result.material_m + " м" : "—") + "</span></div>"));
  }

  function setSliceBar(state, progress) {
    var bar = $("slice-bar");
    if (state === "processing") {
      bar.classList.remove("hidden");
      var pct = Math.round((progress || 0) * 100);
      $("slice-progress").style.width = pct + "%";
      $("slice-pct").textContent = pct + "%";
    } else {
      bar.classList.add("hidden");
    }
  }

  function setStatus(state, tool) {
    var pill = $("status");
    pill.className = "pill " + (state === "idle" ? "idle" : state === "error" ? "error" : "");
    pill.textContent = state === "thinking" ? "Думает…" :
      state === "tool" ? "Выполняет: " + (TOOL_LABELS[tool] || tool) :
      state === "error" ? "Ошибка" : "Готов";
    busy = state === "thinking" || state === "tool";
    $("btn-send").classList.toggle("hidden", busy);
    $("btn-stop").classList.toggle("hidden", !busy);
  }

  function handle(ev) {
    switch (ev.type) {
      case "reset":
        $("messages").innerHTML = WELCOME_HTML;
        bindExamples();
        toolCards = {};
        break;
      case "user":
        var u = append(div("msg user"));
        u.textContent = ev.text || "";
        if (ev.attachments && ev.attachments.length) {
          var a = div("att"); a.textContent = "📎 " + ev.attachments.join(", "); u.appendChild(a);
        }
        break;
      case "assistant": append(div("msg assistant", markdown(ev.text || ""))); break;
      case "tool_call": renderToolCall(ev); break;
      case "tool_result": renderToolResult(ev); break;
      case "settings_changed": renderChanges(ev); refreshStateSoon(); break;
      case "profile_changed": case "scene_changed": refreshStateSoon(); break;
      case "slice_state":
        setSliceBar(ev.state, 0);
        if (ev.state === "done") renderSliceDone(ev.result);
        if (ev.state === "error" || ev.state === "disabled") append(div("msg error", esc(ev.message || "Ошибка слайсинга")));
        refreshStateSoon();
        break;
      case "slice_progress": setSliceBar("processing", ev.progress); break;
      case "file_saved":
        var check = ev.check ? " · слоёв: " + ev.check.layers + (ev.check.valid ? " ✓" : " ⚠ " + ev.check.problems.join("; ")) : "";
        append(div("card", "💾 " + (ev.kind === "gcode" ? "G-code" : "Проект 3MF") + ": <code>" + esc(ev.path) + "</code>" + esc(check)));
        break;
      case "status": setStatus(ev.state, ev.tool); break;
      case "error": append(div("msg error", "⚠ " + esc(ev.message))); setStatus("error"); break;
    }
  }

  // ---------------------------------------------------------------- polling
  function poll() {
    fetch("/api/events?since=" + seq + "&wait=20")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.events || []).forEach(function (ev) { seq = Math.max(seq, ev.seq); handle(ev); });
        if (!data.busy && busy && !(data.events || []).length) setStatus("idle");
        poll();
      })
      .catch(function () {
        $("status").className = "pill error";
        $("status").textContent = "Нет связи со слайсером";
        setTimeout(poll, 2000);
      });
  }

  // ---------------------------------------------------------------- side panel
  function refreshStateSoon() {
    clearTimeout(stateTimer);
    stateTimer = setTimeout(refreshState, 400);
  }
  function refreshState() {
    api("/api/state").then(function (data) {
      var box = $("state");
      if (!data.ok) { box.innerHTML = '<span class="muted">' + esc(data.error) + "</span>"; return; }
      var s = data.state, mode = s.print_mode || {}, q = s.quality || {}, m = s.material || {};
      var html = "<dl>" +
        "<dt>Принтер</dt><dd>" + esc(s.printer) + "</dd>" +
        "<dt>Режим</dt><dd>" + esc(mode.label || mode.mode || mode.error || "—") + "</dd>" +
        "<dt>Профиль</dt><dd>" + esc(q.name || q.quality_type || "—") + "</dd>" +
        "<dt>Материал</dt><dd>" + esc(m.name || "—") + "</dd>" +
        "<dt>Слайсинг</dt><dd>" + esc((s.slicing || {}).state || "—") + "</dd></dl>";
      (s.objects || []).forEach(function (o) {
        html += '<div class="obj"><b>' + esc(o.object_id) + "</b> " + esc(o.name) + "<br>" +
          esc(o.size_mm.x + " × " + o.size_mm.y + " × " + o.size_mm.z + " мм") +
          (o.outside_build_area ? ' <span style="color:var(--err)">вне стола</span>' : "") + "</div>";
      });
      box.innerHTML = html;
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- attachments
  function addFiles(list) {
    Array.prototype.forEach.call(list, function (file) {
      var ext = file.name.split(".").pop().toLowerCase();
      if (["stl", "obj", "3mf", "jpg", "jpeg", "png"].indexOf(ext) < 0) {
        append(div("msg error", "Неподдерживаемый файл: " + esc(file.name) + " (нужны STL/OBJ/3MF/JPG/PNG)"));
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        var url = reader.result;
        attachments.push({ name: file.name, data: url.slice(url.indexOf(",") + 1), preview: /^(jpg|jpeg|png)$/.test(ext) ? url : null });
        renderAttachments();
      };
      reader.readAsDataURL(file);
    });
  }
  function renderAttachments() {
    var box = $("attachments");
    box.innerHTML = "";
    attachments.forEach(function (a, idx) {
      var chip = div("chip", (a.preview ? '<img src="' + a.preview + '" alt="">' : "🧊") + esc(a.name) + ' <button type="button" title="Убрать">✕</button>');
      chip.querySelector("button").onclick = function () { attachments.splice(idx, 1); renderAttachments(); };
      box.appendChild(chip);
    });
  }

  // ---------------------------------------------------------------- send
  function send() {
    var text = $("input").value.trim();
    if (!text && !attachments.length) return;
    var payload = { text: text, attachments: attachments.map(function (a) { return { name: a.name, data: a.data }; }) };
    $("btn-send").disabled = true;
    stickToBottom = true;
    api("/api/message", payload).then(function () {
      $("input").value = "";
      attachments = [];
      renderAttachments();
    }).catch(function (e) {
      append(div("msg error", "⚠ " + esc(e.message)));
      if (/настройках/.test(e.message)) openSettings();
    }).then(function () { $("btn-send").disabled = false; });
  }

  // ---------------------------------------------------------------- settings
  function openSettings() {
    api("/api/config").then(function (c) {
      var f = $("settings-form");
      f.base_url.value = c.base_url || "";
      f.model.value = c.model || "";
      f.api_key.value = "";
      f.verify_ssl.checked = !!c.verify_ssl;
      f.output_dir.value = c.output_dir || "";
      $("key-hint").textContent = c.api_key_set ? "Ключ сохранён (" + c.api_key_hint + "). Оставьте поле пустым, чтобы не менять." : "Ключ не задан.";
      $("settings-msg").textContent = "";
      $("settings").showModal();
    });
  }
  function saveSettings() {
    var f = $("settings-form");
    return api("/api/config", {
      base_url: f.base_url.value.trim(), model: f.model.value.trim(), api_key: f.api_key.value.trim(),
      verify_ssl: f.verify_ssl.checked, output_dir: f.output_dir.value.trim()
    });
  }
  function loadConfig() {
    api("/api/config").then(function (c) {
      $("mcp-url").textContent = c.mcp_url;
      $("tool-count").textContent = "Инструментов: " + (c.tools || []).length;
      if (!c.llm_ready) openSettings();
    });
  }

  // ---------------------------------------------------------------- screenshot
  function screenshot() {
    var run = function () {
      window.html2canvas($("messages"), { backgroundColor: getComputedStyle(document.body).backgroundColor, scale: 1.5 })
        .then(function (canvas) {
          var a = document.createElement("a");
          a.download = "chat_" + new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) + ".png";
          a.href = canvas.toDataURL("image/png");
          a.click();
        });
    };
    if (window.html2canvas) return run();
    var s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
    s.onload = run;
    s.onerror = function () { alert("Нет доступа к интернету для html2canvas — сделайте скриншот средствами ОС."); };
    document.head.appendChild(s);
  }

  // ---------------------------------------------------------------- wiring
  $("composer").addEventListener("submit", function (e) { e.preventDefault(); send(); });
  $("input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("file-input").addEventListener("change", function (e) { addFiles(e.target.files); e.target.value = ""; });
  $("btn-stop").onclick = function () { api("/api/stop", {}); };
  $("btn-reset").onclick = function () { if (confirm("Начать новый диалог? История будет очищена.")) api("/api/reset", {}); };
  $("btn-settings").onclick = openSettings;
  $("btn-shot").onclick = screenshot;
  $("btn-cancel").onclick = function () { $("settings").close(); };
  $("settings-form").addEventListener("submit", function (e) {
    e.preventDefault();
    saveSettings().then(function () { $("settings").close(); loadConfig(); })
      .catch(function (err) { $("settings-msg").innerHTML = '<span class="msg-err">' + esc(err.message) + "</span>"; });
  });
  $("btn-test").onclick = function () {
    $("settings-msg").textContent = "Проверяю…";
    saveSettings().then(function () { return api("/api/test_llm", {}); }).then(function (r) {
      $("settings-msg").innerHTML = r.ok
        ? '<span class="msg-ok">✓ Подключение работает (' + esc(r.model) + ", " + r.latency_s + " с)</span>"
        : '<span class="msg-err">✕ ' + esc(r.error) + "</span>";
    }).catch(function (err) { $("settings-msg").innerHTML = '<span class="msg-err">✕ ' + esc(err.message) + "</span>"; });
  };
  function bindExamples() {
    document.querySelectorAll(".example").forEach(function (b) {
      b.onclick = function () { $("input").value = b.textContent; $("input").focus(); };
    });
  }
  bindExamples();

  var dragDepth = 0;
  window.addEventListener("dragenter", function (e) { e.preventDefault(); dragDepth++; $("drop").classList.remove("hidden"); });
  window.addEventListener("dragleave", function () { if (--dragDepth <= 0) { dragDepth = 0; $("drop").classList.add("hidden"); } });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) {
    e.preventDefault(); dragDepth = 0; $("drop").classList.add("hidden");
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  loadConfig();
  refreshState();
  poll();
})();
