/* Home — the only client-side code: search filter, clock, link-mode switch and
   broken-icon fallback. The page itself is rendered by the server on every
   request; no config is ever fetched from here. */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- поиск */
  var input = document.getElementById("search");
  var noResults = document.getElementById("no-results");
  var clearButton = document.getElementById("clear-search");
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".panel"));
  var columns = Array.prototype.slice.call(document.querySelectorAll(".column"));

  function applyFilter() {
    var query = (input && input.value || "").trim().toLowerCase();
    var visible = 0;
    cards.forEach(function (card) {
      var hit = !query || (card.dataset.search || "").indexOf(query) !== -1;
      card.hidden = !hit;
      if (hit) { visible += 1; }
    });
    panels.forEach(function (panel) {
      var any = Array.prototype.some.call(panel.querySelectorAll(".card"), function (card) { return !card.hidden; });
      panel.hidden = !any && !!query;
    });
    columns.forEach(function (column) {
      column.hidden = !column.querySelector(".panel:not([hidden])");
    });
    if (noResults) { noResults.hidden = visible !== 0 || !query; }
  }

  if (input) {
    input.addEventListener("input", applyFilter);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { input.value = ""; applyFilter(); input.blur(); }
    });
  }
  if (clearButton) {
    clearButton.addEventListener("click", function () {
      if (input) { input.value = ""; applyFilter(); input.focus(); }
    });
  }
  document.addEventListener("keydown", function (event) {
    var typing = /^(input|textarea|select)$/i.test(event.target.tagName);
    if ((event.key === "/" && !typing) || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k")) {
      event.preventDefault();
      if (input) { input.focus(); input.select(); }
    }
  });

  /* --------------------------------------------- основная ссылка или alt */
  /* Каждая ссылка несёт оба адреса: data-main и data-alt (alt пуст, если
     у сервиса он не задан — тогда всегда открывается основная). */
  var MODE_KEY = "home.linkMode";
  var switchButtons = Array.prototype.slice.call(document.querySelectorAll(".linkmode-btn"));
  var links = Array.prototype.slice.call(document.querySelectorAll(".card-link, .card-alt"));
  var chips = Array.prototype.slice.call(document.querySelectorAll(".card-alt"));
  /* приоритет: ?links= / localStorage (ставит шапка), иначе выбор сервера */
  var root = document.documentElement;
  var initial = root.dataset.linkMode || root.dataset.defaultLinkMode || "main";
  var mode = initial === "alt" ? "alt" : "main";

  function applyMode(next, remember) {
    mode = next === "alt" ? "alt" : "main";
    root.dataset.linkMode = mode;
    links.forEach(function (link) {
      var target = mode === "alt" ? link.dataset.alt : link.dataset.main;
      if (target) { link.setAttribute("href", target); }
    });
    /* подсказка чипа описывает ту ссылку, на которую он теперь ведёт */
    chips.forEach(function (chip) {
      var hint = mode === "alt" ? chip.dataset.titleAlt : chip.dataset.titleMain;
      if (hint) { chip.dataset.tip = hint; }
    });
    switchButtons.forEach(function (button) {
      button.setAttribute("aria-pressed", button.dataset.mode === mode ? "true" : "false");
    });
    if (remember) {
      try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
    }
  }

  switchButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.dataset.mode === mode) { return; }
      applyMode(button.dataset.mode, true);
    });
  });
  if (switchButtons.length) { applyMode(mode, false); }

  /* --------------------------------------------------------------- часы */
  var clockTime = document.getElementById("clock-time");
  var clockDate = document.getElementById("clock-date");
  /* [header] time_format = 12 -> AM/PM, 24 -> 24-часовой */
  var hour12 = root.dataset.timeFormat !== "24";
  var timeFormat = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: hour12 };
  var dateFormat = { weekday: "short", day: "numeric", month: "short" };

  function tick() {
    var now = new Date();
    /* en-US даёт именно AM/PM — русская локаль отдаёт «ДП/ПП» */
    clockTime.textContent = now.toLocaleTimeString("en-US", timeFormat);
    clockDate.textContent = now.toLocaleDateString("ru-RU", dateFormat);
  }

  if (clockTime && clockDate) {
    tick();
    setInterval(tick, 1000);
  }

  /* -------------------------------------------------- битые иконки (img) */
  /* Локальные svg вставляются инлайном и не ломаются; тут — png/jpg и
     внешние icon_link, которые могут не загрузиться. */
  document.querySelectorAll(".card-icon img").forEach(function (img) {
    img.addEventListener("error", function () {
      var span = document.createElement("span");
      span.className = "placeholder";
      span.textContent = img.dataset.fallback || "?";
      img.replaceWith(span);
    });
  });

  /* ------------------------------------------------------------ подсказки */
  /* Своя подсказка вместо title: родная всплывает почти через секунду, а
     причина ошибки и пинг нужны сразу. Текст живёт в data-tip и адресов не
     содержит — это забота сервера, как и у ping. */
  var TIP_DELAY = 90;
  var tipNode = null;
  var tipTarget = null;
  var tipTimer = null;

  function tipFor(node) {
    return node && node.closest ? node.closest("[data-tip]") : null;
  }

  function placeTip(el) {
    var box = el.getBoundingClientRect();
    var own = tipNode.getBoundingClientRect();
    var gap = 7;
    var left = box.left + (box.width - own.width) / 2;
    var top = box.top - own.height - gap;
    if (top < 8) { top = Math.min(box.bottom + gap, window.innerHeight - own.height - 8); }
    left = Math.max(8, Math.min(left, window.innerWidth - own.width - 8));
    tipNode.style.left = Math.round(left) + "px";
    tipNode.style.top = Math.round(top) + "px";
  }

  function openTip(el) {
    var text = (el.dataset.tip || "").trim();
    if (!text) { return; }
    if (!tipNode) {
      tipNode = document.createElement("div");
      tipNode.className = "tip";
      tipNode.setAttribute("role", "tooltip");
      document.body.appendChild(tipNode);
    }
    tipNode.textContent = text;
    tipNode.classList.add("on");
    placeTip(el);
  }

  function closeTip() {
    clearTimeout(tipTimer);
    tipTimer = null;
    tipTarget = null;
    if (tipNode) { tipNode.classList.remove("on"); }
  }

  function armTip(el, now) {
    if (!el || el === tipTarget) { return; }
    closeTip();
    tipTarget = el;
    if (now) { openTip(el); return; }
    tipTimer = setTimeout(function () { openTip(el); }, TIP_DELAY);
  }

  document.addEventListener("mouseover", function (event) {
    var el = tipFor(event.target);
    if (!el || tipFor(event.relatedTarget) === el) { return; }
    armTip(el, false);
  });
  document.addEventListener("mouseout", function (event) {
    var el = tipFor(event.target);
    if (!el || tipFor(event.relatedTarget) === el) { return; }
    if (el === tipTarget) { closeTip(); }
  });
  document.addEventListener("focusin", function (event) { armTip(tipFor(event.target), true); });
  document.addEventListener("focusout", function (event) {
    if (tipFor(event.target) === tipTarget) { closeTip(); }
  });
  document.addEventListener("click", closeTip, true);
  window.addEventListener("scroll", closeTip, true);
  window.addEventListener("blur", closeTip);

  /* ------------------------------------------------ живые карточки (ping) */
  /* Состояние всё считает сервер: здесь только раз в system.poll секунд
     спросить /api/state и перекрасить уже нарисованное. */
  var liveNode = document.getElementById("live-state");
  if (liveNode) {
    var live = {};
    try {
      live = JSON.parse(liveNode.textContent || "{}") || {};
    } catch (error) {
      live = {};
    }

    var pollMs = Math.max(3, Number(live.poll) || 10) * 1000;
    var timer = null;
    var busy = false;
    var failures = 0;

    function paintPing(card, ping) {
      var node = card.querySelector(".card-ping");
      if (!node) return;
      var state = ping.state || "unknown";
      ["ok", "slow", "down", "unknown"].forEach(function (name) {
        node.classList.toggle("state-" + name, name === state);
      });
      var words = { ok: "Up", slow: "Slow", down: "Down" };   // те же слова, что в hp/widgets.py
      node.dataset.tip = "Ping: " + (words[state] || "Unknown") +
        (ping.ms === null || ping.ms === undefined ? "" : " · " + ping.ms + " ms");
    }

    function paintWidget(card, widget) {
      var box = card.querySelector(".card-widget");
      if (!box) return;
      // поля перекрашиваем всегда: на ошибке сервер присылает кресты вместо старых цифр
      (widget.fields || []).forEach(function (field) {
        var cell = box.querySelector('.widget-field[data-field="' + field.key + '"]');
        if (!cell) return;
        var value = cell.querySelector(".widget-value");
        if (!value) return;
        value.textContent = field.value === null || field.value === undefined ? "—" : field.value;
        value.className = "widget-value tone-" + (field.tone || "idle");
      });
      if (widget.ok) {
        box.dataset.phase = "ok";
        delete box.dataset.tip;
      } else if (widget.error) {
        box.dataset.phase = "error";
        box.dataset.tip = widget.error;   // причина в подсказке, без адреса
      }
    }

    function applyState(payload) {
      var states = (payload && payload.cards) || {};
      Array.prototype.forEach.call(document.querySelectorAll(".card[data-key]"), function (card) {
        var state = states[card.dataset.key];
        if (!state) return;
        if (state.ping) paintPing(card, state.ping);
        if (state.widget) paintWidget(card, state.widget);
      });
    }

    function refresh() {
      if (busy || document.hidden) return;
      busy = true;
      fetch("/api/state", { cache: "no-store", headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (payload) {
          failures = 0;
          document.body.classList.remove("live-stale");
          applyState(payload);
        })
        .catch(function () {
          /* сеть пропала: последние цифры остаются на месте, но гаснут */
          failures += 1;
          if (failures >= 2) document.body.classList.add("live-stale");
        })
        .then(function () {
          busy = false;
        });
    }

    function schedule() {
      if (timer) clearInterval(timer);
      timer = document.hidden ? null : setInterval(refresh, pollMs);
    }

    applyState(live);
    refresh();      // первый запрос сразу: кэш на сервере общий, лишнего похода в чужие API нет
    schedule();
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refresh();
      schedule();
    });
  }
})();
