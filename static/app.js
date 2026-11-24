/* IT Stock Manager — progressive enhancement (server validates everything) */
(function () {
  "use strict";

  // ---- Flash messages: close button + auto-dismiss -----------------------
  document.querySelectorAll(".flash-close").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var flash = btn.closest(".flash");
      if (flash) flash.remove();
    });
  });
  window.setTimeout(function () {
    document.querySelectorAll(".flash").forEach(function (el) {
      el.style.transition = "opacity .4s ease";
      el.style.opacity = "0";
      window.setTimeout(function () { el.remove(); }, 450);
    });
  }, 6000);

  // ---- Confirmation dialogs on dangerous forms (delete) ------------------
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // ---- Import page: accessory quantity vs Excel/CSV panels ---------------
  var importModeInputs = document.querySelectorAll("input[name='import_mode']");
  if (importModeInputs.length) {
    var accPanel = document.getElementById("accessory-panel");
    var excelPanel = document.getElementById("excel-panel");
    var syncImportPanels = function () {
      var checked = document.querySelector("input[name='import_mode']:checked");
      var isExcel = checked && checked.value === "excel";
      if (accPanel) accPanel.style.display = isExcel ? "none" : "";
      if (excelPanel) excelPanel.style.display = isExcel ? "" : "none";
    };
    importModeInputs.forEach(function (inp) {
      inp.addEventListener("change", syncImportPanels);
    });
    syncImportPanels();
  }

  // ---- Item page: filter the tracked units table --------------------------
  var unitsTable = document.getElementById("units-table");
  var unitFilterQ = document.getElementById("unit-filter-q");
  if (unitsTable && unitFilterQ) {
    var unitStatusBtns = document.getElementById("unit-filter-status");
    var unitCondemnBtns = document.getElementById("unit-filter-condemn");
    var unitYearSelect = document.getElementById("unit-filter-year");
    var unitCount = document.getElementById("unit-filter-count");
    var unitRows = [];
    var yearSet = {};
    unitsTable.querySelectorAll("tbody tr").forEach(function (tr) {
      // Searchable text: EAM, S/N, site, holder, funding/project, stocked-in, notes
      // (skip status pill and buttons)
      var text = [0, 1, 2, 4, 5, 6, 7].map(function (i) {
        var td = tr.children[i];
        return td ? td.textContent : "";
      }).join(" ").toLowerCase();
      var year = tr.getAttribute("data-stocked-year") || "";
      if (year) yearSet[year] = true;
      unitRows.push({
        tr: tr,
        status: tr.getAttribute("data-status") || "",
        condemned: tr.getAttribute("data-condemned") || "0",
        year: year,
        text: text,
      });
    });
    if (unitYearSelect) {
      Object.keys(yearSet).sort().reverse().forEach(function (year) {
        var opt = document.createElement("option");
        opt.value = year;
        opt.textContent = year;
        unitYearSelect.appendChild(opt);
      });
    }
    var unitStatus = "";
    var unitCondemn = "";
    var applyUnitFilter = function () {
      var q = unitFilterQ.value.trim().toLowerCase();
      var year = unitYearSelect ? unitYearSelect.value : "";
      var shown = 0;
      unitRows.forEach(function (r) {
        var ok = (!unitStatus || r.status === unitStatus) &&
                 (!unitCondemn || r.condemned === unitCondemn) &&
                 (!year || r.year === year) &&
                 (!q || r.text.indexOf(q) !== -1);
        r.tr.style.display = ok ? "" : "none";
        if (ok) shown += 1;
      });
      if (unitCount) {
        unitCount.textContent = shown + " of " + unitRows.length + " unit" +
          (unitRows.length === 1 ? "" : "s");
      }
    };
    unitFilterQ.addEventListener("input", applyUnitFilter);
    if (unitYearSelect) unitYearSelect.addEventListener("change", applyUnitFilter);
    var wireToggle = function (box, attr, setter) {
      if (!box) return;
      box.querySelectorAll(".mode-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
          box.querySelectorAll(".mode-btn").forEach(function (b) {
            b.classList.remove("active");
          });
          btn.classList.add("active");
          setter(btn.getAttribute(attr) || "");
          applyUnitFilter();
        });
      });
    };
    wireToggle(unitStatusBtns, "data-status", function (v) { unitStatus = v; });
    wireToggle(unitCondemnBtns, "data-condemn", function (v) { unitCondemn = v; });
    applyUnitFilter();
  }

  // ---- Scan & batch page (stock in / stock out) --------------------------
  var scanInput = document.getElementById("scan-input");
  var finishForm = document.getElementById("finish-form");
  if (!scanInput || !finishForm) return;

  var isOut = finishForm.getAttribute("data-tx-type") === "OUT";
  var tbody = document.getElementById("batch-body");
  var hiddenBox = document.getElementById("hidden-inputs");
  var statusEl = document.getElementById("scan-status");
  var summaryEl = document.getElementById("batch-summary");
  var emptyEl = document.getElementById("batch-empty");
  var table = document.getElementById("batch-table");
  var finishBtn = document.getElementById("finish-btn");
  var clearBtn = document.getElementById("clear-btn");
  var newItemSelect = document.getElementById("new-item-select");
  var modeToggle = document.getElementById("mode-toggle");
  var loanToggle = document.getElementById("on_loan");
  var condemnToggle = document.getElementById("condemned");
  var finishLabel = finishBtn.textContent;
  // A loan always needs a custodian, department and ticket number.
  var loanInputs = ["person", "department", "reference"].map(function (id) {
    return document.getElementById(id);
  });
  var accQtyInputs = [];
  document.querySelectorAll(".acc-qty").forEach(function (inp) {
    accQtyInputs.push(inp);
  });

  // Entries: {kind:'unit', unitId, name, code}
  //          {kind:'new', itemId, name, serial, eam}
  //          {kind:'bulk', itemId, name, sku, stock, count}
  var entries = [];
  var scanMode = "eam"; // unknown scanned codes land in this field for new units

  function beep(ok) {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = beep.ctx || (beep.ctx = new Ctx());
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";
      osc.frequency.value = ok ? 880 : 220;
      gain.gain.value = 0.05;
      osc.start();
      osc.stop(ctx.currentTime + (ok ? 0.07 : 0.18));
    } catch (e) { /* audio unavailable — ignore */ }
  }

  function setStatus(msg, ok) {
    statusEl.textContent = msg;
    statusEl.className = "scan-status" + (msg ? (ok ? " ok" : " err") : "");
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function hidden(name, value) {
    var input = el("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    hiddenBox.appendChild(input);
  }

  function codeChip(label, code) {
    var chip = el("span", "code-chip");
    chip.appendChild(el("span", "code-chip-label", label));
    chip.appendChild(document.createTextNode(code));
    return chip;
  }

  // On-loan and condemnation are mutually exclusive batch modes, and any
  // keyboard/mouse (bulk) row in the batch rules condemnation out — the
  // switch greys out until those rows are removed. Called from render() and
  // from both toggles' change events.
  function syncModeToggles() {
    if (!loanToggle || !condemnToggle) return;
    var hasBulk = false;
    entries.forEach(function (e) { if (e.kind === "bulk") hasBulk = true; });
    if (hasBulk) condemnToggle.checked = false;
    if (condemnToggle.checked) loanToggle.checked = false;
    loanToggle.disabled = condemnToggle.checked;
    condemnToggle.disabled = hasBulk || loanToggle.checked;
    // Keep the tooltip honest about why the switch is greyed out.
    var condemnLabel = condemnToggle.closest("label");
    if (condemnLabel) {
      condemnLabel.title = hasBulk
        ? "Remove keyboards and mice from this batch to enable condemnation"
        : loanToggle.checked
          ? "Turn off On loan first — a batch can't be both on loan and condemned"
          : "Write this batch off as condemned — later returns of these units will raise a warning";
    }
    finishForm.classList.toggle("loan-on", loanToggle.checked);
    loanInputs.forEach(function (inp) {
      if (inp) inp.required = loanToggle.checked;
    });
    if (loanToggle.checked) {
      finishBtn.textContent = "Finish — record on-loan stock out";
    } else if (condemnToggle.checked) {
      finishBtn.textContent = "Finish — record condemned stock out";
    } else {
      finishBtn.textContent = finishLabel;
    }
  }

  function render() {
    tbody.innerHTML = "";
    hiddenBox.innerHTML = "";
    var totalUnits = 0;

    entries.forEach(function (entry, idx) {
      var tr = document.createElement("tr");

      var nameTd = el("td");
      nameTd.appendChild(el("span", "item-name", entry.name));
      tr.appendChild(nameTd);

      var detailTd = el("td");
      if (entry.kind === "unit") {
        detailTd.appendChild(el("span", "tag-badge " + (isOut ? "tag-out" : "tag-return"),
          isOut ? "Out" : "Return"));
        detailTd.appendChild(document.createTextNode(" "));
        detailTd.appendChild(codeChip(entry.code.indexOf("SN-") === 0 || entry.codeIsSerial ? "S/N" : "EAM", entry.code));
        if (entry.condemned) {
          detailTd.appendChild(document.createTextNode(" "));
          detailTd.appendChild(el("span", "tag-badge tag-condemn", "Condemnation"));
        }
      } else if (entry.kind === "new") {
        detailTd.appendChild(el("span", "tag-badge tag-new", "New"));
        detailTd.appendChild(document.createTextNode(" "));
        if (entry.eam) {
          detailTd.appendChild(codeChip("EAM", entry.eam));
          detailTd.appendChild(document.createTextNode(" "));
        }
        if (entry.serial) detailTd.appendChild(codeChip("S/N", entry.serial));
      } else {
        detailTd.appendChild(el("span", "tag-badge tag-bulk", "Bulk"));
        if (entry.sku) {
          detailTd.appendChild(document.createTextNode(" "));
          detailTd.appendChild(codeChip("SKU", entry.sku));
        }
      }
      tr.appendChild(detailTd);

      var countTd = el("td", "num");
      if (entry.kind === "bulk") {
        totalUnits += entry.count;
        var ctl = el("div", "count-ctl");
        var minus = el("button", "count-btn", "−");
        minus.type = "button";
        minus.addEventListener("click", function () {
          if (entry.count > 1) entry.count -= 1;
          else entries.splice(idx, 1);
          render();
          scanInput.focus();
        });
        var plus = el("button", "count-btn", "+");
        plus.type = "button";
        plus.addEventListener("click", function () {
          if (isOut && entry.count >= entry.stock) {
            setStatus("Only " + entry.stock + " untagged × " + entry.name + " available.", false);
            beep(false);
            return;
          }
          entry.count += 1;
          render();
          scanInput.focus();
        });
        var val = el("span", "count-val" + (isOut && entry.count >= entry.stock ? " batch-over" : ""),
          String(entry.count));
        ctl.appendChild(minus);
        ctl.appendChild(val);
        ctl.appendChild(plus);
        countTd.appendChild(ctl);
      } else {
        totalUnits += 1;
        countTd.appendChild(el("span", "count-val", "1"));
      }
      tr.appendChild(countTd);

      var rmTd = el("td", "actions-col");
      var rm = el("button", "row-remove", "×");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove row");
      rm.addEventListener("click", function () {
        entries.splice(idx, 1);
        render();
        scanInput.focus();
      });
      rmTd.appendChild(rm);
      tr.appendChild(rmTd);
      tbody.appendChild(tr);

      // Wire format for the server
      hidden("rtype", entry.kind);
      if (entry.kind === "unit") {
        hidden("rref", String(entry.unitId));
        hidden("rqty", "1");
        hidden("rserial", "");
        hidden("ream", "");
      } else if (entry.kind === "new") {
        hidden("rref", String(entry.itemId));
        hidden("rqty", "1");
        hidden("rserial", entry.serial || "");
        hidden("ream", entry.eam || "");
      } else {
        hidden("rref", String(entry.itemId));
        hidden("rqty", String(entry.count));
        hidden("rserial", "");
        hidden("ream", "");
      }
    });

    accQtyInputs.forEach(function (inp) {
      var itemId = inp.getAttribute("data-item-id");
      var match = null;
      entries.forEach(function (e) {
        if (e.kind === "bulk" && String(e.itemId) === itemId) match = e;
      });
      inp.value = match ? String(match.count) : "";
    });

    emptyEl.style.display = entries.length ? "none" : "";
    table.style.display = entries.length ? "" : "none";
    summaryEl.textContent = entries.length + " row" + (entries.length === 1 ? "" : "s") +
      " · " + totalUnits + " unit" + (totalUnits === 1 ? "" : "s");
    finishBtn.disabled = entries.length === 0;

    // Re-grey the condemnation switch whenever the batch contents change.
    if (isOut && loanToggle && condemnToggle) {
      var condemnWasOn = condemnToggle.checked;
      syncModeToggles();
      if (condemnWasOn && !condemnToggle.checked) {
        setStatus("Condemnation switched off — keyboards and mice can't be condemned.", false);
      }
    }
  }

  function addBulk(item) {
    var existing = null;
    entries.forEach(function (e) {
      if (e.kind === "bulk" && e.itemId === item.id) existing = e;
    });
    if (existing) {
      if (isOut && existing.count >= existing.stock) {
        setStatus("Only " + existing.stock + " untagged × " + existing.name + " available.", false);
        beep(false);
        return;
      }
      existing.count += 1;
      setStatus("Added another: " + existing.name + " (×" + existing.count + ")", true);
    } else {
      if (isOut && item.quantity < 1) {
        setStatus("No untagged × " + item.name + " on the shelf.", false);
        beep(false);
        return;
      }
      entries.push({ kind: "bulk", itemId: item.id, name: item.name, sku: item.sku || "", stock: item.quantity, count: 1 });
      setStatus("Added: " + item.name, true);
    }
    beep(true);
    render();
  }

  function addUnit(unit) {
    var dup = false;
    entries.forEach(function (e) {
      if (e.kind === "unit" && e.unitId === unit.unit_id) dup = true;
    });
    var code = unit.eam_no || unit.serial_no;
    if (dup) {
      setStatus(code + " is already in the list.", false);
      beep(false);
      return;
    }
    entries.push({
      kind: "unit",
      unitId: unit.unit_id,
      name: unit.name,
      code: code,
      codeIsSerial: !unit.eam_no && !!unit.serial_no,
      condemned: !!unit.condemned,
    });
    var msg = unit.name + " (" + code + ")";
    if (!isOut && unit.condemned) {
      // Warn loudly on a condemned return — the unit still gets added so the
      // operator can record it, but it must not go straight back on a shelf.
      setStatus("Warning — condemned stock returned: " + msg +
        (unit.holder ? " — was with " + unit.holder : "") +
        ". Inspect and quarantine before shelving.", false);
      beep(false);
    } else {
      setStatus(isOut ? "Added: " + msg
        : "Return: " + msg + (unit.holder ? " — was with " + unit.holder : "") +
          (unit.on_loan ? " · on loan" : ""), true);
      beep(true);
    }
    render();
  }

  function addNewUnit(code) {
    if (!newItemSelect || !newItemSelect.value) {
      setStatus("Unknown code: " + code + " — choose the product under “Register new units as” first.", false);
      beep(false);
      return;
    }
    var dup = false;
    entries.forEach(function (e) {
      if (e.kind === "new" && ((e.eam && e.eam.toLowerCase() === code.toLowerCase()) ||
          (e.serial && e.serial.toLowerCase() === code.toLowerCase()))) dup = true;
    });
    if (dup) {
      setStatus(code + " is already in the list.", false);
      beep(false);
      return;
    }
    var opt = newItemSelect.options[newItemSelect.selectedIndex];
    var entry = {
      kind: "new",
      itemId: parseInt(newItemSelect.value, 10),
      name: opt.getAttribute("data-name"),
      serial: scanMode === "serial" ? code : "",
      eam: scanMode === "eam" ? code : "",
    };
    entries.push(entry);
    setStatus("New unit: " + entry.name + " — " + (scanMode === "eam" ? "EAM " : "S/N ") + code, true);
    beep(true);
    render();
  }

  function handleLookup(res, code) {
    if (!res.ok) {
      if (isOut) {
        setStatus("Unknown code: " + code + " — only registered units can be handed out.", false);
        beep(false);
      } else {
        addNewUnit(code);
      }
      return;
    }
    var body = res.body;
    if (body.type === "unit") {
      if (isOut && body.status !== "in_stock") {
        setStatus(body.name + " (" + code + ") is already checked out" +
          (body.holder ? " to " + body.holder : "") + ".", false);
        beep(false);
        return;
      }
      if (!isOut && body.status === "in_stock") {
        setStatus(body.name + " (" + code + ") is already on the shelf.", false);
        beep(false);
        return;
      }
      addUnit(body);
    } else {
      // SKU match: on IN, prefer it as the product for subsequent unit scans
      // when the item has tracked units; otherwise treat as bulk stock.
      if (!isOut && body.units_in > 0) {
        if (newItemSelect) {
          newItemSelect.value = String(body.id);
          setStatus("Product set: " + body.name + " — now scan each unit’s S/N or EAM tag.", true);
          beep(true);
        }
      } else {
        addBulk(body);
      }
    }
  }

  function lookupAndAdd(code) {
    fetch("/api/lookup?code=" + encodeURIComponent(code), {
      headers: { Accept: "application/json" },
    })
      .then(function (resp) {
        return resp.json().then(function (body) { return { ok: resp.ok, body: body }; });
      })
      .then(function (res) { handleLookup(res, code); })
      .catch(function () {
        setStatus("Lookup failed — check the connection to the server.", false);
        beep(false);
      });
  }

  scanInput.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    var code = scanInput.value.trim();
    scanInput.value = "";
    if (!code) return;
    lookupAndAdd(code);
  });

  if (modeToggle) {
    modeToggle.querySelectorAll(".mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        modeToggle.querySelectorAll(".mode-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        scanMode = btn.getAttribute("data-mode");
        scanInput.focus();
      });
    });
  }

  accQtyInputs.forEach(function (inp) {
    inp.addEventListener("input", function () {
      var itemId = parseInt(inp.getAttribute("data-item-id"), 10);
      if (!itemId) return;
      var name = inp.getAttribute("data-name");
      var sku = inp.getAttribute("data-sku");
      var stock = parseInt(inp.getAttribute("data-qty"), 10) || 0;
      var qty = parseInt(inp.value, 10);
      if (isNaN(qty) || qty < 0) qty = 0;
      var existing = null;
      entries.forEach(function (e) {
        if (e.kind === "bulk" && e.itemId === itemId) existing = e;
      });
      if (qty === 0) {
        if (existing) entries.splice(entries.indexOf(existing), 1);
        setStatus("", true);
      } else if (qty > stock) {
        if (existing) existing.count = stock;
        else entries.push({ kind: "bulk", itemId: itemId, name: name, sku: sku, stock: stock, count: stock });
        setStatus("Only " + stock + " × " + name + " on the shelf.", false);
        beep(false);
      } else {
        if (existing) existing.count = qty;
        else entries.push({ kind: "bulk", itemId: itemId, name: name, sku: sku, stock: stock, count: qty });
        setStatus("Added: " + name + " (×" + qty + ")", true);
      }
      render();
    });
  });

  clearBtn.addEventListener("click", function () {
    entries = [];
    render();
    setStatus("", true);
    scanInput.focus();
  });

  // On-loan and condemnation toggles (stock out): under either flag the
  // whole batch must be scanned units only — untagged keyboards/mice can't
  // be tracked. The two modes exclude each other and the server enforces
  // the same rules.
  if (isOut && loanToggle && condemnToggle) {
    loanToggle.addEventListener("change", function () {
      syncModeToggles();
      scanInput.focus();
    });
    condemnToggle.addEventListener("change", function () {
      syncModeToggles();
      scanInput.focus();
    });
    finishForm.addEventListener("submit", function (event) {
      if (!loanToggle.checked && !condemnToggle.checked) return;
      var hasUnit = false;
      var hasBulk = false;
      entries.forEach(function (e) {
        if (e.kind === "unit") hasUnit = true;
        else hasBulk = true;
      });
      if (hasBulk) {
        event.preventDefault();
        setStatus("Only scanned units can " +
          (condemnToggle.checked ? "be condemned" : "go on loan") +
          " — remove the keyboard/mouse rows (set their quantities back to 0).", false);
        beep(false);
      } else if (!hasUnit) {
        event.preventDefault();
        setStatus((condemnToggle.checked ? "Condemned" : "On-loan") +
          " batches need at least one scanned unit.", false);
        beep(false);
      }
    });
  }

  // Prefill from quick-action links: /stock/in?code=XYZ or ?item_id=3
  var params = new URLSearchParams(window.location.search);
  var preCode = params.get("code");
  var preId = params.get("item_id");
  if (preCode) {
    lookupAndAdd(preCode);
  } else if (preId && !isOut && newItemSelect) {
    newItemSelect.value = String(preId);
    if (newItemSelect.value) {
      var opt = newItemSelect.options[newItemSelect.selectedIndex];
      setStatus("Product set: " + opt.getAttribute("data-name") + " — scan each unit’s S/N or EAM tag.", true);
    }
  }

  render();
  scanInput.focus();
})();
