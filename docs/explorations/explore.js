/* The switcher bar shared by every direction page: jump between A/B/C/D and
 * the index, and pick which fixture the page shows (sample, no orders,
 * nothing set up) so the empty states can be reached. */
(function (global) {
  "use strict";
  var DIRECTIONS = [
    ["index.html", "Index"],
    ["a.html", "A · Counter"],
    ["b.html", "B · Register"],
    ["c.html", "C · Guide"],
    ["d.html", "D · Tally"]
  ];
  var here = (location.pathname.split("/").pop() || "index.html").toLowerCase();

  global.explore = function (store) {
    var bar = document.createElement("nav");
    bar.className = "x-bar";
    bar.setAttribute("aria-label", "Design explorations");
    var html = '<span class="x-tag">Exploration</span>';
    DIRECTIONS.forEach(function (d) {
      html += '<a href="' + d[0] + '"' + (here === d[0] ? ' aria-current="page"' : "") + ">" + d[1] + "</a>";
    });
    html += '<span class="x-spacer"></span>';
    if (store) {
      html += '<label>Data <select id="x-fixture">';
      ST.FIXTURES.forEach(function (name) {
        html += '<option value="' + name + '">' + ST.FIXTURE_LABELS[name] + "</option>";
      });
      html += "</select></label>";
    }
    bar.innerHTML = html;
    document.body.prepend(bar);
    if (store) {
      var select = bar.querySelector("#x-fixture");
      select.value = store.fixture;
      select.addEventListener("change", function () { store.load(select.value); });
      store.subscribe(function () { if (select.value !== store.fixture) select.value = store.fixture; });
    }
  };
})(window);
