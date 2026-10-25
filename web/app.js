(function () {
  "use strict";

  const TOOL_LABELS = {
    get_state: "Состояние слайсера",
    get_settings: "Чтение настроек",
    search_settings: "Поиск настроек",
    set_settings: "Изменение настроек",
    reset_settings: "Сброс настроек",
    list_quality_profiles: "Профили качества",
    set_quality_profile: "Выбор профиля",
    list_materials: "Материалы",
    set_material: "Выбор материала",
    save_profile: "Сохранение профиля",
    get_print_mode: "Режим печати",
    set_print_mode: "Переключение 3D/5D",
    load_model: "Загрузка модели",
    list_objects: "Объекты сцены",
    get_object_dimensions: "Габариты",
    move_object: "Перемещение",
    center_object: "Центрирование",
    rotate_object: "Поворот",
    scale_object: "Масштаб",
    arrange_all: "Авторасстановка",
    delete_object: "Удаление",
    analyze_geometry: "Анализ геометрии",
    suggest_orientations: "Подбор ориентации",
    apply_orientation: "Применение ориентации",
    get_scene_snapshot: "Снимок сцены",
    start_slicing: "Слайсинг",
    get_slicing_status: "Статус слайсинга",
    get_slicing_result: "Результат слайсинга",
    export_gcode: "Экспорт G-code",
    save_project_3mf: "Сохранение 3MF",
    export_results: "Сохранение результатов",
    request_user_review: "Проверка пользователем",
  };

  const REVIEW_STATUS_TEXT = {
    approved: "Подтверждено",
    cancelled: "Проверка остановлена",
    timeout: "Время ожидания истекло",
  };

  const STATE_CHANGING_TOOLS = /^(set_|load_|move_|center_|rotate_|scale_|arrange_|delete_|apply_|reset_|start_)/;
  const SHARED_STATE_EVENTS = ["tool_result", "settings_changed", "profile_changed", "scene_changed", "review_closed"];
  const UNREAD_EVENTS = ["assistant", "error", "review_requested"];
  const ALLOWED_EXTENSIONS = ["stl", "obj", "3mf", "jpg", "jpeg", "png", "docx", "txt", "md"];
  const DOCUMENT_EXTENSIONS = ["docx", "txt", "md"];
  const IMAGE_EXTENSION_PATTERN = /^(jpg|jpeg|png)$/;
  const HTML2CANVAS_URL = "/vendor/html2canvas.min.js";

  const element = (id) => document.getElementById(id);

  let lastEventSeq = 0;
  let serverSession = null;
  let sliceOpen = false;
  let toolCards = {};
  let reviewCards = {};
  let attachments = [];
  let stateRefreshTimer = null;
  let stickToBottom = true;
  let dragDepth = 0;
  const welcomeHtml = element("messages").innerHTML;

  // Чаты идут параллельно. У каждого свой флаг запуска.
  let chats = [];
  let runningIds = new Set();
  let agentState = { state: "idle", tool: null, chatId: null };
  let sending = false;
  // Открытый чат: история приходит одним запросом, живые события — через общий long-polling.
  // seq — номер последнего события, уже вошедшего в историю; пока история грузится, события копятся в queue.
  let view = { chatId: null, seq: 0, loading: false, queue: [] };
  const unreadChats = new Set();
  let chatsRefreshTimer = null;
  let renamingChatId = null;

  function escapeHtml(value) {
    const replacements = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    const text = value === null || value === undefined ? "" : String(value);
    return text.replace(/[&<>"']/g, (character) => replacements[character]);
  }

  function formatValue(value) {
    if (value === null || value === undefined) {
      return "—";
    }
    if (typeof value === "boolean") {
      return value ? "да" : "нет";
    }
    if (typeof value === "object") {
      return JSON.stringify(value);
    }
    return String(value);
  }

  async function api(path, body) {
    const options = {};
    if (body !== undefined) {
      options.method = "POST";
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }

    const response = await fetch(path, options);
    const text = await response.text();

    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (error) {
      throw new Error(
        `Сервер плагина вернул не JSON (HTTP ${response.status} на ${path}). Подробности — в логе слайсера.`
      );
    }

    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  function scrollToBottom() {
    const scroll = element("scroll");
    scroll.scrollTop = scroll.scrollHeight;
  }

  function appendToChat(node) {
    const welcome = element("welcome");
    if (welcome) {
      welcome.remove();
    }
    element("messages").appendChild(node);
    if (stickToBottom) {
      scrollToBottom();
    }
    return node;
  }

  function createDiv(className, html) {
    const div = document.createElement("div");
    div.className = className;
    if (html !== undefined) {
      div.innerHTML = html;
    }
    return div;
  }

  function formatInline(text) {
    return text
      .replace(/⏱️?/g, '<img class="inline-icon" src="icons/print_time.svg" alt="">')
      .replace(/🧵/g, '<img class="inline-icon" src="icons/dense.svg" alt="">')
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  }

  function tableCells(row) {
    return row
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((cell) => formatInline(cell.trim()));
  }

  function renderTable(rows) {
    let html = "<table>";
    rows.forEach((row, index) => {
      if (/^\s*\|?[\s:\-|]+\|?\s*$/.test(row)) {
        return;
      }
      const tag = index === 0 ? "th" : "td";
      const cells = tableCells(row).map((cell) => `<${tag}>${cell}</${tag}>`);
      html += `<tr>${cells.join("")}</tr>`;
    });
    return html + "</table>";
  }

  function markdownToHtml(source) {
    const codeBlocks = [];
    const escaped = escapeHtml(source).replace(/```[a-z]*\n?([\s\S]*?)```/g, (match, code) => {
      codeBlocks.push(`<pre>${code}</pre>`);
      return `\u0000${codeBlocks.length - 1}\u0000`;
    });

    const lines = escaped.split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];

      if (/^\s*\|/.test(line)) {
        const rows = [];
        while (index < lines.length && /^\s*\|/.test(lines[index])) {
          rows.push(lines[index]);
          index++;
        }
        blocks.push(renderTable(rows));
        continue;
      }

      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        const level = heading[1].length + 1;
        blocks.push(`<h${level}>${formatInline(heading[2])}</h${level}>`);
        index++;
        continue;
      }

      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (index < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[index])) {
          const itemText = lines[index].replace(/^\s*([-*]|\d+\.)\s+/, "");
          items.push(`<li>${formatInline(itemText)}</li>`);
          index++;
        }
        const listTag = ordered ? "ol" : "ul";
        blocks.push(`<${listTag}>${items.join("")}</${listTag}>`);
        continue;
      }

      if (!line.trim()) {
        index++;
        continue;
      }

      const paragraph = [];
      while (
        index < lines.length &&
        lines[index].trim() &&
        !/^\s*(\||#|[-*]\s|\d+\.\s)/.test(lines[index])
      ) {
        paragraph.push(lines[index]);
        index++;
      }
      if (paragraph.length === 0) {
        paragraph.push(line);
        index++;
      }
      blocks.push(`<p>${formatInline(paragraph.join("<br>"))}</p>`);
    }

    return blocks.join("").replace(/\u0000(\d+)\u0000/g, (match, number) => codeBlocks[Number(number)]);
  }

  function shortArguments(args) {
    if (!args) {
      return "";
    }

    const parts = [];
    Object.keys(args).forEach((key) => {
      if (key === "reason") {
        return;
      }

      let value = args[key];
      if (typeof value === "object") {
        value = JSON.stringify(value);
      }
      value = String(value);
      if (key === "path") {
        value = value.split(/[\\/]/).pop();
      }
      if (value.length > 60) {
        value = value.slice(0, 57) + "…";
      }
      parts.push(`${key}=${value}`);
    });
    return parts.join(", ");
  }

  function renderToolCall(event) {
    const card = document.createElement("details");
    card.className = "tool run";

    const label = TOOL_LABELS[event.name] || event.name;
    const mcpBadge = event.source === "mcp" ? ' <span class="badge">MCP</span>' : "";
    card.innerHTML = `
      <summary>
        <span class="t-icon"><span class="spin"></span></span>
        <span class="t-name">${escapeHtml(label)}</span>${mcpBadge}
        <span class="t-args">${escapeHtml(shortArguments(event.args))}</span>
        <span class="t-meta"></span>
      </summary>
      <div class="t-body">
        <div class="t-label">${escapeHtml(event.name)} · аргументы</div>
        <pre>${escapeHtml(JSON.stringify(event.args, null, 2))}</pre>
        <div class="t-result"></div>
      </div>`;

    toolCards[toolCallId(event)] = card;
    appendToChat(card);
  }

  function toolCallId(event) {
    if (event.call_id) {
      return event.call_id;
    }
    const storedId = event.data && event.data.id;
    if (typeof storedId === "string") {
      return storedId;
    }
    return event.id;
  }

  function toolOutcome(event) {
    const nested = event.result && typeof event.result.ok === "boolean" ? event.result : null;
    const ok = typeof event.ok === "boolean" ? event.ok : Boolean(nested && nested.ok);
    return {
      ok,
      error: event.error || (nested && nested.error) || "",
      result: nested ? nested.result : event.result,
    };
  }

  function renderToolResult(event) {
    const callId = toolCallId(event);
    if (!toolCards[callId]) {
      renderToolCall({ call_id: callId, name: event.name, args: event.args || {}, source: event.source });
    }
    const card = toolCards[callId];
    const outcome = toolOutcome(event);

    card.className = outcome.ok ? "tool ok" : "tool fail";
    card.querySelector(".t-icon").textContent = outcome.ok ? "✓" : "✕";
    card.querySelector(".t-meta").textContent = event.duration !== null && event.duration !== undefined
      ? `${event.duration} с`
      : "";

    let body = outcome.ok ? JSON.stringify(outcome.result, null, 2) : outcome.error;
    if (body && body.length > 20000) {
      body = body.slice(0, 20000) + "\n…";
    }
    const title = outcome.ok ? "результат" : "ошибка";
    card.querySelector(".t-result").innerHTML = `<div class="t-label">${title}</div><pre>${escapeHtml(body)}</pre>`;

    if (!outcome.ok) {
      card.open = true;
    }
    if (STATE_CHANGING_TOOLS.test(event.name)) {
      scheduleStateRefresh();
    }
  }

  function renderSettingsChange(event) {
    const rows = (event.changes || []).map((change) => `
      <tr>
        <td><b>${escapeHtml(change.label || change.key)}</b> <span class="muted small">${escapeHtml(change.key)}</span></td>
        <td><span class="old">${escapeHtml(formatValue(change.old))}</span> → <b>${escapeHtml(formatValue(change.new))}</b></td>
      </tr>`);

    let html = '<div class="c-title"><img class="message-icon" src="icons/pencil.svg" alt=""> Изменены настройки</div>';
    if (rows.length > 0) {
      html += `<table>${rows.join("")}</table>`;
    }
    if (event.reason) {
      html += `<div class="reason">${escapeHtml(event.reason)}</div>`;
    }
    appendToChat(createDiv("card changes", html));
  }

  function renderSliceDone(result) {
    const info = result || {};
    const grams = info.material_g !== null && info.material_g !== undefined ? `${info.material_g} г` : "—";
    const meters = info.material_m !== null && info.material_m !== undefined ? `${info.material_m} м` : "—";
    const html = `
      <div class="c-title">✓ Слайсинг завершён</div>
      <div class="stats">
        <span><img class="stats-icon" src="icons/print_time.svg" alt=""> ${escapeHtml(info.print_time || "—")}</span>
        <span><img class="stats-icon" src="icons/dense.svg" alt=""> ${escapeHtml(grams)}</span>
        <span><img class="stats-icon" src="icons/scale_max.svg" alt=""> ${escapeHtml(meters)}</span>
      </div>`;
    appendToChat(createDiv("card result", html));
  }

  function renderSnapshot(event) {
    const caption = escapeHtml(event.caption || "Снимок сцены");
    const card = appendToChat(createDiv(
      "card snapshot",
      `<div class="c-title"><img class="message-icon" src="icons/viewmode.svg" alt=""> ${caption} <span class="muted small">— агент смотрит на модель</span></div>`
    ));

    const image = document.createElement("img");
    image.src = event.image;
    image.alt = "Снимок сцены";
    image.onload = () => {
      if (stickToBottom) {
        scrollToBottom();
      }
    };
    card.appendChild(image);
  }

  function renderSavedFile(event) {
    const kind = event.kind === "gcode" ? "G-code" : "Проект 3MF";
    let checkText = "";
    if (event.check) {
      const verdict = event.check.valid ? " ✓" : " ⚠ " + event.check.problems.join("; ");
      checkText = ` · слоёв: ${event.check.layers}${verdict}`;
    }
    const html = `<img class="load-icon" src="icons/load.svg" alt=""> ${kind}: <code>${escapeHtml(event.path)}</code>${escapeHtml(checkText)}`;
    appendToChat(createDiv("card", html));
  }

  function attachmentLabel(item) {
    if (typeof item === "string") {
      return item;
    }
    if (item && item.name) {
      return item.name;
    }
    if (item && item.kind === "image") {
      return "изображение";
    }
    return "";
  }

  function renderUserMessage(event) {
    const message = appendToChat(createDiv("msg user"));
    message.textContent = event.text || "";

    const labels = (event.attachments || []).map(attachmentLabel).filter(Boolean);
    if (labels.length > 0) {
      const attachmentLine = createDiv("att");
      attachmentLine.textContent = "📎 " + labels.join(", ");
      message.appendChild(attachmentLine);
    }
  }

  function optionsHtml(options, selectedValue) {
    return options
      .map((option) => {
        const selected = option.value === selectedValue ? " selected" : "";
        return `<option value="${escapeHtml(option.value)}"${selected}>${escapeHtml(option.label)}</option>`;
      })
      .join("");
  }

  function selectHtml(name, options, selectedValue) {
    const initial = selectedValue || "";
    const choices = options.slice();
    if (!choices.some((option) => option.value === initial)) {
      choices.unshift({ value: initial, label: initial || "—" });
    }
    return `<select name="${name}" data-initial="${escapeHtml(initial)}">${optionsHtml(choices, initial)}</select>`;
  }

  function numberInputHtml(name, value) {
    const initial = value === null || value === undefined ? "" : String(value);
    return `<input type="number" step="any" name="${name}" value="${escapeHtml(initial)}" data-initial="${escapeHtml(initial)}">`;
  }

  function materialLabel(material) {
    const parts = [material.brand, material.name].filter(Boolean);
    return parts.length > 0 ? parts.join(" ") : material.id;
  }

  function materialOptions(proposal) {
    const options = (proposal.materials || []).map((material) => ({
      value: material.id,
      label: materialLabel(material),
    }));

    const current = proposal.material;
    const hasCurrent = current && options.some((option) => option.value === current.id);
    if (current && !hasCurrent) {
      options.unshift({ value: current.id, label: current.name || current.id });
    }
    return options;
  }

  function reviewModeSection(proposal) {
    const currentMaterial = proposal.material ? proposal.material.id : "";
    return `
      <fieldset>
        <legend>Режим и материал</legend>
        <label>Режим печати ${selectHtml("print_mode", proposal.print_modes || [], proposal.print_mode)}</label>
        <label>Материал ${selectHtml("material_id", materialOptions(proposal), currentMaterial)}</label>
      </fieldset>`;
  }

  function reviewSettingsSection(settings) {
    const rows = settings.map((setting) => {
      const unit = setting.unit ? `, ${escapeHtml(setting.unit)}` : "";
      const label = escapeHtml(setting.label || setting.key);
      return `<label>${label}${unit} ${numberInputHtml("setting:" + setting.key, setting.value)}</label>`;
    });

    return `
      <fieldset>
        <legend>Параметры печати</legend>
        ${rows.join("")}
      </fieldset>`;
  }

  function reviewPlacementSection(objects) {
    if (objects.length === 0) {
      return "";
    }

    const first = objects[0];
    const center = first.center_mm || {};
    const scale = first.scale_pct || {};
    const objectChoices = objects.map((object) => ({
      value: object.object_id,
      label: `${object.object_id} ${object.name}`,
    }));

    return `
      <fieldset>
        <legend>Размещение модели</legend>
        <label>Объект ${selectHtml("object_id", objectChoices, first.object_id)}</label>
        <label>Центр X, мм ${numberInputHtml("x", center.x)}</label>
        <label>Центр Y, мм ${numberInputHtml("y", center.y)}</label>
        <label>Масштаб, % ${numberInputHtml("scale_percent", scale.x)}</label>
        <label>Повернуть вокруг X, ° ${numberInputHtml("rotate_x", 0)}</label>
        <label>Повернуть вокруг Y, ° ${numberInputHtml("rotate_y", 0)}</label>
        <label>Повернуть вокруг Z, ° ${numberInputHtml("rotate_z", 0)}</label>
      </fieldset>`;
  }

  function resetField(field, value) {
    field.value = value;
    field.dataset.initial = String(value);
  }

  function bindObjectSelect(form, objects) {
    if (!form.object_id) {
      return;
    }

    form.object_id.addEventListener("change", () => {
      const object = objects.find((item) => item.object_id === form.object_id.value);
      if (!object) {
        return;
      }
      form.object_id.dataset.initial = object.object_id;
      resetField(form.x, object.center_mm.x);
      resetField(form.y, object.center_mm.y);
      resetField(form.scale_percent, object.scale_pct.x);
    });
  }

  function changedFields(form) {
    return Array.from(form.querySelectorAll("[data-initial]")).filter((field) => {
      return field.value !== "" && field.value !== field.dataset.initial;
    });
  }

  function collectReviewChanges(form) {
    const changes = {};
    const settings = {};
    const placement = {};

    changedFields(form).forEach((field) => {
      if (field.name === "print_mode" || field.name === "material_id") {
        changes[field.name] = field.value;
      } else if (field.name.startsWith("setting:")) {
        settings[field.name.slice("setting:".length)] = Number(field.value);
      } else if (field.name !== "object_id") {
        placement[field.name] = Number(field.value);
      }
    });

    if (Object.keys(settings).length > 0) {
      changes.settings = settings;
    }
    if (Object.keys(placement).length > 0) {
      placement.object_id = form.object_id.value;
      changes.placement = placement;
    }
    return changes;
  }

  function setFormDisabled(form, disabled) {
    Array.from(form.elements).forEach((field) => {
      field.disabled = disabled;
    });
  }

  async function submitReview(reviewId, form) {
    const button = form.querySelector("button[type=submit]");
    const status = form.querySelector(".review-status");
    const payload = {
      review_id: reviewId,
      changes: collectReviewChanges(form),
      comment: form.comment.value,
    };

    button.disabled = true;
    try {
      await api("/api/review", payload);
      setFormDisabled(form, true);
      status.textContent = "Отправлено, применяю изменения…";
    } catch (error) {
      button.disabled = false;
      status.textContent = error.message;
    }
  }

  function renderReviewRequest(event) {
    const proposal = event.proposal || {};
    const card = createDiv("card review");
    card.innerHTML = `
      <div class="c-title">Проверьте параметры перед слайсингом</div>
      <div class="reason">${escapeHtml(event.summary || "")}</div>
      <form class="review-form">
        ${reviewModeSection(proposal)}
        ${reviewSettingsSection(proposal.settings || [])}
        ${reviewPlacementSection(proposal.objects || [])}
        <label class="review-comment">
          Комментарий для ассистента
          <textarea name="comment" rows="2" placeholder="Например: сделай стенки толще"></textarea>
        </label>
        <div class="review-actions">
          <button type="submit" class="primary">Продолжить</button>
          <span class="review-status muted small"></span>
        </div>
      </form>`;

    const form = card.querySelector("form");
    bindObjectSelect(form, proposal.objects || []);
    form.addEventListener("submit", (submitEvent) => {
      submitEvent.preventDefault();
      submitReview(event.review_id, form);
    });

    reviewCards[event.review_id] = card;
    appendToChat(card);
  }

  function reviewResultText(event) {
    let text = REVIEW_STATUS_TEXT[event.status] || event.status;

    if (event.status === "approved") {
      const applied = event.applied || [];
      if (applied.length > 0) {
        text += ". Изменено: " + applied.join("; ");
      } else {
        text += " без изменений";
      }
    }
    if (event.errors && event.errors.length > 0) {
      text += ". Не удалось: " + event.errors.join("; ");
    }
    return text;
  }

  function renderReviewClosed(event) {
    const card = reviewCards[event.review_id];
    if (!card) {
      return;
    }

    setFormDisabled(card.querySelector("form"), true);
    card.classList.add("closed");
    card.querySelector(".review-status").textContent = reviewResultText(event);
    scheduleStateRefresh();
  }

  function setSliceBar(state, progress) {
    const bar = element("slice-bar");
    const percent = Math.round((progress || 0) * 100);
    if (state !== "processing" || percent >= 100) {
      sliceOpen = false;
      bar.classList.add("hidden");
      return;
    }

    sliceOpen = true;
    bar.classList.remove("hidden");
    element("slice-progress").style.width = percent + "%";
    element("slice-pct").textContent = percent + "%";
    element("slice-label").textContent = percent === 0 ? "Новый слайсинг…" : "Слайсинг…";
  }

  function isOpenChatEvent(event) {
    if (!view.chatId) {
      return false;
    }
    if (!event.chat_id || event.chat_id === "default" || event.chat_id === view.chatId) {
      return true;
    }
    return false;
  }

  function statusText(state, tool) {
    if (state === "thinking") {
      return "Думает…";
    }
    if (state === "tool" && tool === "request_user_review") {
      return "Ждёт вашего подтверждения";
    }
    if (state === "tool") {
      return "Выполняет: " + (TOOL_LABELS[tool] || tool);
    }
    if (state === "error") {
      return "Ошибка";
    }
    return "Готов";
  }

  function renderStatus() {
    const pill = element("status");
    const runningHere = runningIds.has(view.chatId);

    pill.className = "pill";
    pill.title = "";
    pill.onclick = null;
    if (runningHere) {
      const ownState = agentState.chatId === view.chatId ? agentState.state : "thinking";
      const state = ownState === "idle" ? "thinking" : ownState;
      pill.textContent = statusText(state, agentState.tool);
    } else if (agentState.state === "error" && agentState.chatId === view.chatId) {
      pill.classList.add("error");
      pill.textContent = statusText("error");
    } else {
      pill.classList.add("idle");
      pill.textContent = statusText("idle");
    }

    element("btn-send").classList.toggle("hidden", runningHere);
    element("btn-stop").classList.toggle("hidden", !runningHere);
    element("btn-send").disabled = sending;
    element("busy-note").classList.add("hidden");
  }

  function handleStatusEvent(event) {
    if (event.chat_id && event.chat_id !== view.chatId) {
      return;
    }
    agentState = { state: event.state, tool: event.tool, chatId: event.chat_id };
    renderStatus();
  }

  function handleSliceState(event, replaying) {
    if (!replaying) {
      setSliceBar(event.state, 0);
    }
    if (event.state === "done") {
      renderSliceDone(event.result);
    }
    if (event.state === "error" || event.state === "disabled") {
      appendToChat(createDiv("msg error", escapeHtml(event.message || "Ошибка слайсинга")));
    }
    scheduleStateRefresh();
  }

  function resetChat() {
    element("messages").innerHTML = welcomeHtml;
    bindExamples();
    toolCards = {};
    reviewCards = {};
  }

  // После показа сохранённой истории: незавершённое в чате, где агент уже не работает, помечаем прерванным.
  function finishReplay(chatIsRunning) {
    if (chatIsRunning) {
      return;
    }
    Object.keys(toolCards).forEach((callId) => {
      const card = toolCards[callId];
      if (!card.classList.contains("run")) {
        return;
      }
      card.className = "tool fail";
      card.querySelector(".t-icon").textContent = "–";
      card.querySelector(".t-meta").textContent = "прервано";
    });
    Object.keys(reviewCards).forEach((reviewId) => {
      const card = reviewCards[reviewId];
      if (card.classList.contains("closed")) {
        return;
      }
      setFormDisabled(card.querySelector("form"), true);
      card.classList.add("closed");
      card.querySelector(".review-status").textContent = "Проверка не завершена";
    });
  }

  function handleEvent(rawEvent, replaying) {
    const payload = rawEvent.data || {};
    const event = Object.assign({}, payload, rawEvent);
    switch (event.type) {
      case "user":
        renderUserMessage(event);
        break;
      case "assistant":
        appendToChat(createDiv("msg assistant", markdownToHtml(event.text || "")));
        break;
      case "tool_call":
        renderToolCall(event);
        break;
      case "tool_result":
        renderToolResult(event);
        break;
      case "settings_changed":
        renderSettingsChange(event);
        scheduleStateRefresh();
        break;
      case "profile_changed":
      case "scene_changed":
        scheduleStateRefresh();
        break;
      case "slice_state":
        handleSliceState(event, replaying);
        break;
      case "snapshot":
        renderSnapshot(event);
        break;
      case "file_saved":
        renderSavedFile(event);
        break;
      case "review_requested":
        renderReviewRequest(event);
        break;
      case "review_closed":
        renderReviewClosed(event);
        break;
      case "error":
        appendToChat(createDiv("msg error", "⚠ " + escapeHtml(event.message)));
        if (!replaying) {
          agentState = { state: "error", tool: null, chatId: event.chat_id };
          renderStatus();
        }
        break;
    }
  }

  // Каждое событие шины проходит здесь: общее (статус, список чатов, слайсер) — обрабатываем всегда,
  // события открытого чата — рисуем, остальные чаты только помечаем непрочитанными.
  function dispatchEvent(event) {
    if (event.type === "chats") {
      handleChatsEvent(event);
      return;
    }
    if (event.type === "status") {
      handleStatusEvent(event);
      return;
    }
    if (event.type === "slice_progress") {
      if (isOpenChatEvent(event) && sliceOpen) {
        setSliceBar("processing", event.progress);
      }
      return;
    }

    if (!isOpenChatEvent(event)) {
      if (UNREAD_EVENTS.includes(event.type)) {
        markUnread(event.chat_id);
      }
      return;
    }

    if (event.type === "slice_state") {
      setSliceBar(event.state, event.state === "processing" ? 0 : 1);
    }

    if (view.loading) {
      view.queue.push(event);
      return;
    }
    if (event.seq > view.seq) {
      handleEvent(event, false);
    }
  }

  async function pollEvents() {
    try {
      const response = await fetch(`/api/events?since=${lastEventSeq}&wait=20&session=${serverSession}`);
      const data = await response.json();
      if (!response.ok || !data.session) {
        throw new Error(data.error || "События недоступны");
      }
      if (data.session !== serverSession) {
        // Слайсер перезапустили: номера событий начались заново — перечитываем чаты.
        start();
        return;
      }
      const events = data.events || [];

      events.forEach((event) => {
        lastEventSeq = Math.max(lastEventSeq, event.id || event.seq || 0);
        dispatchEvent(event);
      });
      syncRunningChats(data.running_chats, data.running_chat);
      pollEvents();
    } catch (error) {
      const pill = element("status");
      pill.className = "pill error";
      pill.onclick = null;
      pill.textContent = "Нет связи со слайсером";
      setTimeout(pollEvents, 2000);
    }
  }

  function sameRunningSet(nextIds) {
    if (nextIds.size !== runningIds.size) {
      return false;
    }
    for (const id of nextIds) {
      if (!runningIds.has(id)) {
        return false;
      }
    }
    return true;
  }

  function syncRunningChats(runningChats, runningChat) {
    const nextIds = new Set(
      Array.isArray(runningChats) ? runningChats : (runningChat ? [runningChat] : [])
    );
    if (sameRunningSet(nextIds)) {
      return;
    }
    runningIds = nextIds;
    renderStatus();
    renderChatList();
  }

  function markUnread(chatId) {
    if (!chatId || chatId === view.chatId || unreadChats.has(chatId)) {
      return;
    }
    unreadChats.add(chatId);
    renderChatList();
  }

  function handleChatsEvent(event) {
    if (event.action === "run_started" && event.target) {
      runningIds.add(event.target);
    } else if (event.action === "run_finished" && event.target) {
      runningIds.delete(event.target);
      if (agentState.chatId === event.target) {
        agentState = { state: "idle", tool: null, chatId: null };
      }
    }
    renderStatus();
    scheduleChatsRefresh();
  }

  function scheduleChatsRefresh() {
    clearTimeout(chatsRefreshTimer);
    chatsRefreshTimer = setTimeout(refreshChats, 150);
  }

  async function refreshChats() {
    let listing;
    try {
      listing = await api("/api/chats");
    } catch (error) {
      return;
    }
    applyListing(listing);
    if (view.chatId && !chats.some((chat) => chat.id === view.chatId)) {
      openChat(listing.active_id);
    }
  }

  function applyListing(listing) {
    chats = listing.chats || [];
    runningIds = new Set(
      listing.running_ids || chats.filter((chat) => chat.running).map((chat) => chat.id)
    );
    renderChatList();
    renderStatus();
    updateDocumentTitle();
  }

  function updateDocumentTitle() {
    const chat = chats.find((item) => item.id === view.chatId);
    document.title = chat && !chat.empty ? `${chat.title} · STE AI-ассистент` : "STE AI-ассистент";
  }

  function formatChatTime(timestamp) {
    const date = new Date(timestamp * 1000);
    const time = date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    if (date.toDateString() === new Date().toDateString()) {
      return time;
    }
    return `${date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })} ${time}`;
  }

  function chatMetaHtml(chat) {
    if (runningIds.has(chat.id) || chat.running) {
      return '<span class="spin"></span> работает';
    }
    if (chat.empty) {
      return "пустой";
    }
    return escapeHtml(formatChatTime(chat.updated));
  }

  function renderChatList() {
    if (renamingChatId) {
      return;
    }

    const list = element("chat-list");
    list.innerHTML = "";
    chats.forEach((chat) => {
      const item = document.createElement("li");
      item.className = "chat-item";
      item.dataset.id = chat.id;
      item.classList.toggle("active", chat.id === view.chatId);
      item.classList.toggle("running", runningIds.has(chat.id) || chat.running);
      item.classList.toggle("unread", unreadChats.has(chat.id));
      item.innerHTML = `
        <div class="ci-main">
          <div class="ci-title" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title)}</div>
          <div class="ci-meta">${chatMetaHtml(chat)}</div>
        </div>
        <span class="ci-dot" title="Новый ответ"></span>
        <div class="ci-actions">
          <button type="button" class="ci-btn" data-action="rename" title="Переименовать"><img src="icons/pencil.svg" alt=""></button>
          <button type="button" class="ci-btn" data-action="delete" title="Удалить чат"><img src="icons/close.svg" alt=""></button>
        </div>`;
      list.appendChild(item);
    });
  }

  function onChatListClick(event) {
    const item = event.target.closest(".chat-item");
    if (!item || event.target.closest(".ci-input")) {
      return;
    }

    const button = event.target.closest(".ci-btn");
    if (button && button.dataset.action === "rename") {
      startRename(item.dataset.id);
    } else if (button && button.dataset.action === "delete") {
      deleteChat(item.dataset.id);
    } else if (item.dataset.id !== view.chatId) {
      openChat(item.dataset.id);
    } else {
      closeChatsDrawer();
    }
  }

  async function openChat(chatId) {
    if (!chatId) {
      return;
    }

    view = { chatId: chatId, seq: Infinity, loading: true, queue: [] };
    unreadChats.delete(chatId);
    renderChatList();
    renderStatus();
    updateDocumentTitle();
    closeChatsDrawer();
    api("/api/chats/select", { chat_id: chatId }).catch(() => {});

    let history;
    try {
      history = await api(`/api/chat?id=${encodeURIComponent(chatId)}`);
    } catch (error) {
      if (view.chatId === chatId) {
        view = { chatId: chatId, seq: lastEventSeq, loading: false, queue: [] };
        resetChat();
        appendToChat(createDiv("msg error", "⚠ Не удалось открыть чат: " + escapeHtml(error.message)));
        refreshChats();
      }
      return;
    }
    if (view.chatId !== chatId) {
      return;
    }
    showHistory(history);
  }

  function showHistory(history) {
    resetChat();
    stickToBottom = true;
    if (history.events.length === 0 && (history.chat.running || !history.chat.empty)) {
      element("messages").innerHTML = "";
      const text = history.chat.running
        ? "Этот чат ещё числится запущенным, поэтому вместо «Отправить» стоит «Стоп». Нажмите «Стоп», чтобы освободить его. Лента прошлого запуска не сохранилась."
        : "Лента этого чата не сохранилась. Можно отправить новое сообщение.";
      appendToChat(createDiv("msg", escapeHtml(text)));
    } else {
      history.events.forEach((event) => handleEvent(event, true));
    }
    finishReplay(history.chat.running);

    const queued = view.queue;
    view.seq = history.seq;
    view.loading = false;
    view.queue = [];
    queued.forEach((event) => {
      if (event.seq > view.seq) {
        handleEvent(event, false);
      }
    });

    scrollToBottom();
    renderStatus();
    element("input").focus();
  }

  async function createChat() {
    try {
      const data = await api("/api/chats/new", {});
      await refreshChats();
      openChat(data.chat.id);
    } catch (error) {
      appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
    }
  }

  function startRename(chatId) {
    const chat = chats.find((item) => item.id === chatId);
    const item = element("chat-list").querySelector(`[data-id="${chatId}"]`);
    if (!chat || !item) {
      return;
    }

    const input = document.createElement("input");
    input.className = "ci-input";
    input.value = chat.title;
    input.maxLength = 80;
    item.querySelector(".ci-title").replaceWith(input);
    renamingChatId = chatId;
    input.focus();
    input.select();

    let finished = false;
    const finish = async (save) => {
      if (finished) {
        return;
      }
      finished = true;
      renamingChatId = null;

      const title = input.value.trim();
      if (save && title && title !== chat.title) {
        try {
          await api("/api/chats/rename", { chat_id: chatId, title: title });
        } catch (error) {
          appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
        }
      }
      refreshChats();
    };

    input.addEventListener("keydown", (keyEvent) => {
      if (keyEvent.key === "Enter") {
        keyEvent.preventDefault();
        finish(true);
      } else if (keyEvent.key === "Escape") {
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
  }

  async function deleteChat(chatId) {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat) {
      return;
    }
    if (runningIds.has(chatId) || chat.running) {
      alert("В этом чате сейчас работает ассистент. Остановите его, затем удалите чат.");
      return;
    }
    const question = `Удалить чат «${chat.title}»? Переписка удалится без возможности восстановления. ` +
      "Модели и настройки в слайсере не изменятся.";
    if (!chat.empty && !confirm(question)) {
      return;
    }

    try {
      const listing = await api("/api/chats/delete", { chat_id: chatId });
      unreadChats.delete(chatId);
      applyListing(listing);
      if (chatId === view.chatId) {
        openChat(listing.active_id);
      }
    } catch (error) {
      appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
    }
  }

  function closeChatsDrawer() {
    element("chats").classList.remove("open");
  }

  function scheduleStateRefresh() {
    clearTimeout(stateRefreshTimer);
    stateRefreshTimer = setTimeout(refreshState, 400);
  }

  function stateHtml(state) {
    const mode = state.print_mode || {};
    const quality = state.quality || {};
    const material = state.material || {};
    const slicing = state.slicing || {};

    let html = `
      <dl>
        <dt>Принтер</dt><dd>${escapeHtml(state.printer)}</dd>
        <dt>Режим</dt><dd>${escapeHtml(mode.label || mode.mode || mode.error || "—")}</dd>
        <dt>Профиль</dt><dd>${escapeHtml(quality.name || quality.quality_type || "—")}</dd>
        <dt>Материал</dt><dd>${escapeHtml(material.name || "—")}</dd>
        <dt>Слайсинг</dt><dd>${escapeHtml(slicing.state || "—")}</dd>
      </dl>`;

    (state.objects || []).forEach((object) => {
      const size = `${object.size_mm.x} × ${object.size_mm.y} × ${object.size_mm.z} мм`;
      const outside = object.outside_build_area ? ' <span style="color:var(--err)">вне стола</span>' : "";
      html += `
        <div class="obj">
          <b>${escapeHtml(object.object_id)}</b> ${escapeHtml(object.name)}<br>
          ${escapeHtml(size)}${outside}
        </div>`;
    });
    return html;
  }

  async function refreshState() {
    const box = element("state");
    try {
      const data = await api("/api/state");
      if (!data.ok) {
        box.innerHTML = `<span class="muted">${escapeHtml(data.error)}</span>`;
        return;
      }
      box.innerHTML = stateHtml(data.state);
    } catch (error) {
      box.textContent = "Слайсер занят, повторяю запрос…";
      clearTimeout(stateRefreshTimer);
      stateRefreshTimer = setTimeout(refreshState, 5000);
    }
  }

  async function stopRun() {
    const button = element("btn-stop");
    button.disabled = true;
    try {
      await api("/api/stop", { chat_id: view.chatId });
    } catch (error) {
      appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
    } finally {
      button.disabled = false;
    }
  }

  function fileExtension(fileName) {
    return fileName.split(".").pop().toLowerCase();
  }

  function attachmentKind(extension) {
    if (DOCUMENT_EXTENSIONS.includes(extension)) {
      return "doc";
    }
    if (IMAGE_EXTENSION_PATTERN.test(extension)) {
      return "image";
    }
    return "model";
  }

  function addFiles(fileList) {
    Array.from(fileList).forEach((file) => {
      const extension = fileExtension(file.name);
      if (!ALLOWED_EXTENSIONS.includes(extension)) {
        appendToChat(createDiv(
          "msg error",
          `Неподдерживаемый файл: ${escapeHtml(file.name)} (модели STL/OBJ/3MF, фото JPG/PNG, документы DOCX/TXT)`
        ));
        return;
      }

      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = reader.result;
        const kind = attachmentKind(extension);
        attachments.push({
          name: file.name,
          data: dataUrl.slice(dataUrl.indexOf(",") + 1),
          preview: kind === "image" ? dataUrl : null,
          kind: kind,
        });
        renderAttachments();
      };
      reader.readAsDataURL(file);
    });
  }

  function attachmentIcon(attachment) {
    if (attachment.preview) {
      return `<img src="${attachment.preview}" alt="">`;
    }
    if (attachment.kind === "doc") {
      return "📄";
    }
    return "🧊";
  }

  function renderAttachments() {
    const box = element("attachments");
    box.innerHTML = "";

    attachments.forEach((attachment, index) => {
      const chip = createDiv(
        "chip",
        `${attachmentIcon(attachment)}${escapeHtml(attachment.name)} <button type="button" title="Убрать">✕</button>`
      );
      chip.querySelector("button").onclick = () => {
        attachments.splice(index, 1);
        renderAttachments();
      };
      box.appendChild(chip);
    });
  }

  async function sendMessage() {
    const text = element("input").value.trim();
    if (!text && attachments.length === 0) {
      return;
    }

    const payload = {
      chat_id: view.chatId,
      text: text,
      attachments: attachments.map((attachment) => ({ name: attachment.name, data: attachment.data })),
    };

    sending = true;
    renderStatus();
    stickToBottom = true;
    try {
      await api("/api/message", payload);
      element("input").value = "";
      attachments = [];
      renderAttachments();
    } catch (error) {
      appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
      if (/настройках/.test(error.message)) {
        openSettings();
      }
    } finally {
      sending = false;
      renderStatus();
    }
  }

  async function openSettings() {
    const config = await api("/api/config");
    const form = element("settings-form");

    form.base_url.value = config.base_url || "";
    form.model.value = config.model || "";
    form.api_key.value = "";
    form.verify_ssl.checked = Boolean(config.verify_ssl);
    form.output_dir.value = config.output_dir || "";

    element("key-hint").textContent = config.api_key_set
      ? `Ключ сохранён (${config.api_key_hint}). Оставьте поле пустым, чтобы не менять.`
      : "Ключ не задан.";
    element("settings-msg").textContent = "";
    element("settings").showModal();
  }

  function saveSettings() {
    const form = element("settings-form");
    return api("/api/config", {
      base_url: form.base_url.value.trim(),
      model: form.model.value.trim(),
      api_key: form.api_key.value.trim(),
      verify_ssl: form.verify_ssl.checked,
      output_dir: form.output_dir.value.trim(),
    });
  }

  async function loadConfig() {
    const config = await api("/api/config");
    const toolCount = element("tool-count");

    element("mcp-url").textContent = config.mcp_url;
    toolCount.textContent = `Инструментов: ${(config.tools || []).length} · плагин v${config.plugin_version || "?"}`;
    toolCount.title = config.plugin_dir || "";
    element("prompt-kind").textContent = promptKindText(config.system_prompt_custom);

    if (!config.llm_ready) {
      openSettings();
    }
  }

  function promptKindText(isCustom) {
    return isCustom ? "Используется свой промпт" : "Используется стандартный промпт";
  }

  function showPromptState(state) {
    const form = element("prompt-form");
    form.text.value = state.text;
    element("prompt-info").textContent =
      `${promptKindText(state.is_custom)}. Изменения действуют со следующего запроса к модели. Файл: ${state.path}`;
    element("prompt-kind").textContent = promptKindText(state.is_custom);
  }

  function showPromptMessage(text, isError) {
    const className = isError ? "msg-err" : "msg-ok";
    element("prompt-msg").innerHTML = `<span class="${className}">${escapeHtml(text)}</span>`;
  }

  async function openPromptEditor() {
    try {
      showPromptState(await api("/api/system_prompt"));
      element("prompt-msg").textContent = "";
      element("prompt-dialog").showModal();
    } catch (error) {
      appendToChat(createDiv("msg error", "⚠ " + escapeHtml(error.message)));
    }
  }

  async function savePrompt(event) {
    event.preventDefault();
    try {
      const state = await api("/api/system_prompt", { text: element("prompt-form").text.value });
      showPromptState(state);
      showPromptMessage(state.is_custom ? "Свой промпт сохранён" : "Совпадает со стандартным — используется стандартный", false);
    } catch (error) {
      showPromptMessage(error.message, true);
    }
  }

  async function resetPrompt() {
    if (!confirm("Вернуть стандартный промпт? Ваш промпт будет удалён.")) {
      return;
    }
    try {
      showPromptState(await api("/api/system_prompt/reset", {}));
      showPromptMessage("Возвращён стандартный промпт", false);
    } catch (error) {
      showPromptMessage(error.message, true);
    }
  }

  function showSettingsMessage(text, isError) {
    const className = isError ? "msg-err" : "msg-ok";
    element("settings-msg").innerHTML = `<span class="${className}">${escapeHtml(text)}</span>`;
  }

  async function testConnection() {
    element("settings-msg").textContent = "Проверяю…";
    try {
      await saveSettings();
      const result = await api("/api/test_llm", {});
      if (result.ok) {
        showSettingsMessage(`✓ Подключение работает (${result.model}, ${result.latency_s} с)`, false);
      } else {
        showSettingsMessage("✕ " + result.error, true);
      }
    } catch (error) {
      showSettingsMessage("✕ " + error.message, true);
    }
  }

  async function submitSettings(event) {
    event.preventDefault();
    try {
      await saveSettings();
      element("settings").close();
      loadConfig();
    } catch (error) {
      showSettingsMessage(error.message, true);
    }
  }

  function downloadScreenshot() {
    const background = getComputedStyle(document.body).backgroundColor;
    window.html2canvas(element("messages"), { backgroundColor: background, scale: 1.5 }).then((canvas) => {
      const link = document.createElement("a");
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      link.download = `chat_${timestamp}.png`;
      link.href = canvas.toDataURL("image/png");
      link.click();
    });
  }

  function takeScreenshot() {
    if (window.html2canvas) {
      downloadScreenshot();
      return;
    }

    const script = document.createElement("script");
    script.src = HTML2CANVAS_URL;
    script.onload = downloadScreenshot;
    script.onerror = () => {
      alert("Нет доступа к интернету для html2canvas — сделайте скриншот средствами ОС.");
    };
    document.head.appendChild(script);
  }

  function bindExamples() {
    document.querySelectorAll(".example").forEach((button) => {
      button.onclick = () => {
        element("input").value = button.textContent;
        element("input").focus();
      };
    });
  }

  function bindDragAndDrop() {
    const dropOverlay = element("drop");

    window.addEventListener("dragenter", (event) => {
      event.preventDefault();
      dragDepth++;
      dropOverlay.classList.remove("hidden");
    });

    window.addEventListener("dragleave", () => {
      dragDepth--;
      if (dragDepth <= 0) {
        dragDepth = 0;
        dropOverlay.classList.add("hidden");
      }
    });

    window.addEventListener("dragover", (event) => {
      event.preventDefault();
    });

    window.addEventListener("drop", (event) => {
      event.preventDefault();
      dragDepth = 0;
      dropOverlay.classList.add("hidden");
      if (event.dataTransfer && event.dataTransfer.files.length > 0) {
        addFiles(event.dataTransfer.files);
      }
    });
  }

  function bindEvents() {
    element("scroll").addEventListener("scroll", () => {
      const scroll = element("scroll");
      stickToBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
    });

    element("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage();
    });

    element("input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    element("file-input").addEventListener("change", (event) => {
      addFiles(event.target.files);
      event.target.value = "";
    });

    element("btn-stop").addEventListener("click", stopRun);
    element("btn-new-chat").addEventListener("click", createChat);
    element("btn-chats").addEventListener("click", () => element("chats").classList.toggle("open"));
    document.querySelector("main.chat").addEventListener("click", closeChatsDrawer);
    element("chat-list").addEventListener("click", onChatListClick);
    element("chat-list").addEventListener("dblclick", (event) => {
      const item = event.target.closest(".chat-item");
      if (item && event.target.closest(".ci-title")) {
        startRename(item.dataset.id);
      }
    });
    element("btn-settings").addEventListener("click", openSettings);
    element("btn-shot").addEventListener("click", takeScreenshot);
    element("btn-cancel").addEventListener("click", () => element("settings").close());
    element("btn-test").addEventListener("click", testConnection);
    element("btn-prompt").addEventListener("click", openPromptEditor);
    element("btn-prompt-cancel").addEventListener("click", () => element("prompt-dialog").close());
    element("btn-prompt-reset").addEventListener("click", resetPrompt);
    element("prompt-form").addEventListener("submit", savePrompt);
    element("settings-form").addEventListener("submit", submitSettings);

    bindExamples();
    bindDragAndDrop();
  }

  async function start() {
    let listing;
    try {
      listing = await api("/api/chats");
    } catch (error) {
      const pill = element("status");
      pill.className = "pill error";
      pill.textContent = "Нет связи со слайсером";
      setTimeout(start, 2000);
      return;
    }
    lastEventSeq = listing.seq;
    serverSession = listing.session;
    applyListing(listing);
    openChat(listing.active_id);
    pollEvents();
  }

  bindEvents();
  loadConfig();
  refreshState();
  start();
})();
