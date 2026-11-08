/* Home — the only client-side code: search filter, clock, broken-icon fallback.
   The page itself is rendered by the server on every request; no config is
   ever fetched from here. */
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

  /* --------------------------------------------------------------- часы */
  var clockTime = document.getElementById("clock-time");
  var clockDate = document.getElementById("clock-date");
  var timeFormat = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
  var dateFormat = { weekday: "short", day: "numeric", month: "short" };

  function tick() {
    var now = new Date();
    if (clockTime) { clockTime.textContent = now.toLocaleTimeString("ru-RU", timeFormat); }
    if (clockDate) { clockDate.textContent = now.toLocaleDateString("ru-RU", dateFormat); }
  }
  tick();
  setInterval(tick, 1000);

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
