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
})();
