(function () {
  "use strict";
  function renumberLines() {
    document.querySelectorAll("#lineitems .lineitem").forEach(function (fs, i) {
      fs.dataset.index = i;
      fs.querySelector("legend").textContent = "Line " + i;
      fs.querySelectorAll("[name]").forEach(function (el) {
        el.name = el.name.replace(/^line_items\[\d+\]/, "line_items[" + i + "]");
      });
      var scope = "line_items[" + i + "]";
      fs.querySelector(".deductions").dataset.scope = scope;
      var add = fs.querySelector(".adddeduction");
      if (add) add.dataset.scope = scope;
    });
  }
  function renumberDeductions(container) {
    container.querySelectorAll(".dedrow").forEach(function (row, j) {
      row.querySelectorAll("[name]").forEach(function (el) {
        el.name = el.name.replace(/deductions\[\d+\]/, "deductions[" + j + "]");
      });
    });
  }
  document.addEventListener("click", function (ev) {
    var opener = ev.target.closest && ev.target.closest("[data-open-dialog]");
    if (opener) {
      var dlg = document.getElementById(opener.dataset.openDialog);
      if (dlg && dlg.showModal) dlg.showModal();
      return;
    }
    var t = ev.target;
    if (t.id === "addline") {
      var tmpl = document.querySelector("#lineitems .lineitem");
      if (!tmpl) return;
      var clone = tmpl.cloneNode(true);
      clone.querySelectorAll("input").forEach(function (i) { i.value = ""; });
      clone.querySelectorAll(".dedrow").forEach(function (r) { r.remove(); });
      document.getElementById("lineitems").appendChild(clone);
      renumberLines();
    } else if (t.classList.contains("removeline")) {
      t.closest(".lineitem").remove();
      renumberLines();
    } else if (t.classList.contains("adddeduction")) {
      var scope = t.dataset.scope;
      var box = document.querySelector('.deductions[data-scope="' + scope + '"]');
      var proto = document.querySelector(".dedrow");
      if (!box || !proto) return;
      var d = proto.cloneNode(true);
      d.querySelectorAll("input").forEach(function (i) { i.value = ""; });
      d.querySelectorAll("[name]").forEach(function (el) {
        el.name = el.name.replace(/^[^.]+(\[\d+\])?\.deductions/, scope + ".deductions");
      });
      box.appendChild(d);
      renumberDeductions(box);
    } else if (t.classList.contains("removededuction")) {
      var parent = t.closest(".deductions");
      t.closest(".dedrow").remove();
      renumberDeductions(parent);
    }
  });

  // Live-updating list pages: any container with data-poll-url refetches
  // its own fragment on an interval and swaps it in, so Journey/Queue/
  // Approved reflect pipeline progress without a manual reload. Paused
  // while the tab isn't visible so a forgotten background tab is quiet.
  document.querySelectorAll("[data-poll-url]").forEach(function (container) {
    var url = container.dataset.pollUrl;
    var interval = parseInt(container.dataset.pollInterval, 10) || 3500;
    setInterval(function () {
      if (document.hidden) return;
      fetch(url, { headers: { "X-Requested-With": "fetch" } })
        .then(function (resp) { return resp.ok ? resp.text() : null; })
        .then(function (html) {
          if (html !== null) container.innerHTML = html;
        })
        .catch(function () {
          // a transient fetch failure just means this tick's update is
          // skipped — the next interval tries again.
        });
    }, interval);
  });
})();
