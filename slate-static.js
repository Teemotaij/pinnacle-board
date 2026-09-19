/* The slate's filters and sorting, for the copy that has no server behind it.
 *
 * Only ever loaded by the exported page (see the `static` flag in slate.html). The live site does all
 * of this in Python, and the one rule this file has to obey is that it must not become a SECOND
 * definition of what the filters mean: every value it tests is a data- attribute the server wrote out,
 * derived from the same expression the server itself filters on. So this decides what to hide, never
 * what a row is.
 *
 * Day is missing from the list deliberately. A different day is a different set of rows, so it is a
 * different file, and the menu navigates rather than filters.
 */
(function () {
  "use strict";

  var table = document.querySelector("table.slate");
  if (!table) return;                       // a day with nothing scored renders no table at all
  var body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var form = document.getElementById("filters");
  if (!form) return;

  function val(name) {
    var el = form.elements[name];
    return el ? el.value : "";
  }

  // Each filter as a test against one attribute the server wrote. An empty control means "any", which
  // is why every test short-circuits on a falsy value rather than comparing against "".
  function visible(tr) {
    var d = tr.dataset;
    if (val("universe") === "1" && d.universe !== "1") return false;
    if (val("circuit") && d.circuit !== val("circuit")) return false;
    if (val("tour") && d.tour !== val("tour")) return false;
    if (val("src") && d.src !== val("src")) return false;
    if (val("move") && d.move !== val("move")) return false;

    // Same rule as name_search() on the server: every word must appear somewhere in the row, in any
    // order, as a substring rather than a whole word. Half a surname is what you actually remember.
    var q = (val("q") || "").trim().toLowerCase();
    if (!q) return true;
    return q.split(/\s+/).every(function (t) { return d.find.indexOf(t) !== -1; });
  }

  function apply() {
    var shown = 0, picks = 0;
    rows.forEach(function (tr) {
      var ok = visible(tr);
      tr.hidden = !ok;
      if (ok) {
        shown++;
        if (tr.classList.contains("bet")) picks++;
      }
    });
    var a = document.getElementById("n-sides"), b = document.getElementById("n-picks");
    if (a) a.textContent = shown;
    if (b) b.textContent = picks;
  }

  form.addEventListener("change", apply);
  form.addEventListener("input", apply);
  // Nothing to submit to. Without this, Enter in the search box reloads the page and loses the filters.
  form.addEventListener("submit", function (e) { e.preventDefault(); });

  // ---- sorting -------------------------------------------------------------------------------
  //
  // The value sorted on is read from the cell, not from a second copy of the number: the cell is what
  // the server rendered, so they cannot disagree. Blanks are the em dash and come out as NaN, which is
  // sorted to the bottom whichever way the column is pointing -- a missing price is not a small one.
  //
  // Time is the exception and reads the row's stored UTC value, because the cell shows a local clock
  // and a day boundary would otherwise sort 23:40 after 00:10.
  function key(tr, i, col) {
    if (col === "match_date") return tr.dataset.when || "";
    var td = tr.cells[i];
    if (!td) return "";
    var t = td.textContent.replace(/[\s,*†]/g, "");
    var n = parseFloat(t.replace(/[+\u2212]/g, function (c) { return c === "+" ? "" : "-"; }));
    return isNaN(n) ? td.textContent.trim().toLowerCase() : n;
  }

  var current = null, descending = true;

  Array.prototype.forEach.call(table.tHead.querySelectorAll("th[data-col]"), function (th) {
    th.addEventListener("click", function (e) {
      e.preventDefault();
      var col = th.dataset.col;
      var i = th.cellIndex;
      // Second click on the same column reverses it; a new column starts descending, which is what
      // the server's `desc=1` default does for every column the slate offers.
      descending = col === current ? !descending : true;
      current = col;

      rows.sort(function (x, y) {
        var a = key(x, i, col), b = key(y, i, col);
        var an = typeof a === "number", bn = typeof b === "number";
        if (an && isNaN(a)) return 1;                 // blanks last, both directions
        if (bn && isNaN(b)) return -1;
        if (a === b) return 0;
        return (a < b ? -1 : 1) * (descending ? -1 : 1);
      });
      rows.forEach(function (tr) { body.appendChild(tr); });

      table.tHead.querySelectorAll("th").forEach(function (o) { o.classList.remove("sorted"); });
      table.tHead.querySelectorAll(".arrow").forEach(function (o) { o.textContent = ""; });
      th.classList.add("sorted");
      var arrow = th.querySelector(".arrow");
      if (arrow) arrow.textContent = descending ? "\u2193" : "\u2191";
    });
  });

  apply();
})();
