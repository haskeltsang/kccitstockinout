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

  // ---- Dashboard: collapse “Units on the shelf” (+/−) --------------------
  (function () {
    var section = document.getElementById("shelf-section");
    var btn = document.getElementById("shelf-collapse-btn");
    if (!section || !btn) return;
    var key = section.getAttribute("data-collapse-key") || "dash-shelf";
    var apply = function (collapsed) {
      section.classList.toggle("is-collapsed", collapsed);
      btn.textContent = collapsed ? "+" : "−";
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.title = collapsed ? "Show this section" : "Hide this section";
      try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
    };
    var saved = null;
    try { saved = localStorage.getItem(key); } catch (e) { saved = null; }
    apply(saved === "1");
    btn.addEventListener("click", function () {
      apply(!section.classList.contains("is-collapsed"));
    });
  })();

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

    // LAN cable has a fixed 3m variant — hide wired/wireless and submit connection=3m.
    var accType = document.getElementById("accessory-type");
    var connSelect = document.getElementById("connection");
    var connLan = document.getElementById("connection-lan");
    var connField = document.getElementById("connection-field");
    function syncAccessoryConnection() {
      if (!accType || !connSelect) return;
      var isLan = accType.value === "lan_cable";
      connSelect.disabled = isLan;
      connSelect.hidden = isLan;
      if (connLan) connLan.disabled = !isLan;
      var label = connField ? connField.querySelector("label") : null;
      if (label) {
        label.innerHTML = isLan
          ? 'Length <span class="req">*</span>'
          : 'Connection <span class="req">*</span>';
      }
      var cue = document.getElementById("lan-length-cue");
      if (isLan) {
        if (!cue && connField) {
          cue = document.createElement("p");
          cue.id = "lan-length-cue";
          cue.className = "hint";
          cue.textContent = "Fixed standard: 3 Metres (SKU LAN-3M).";
          connField.appendChild(cue);
        }
        if (cue) cue.hidden = false;
      } else if (cue) {
        cue.hidden = true;
      }
    }
    if (accType) {
      accType.addEventListener("change", syncAccessoryConnection);
      syncAccessoryConnection();
    }
  }

  // ---- Nav quick-find: keep focus ready for a barcode scanner ------------
  var navFind = document.getElementById("nav-find-code");
  if (navFind) {
    navFind.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      var code = navFind.value.trim();
      if (!code) {
        event.preventDefault();
      }
    });
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

    var highlighted = unitsTable.querySelector("tr.unit-highlight");
    if (highlighted) {
      // Keep the found unit visible even if filters would hide it.
      highlighted.style.display = "";
      window.setTimeout(function () {
        highlighted.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 80);
    }
  }

  // ---- Item detail: category + model edit (Other category) ----------------
  var itemCategory = document.getElementById("item-category");
  var itemCategoryOther = document.getElementById("item-category-other");
  var itemCategoryOtherWrap = document.getElementById("item-category-other-wrap");
  if (itemCategory && itemCategoryOtherWrap) {
    var syncItemCategoryOther = function () {
      var isOther = itemCategory.value === "__other__";
      itemCategoryOtherWrap.hidden = !isOther;
      if (itemCategoryOther) {
        itemCategoryOther.disabled = !isOther;
        if (!isOther) itemCategoryOther.value = "";
      }
    };
    itemCategory.addEventListener("change", syncItemCategoryOther);
    syncItemCategoryOther();
  }

  // ---- Package form: dynamic checklist lines ------------------------------
  var pkgForm = document.getElementById("package-form");
  if (pkgForm) {
    var pkgBody = document.getElementById("package-lines-body");
    var pkgTpl = document.getElementById("pkg-line-template");
    var addLineBtn = document.getElementById("add-line-btn");
    var matchLabelFor = function (row) {
      var typeSel = row.querySelector(".line-match-type");
      if (!typeSel) return "";
      var type = typeSel.value;
      var sel = row.querySelector(".line-match-" + type);
      if (!sel || sel.selectedIndex < 0) return "";
      var opt = sel.options[sel.selectedIndex];
      if (!opt) return "";
      var custom = (opt.getAttribute("data-label") || "").trim();
      if (custom) return custom.slice(0, 80);
      return (opt.textContent || "").trim().slice(0, 80);
    };
    var syncLineLabel = function (row, force) {
      var input = row.querySelector('input[name="line_label"]');
      if (!input) return;
      var next = matchLabelFor(row);
      var prevAuto = input.getAttribute("data-auto-label");
      var cur = input.value;
      // Fill when empty, forced (new line / match change), or still on last auto value.
      if (force || !cur.trim() || (prevAuto !== null && cur === prevAuto)) {
        input.value = next;
        input.setAttribute("data-auto-label", next);
      }
    };
    var syncLineMatch = function (row) {
      var type = row.querySelector(".line-match-type").value;
      ["category", "sku", "item"].forEach(function (kind) {
        var sel = row.querySelector(".line-match-" + kind);
        if (!sel) return;
        var on = type === kind;
        sel.hidden = !on;
        sel.disabled = !on;
      });
    };
    var wireLine = function (row, isNew) {
      var typeSel = row.querySelector(".line-match-type");
      var labelInput = row.querySelector('input[name="line_label"]');
      if (typeSel) {
        typeSel.addEventListener("change", function () {
          syncLineMatch(row);
          syncLineLabel(row, true);
        });
        syncLineMatch(row);
      }
      ["category", "sku", "item"].forEach(function (kind) {
        var sel = row.querySelector(".line-match-" + kind);
        if (!sel) return;
        sel.addEventListener("change", function () {
          syncLineLabel(row, true);
        });
      });
      if (labelInput) {
        if (isNew || !labelInput.value.trim()) {
          syncLineLabel(row, true);
        } else {
          // Editing: treat current label as auto if it already matches Matches.
          var auto = matchLabelFor(row);
          labelInput.setAttribute(
            "data-auto-label",
            labelInput.value === auto ? auto : "\0"
          );
        }
        labelInput.addEventListener("input", function () {
          var auto = labelInput.getAttribute("data-auto-label") || "";
          if (labelInput.value !== auto) {
            // User customized — stop overwriting until they clear the field.
            labelInput.setAttribute(
              "data-auto-label",
              labelInput.value.trim() ? "\0" : ""
            );
          }
        });
      }
      var rm = row.querySelector(".pkg-line-remove");
      if (rm) {
        rm.addEventListener("click", function () {
          if (pkgBody.querySelectorAll(".pkg-line").length <= 1) return;
          row.remove();
        });
      }
    };
    pkgBody.querySelectorAll(".pkg-line").forEach(function (row) {
      wireLine(row, false);
    });
    if (addLineBtn && pkgTpl) {
      addLineBtn.addEventListener("click", function () {
        var node = pkgTpl.content.cloneNode(true);
        var row = node.querySelector("tr");
        pkgBody.appendChild(node);
        wireLine(row, true);
      });
      if (!pkgBody.querySelector(".pkg-line")) addLineBtn.click();
    }
  }

  // ---- Scan & batch page (stock in / stock out) --------------------------
  var scanInput = document.getElementById("scan-input");
  var scanBox = document.getElementById("scan-box");
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
  var registerModeToggle = document.getElementById("register-mode-toggle");
  var regExisting = document.getElementById("reg-existing");
  var regNew = document.getElementById("reg-new");
  var newItemName = document.getElementById("new-item-name");
  var newItemNameWrap = document.getElementById("new-item-name-wrap");
  var newItemBrand = document.getElementById("new-item-brand");
  var newItemCategory = document.getElementById("new-item-category");
  var newItemCategoryOther = document.getElementById("new-item-category-other");
  var newItemCategoryOtherWrap = document.getElementById("new-item-category-other-wrap");
  var regNewHint = document.getElementById("reg-new-hint");
  var loanToggle = document.getElementById("on_loan");
  var condemnToggle = document.getElementById("condemned");
  var finishLabel = finishBtn.textContent;
  var csrfInput = finishForm.querySelector('input[name="_csrf"]');
  var registerMode = (regNew && !regNew.hidden) ? "new" : "existing";
  var ensureItemPromise = null;

  function selectedCategoryIsAccessory() {
    if (!newItemCategory) return false;
    var opt = newItemCategory.options[newItemCategory.selectedIndex];
    return !!(opt && opt.getAttribute("data-accessory") === "1");
  }

  function syncCategoryOther() {
    if (!newItemCategory || !newItemCategoryOtherWrap) return;
    var isOther = newItemCategory.value === "__other__";
    newItemCategoryOtherWrap.hidden = !isOther;
    if (!isOther && newItemCategoryOther) newItemCategoryOther.value = "";
    var isAcc = selectedCategoryIsAccessory();
    if (newItemNameWrap) newItemNameWrap.hidden = isAcc;
    if (newItemName) {
      newItemName.disabled = registerMode !== "new" || isAcc;
      if (isAcc) {
        var opt = newItemCategory.options[newItemCategory.selectedIndex];
        newItemName.value = opt ? opt.value : "";
      }
    }
    if (regNewHint) {
      regNewHint.textContent = isAcc
        ? "Keyboard/Mouse/LAN Cable maps to the shared stock SKU in the database. Use N/A + quantity to stock in. Brand is optional."
        : "Only needed for a new product. Creates it in the catalog (or reuses the same name + category), then registers scanned units under it.";
    }
    // Fixed accessories are quantity stock — nudge the code mode to N/A.
    if (isAcc && modeToggle && scanMode !== "na") {
      modeToggle.querySelectorAll(".mode-btn").forEach(function (btn) {
        var on = btn.getAttribute("data-mode") === "na";
        btn.classList.toggle("active", on);
      });
      scanMode = "na";
      syncCodeModeUi();
    }
  }

  function selectedNewCategory() {
    if (!newItemCategory) return "";
    if (newItemCategory.value === "__other__") {
      return newItemCategoryOther ? newItemCategoryOther.value.trim() : "";
    }
    return (newItemCategory.value || "").trim();
  }

  function setNewCategoryValue(category) {
    if (!newItemCategory || !category) return;
    var match = null;
    Array.prototype.forEach.call(newItemCategory.options, function (opt) {
      if (opt.value && opt.value !== "__other__" &&
          opt.value.toLowerCase() === category.toLowerCase()) {
        match = opt.value;
      }
    });
    if (match) {
      newItemCategory.value = match;
      syncCategoryOther();
      return;
    }
    // Unknown category — add it to the list so it can be re-selected later.
    var opt = document.createElement("option");
    opt.value = category;
    opt.textContent = category;
    var otherOpt = newItemCategory.querySelector('option[value="__other__"]');
    if (otherOpt) newItemCategory.insertBefore(opt, otherOpt);
    else newItemCategory.appendChild(opt);
    newItemCategory.value = category;
    syncCategoryOther();
  }

  if (newItemCategory) {
    newItemCategory.addEventListener("change", syncCategoryOther);
    syncCategoryOther();
  }
  // A loan always needs a custodian and department (ticket is auto-assigned).
  var loanInputs = ["person", "department"].map(function (id) {
    return document.getElementById(id);
  });
  var accSkuMetas = [];
  document.querySelectorAll(".acc-sku-meta").forEach(function (el) {
    accSkuMetas.push(el);
  });
  var untaggedOutSelect = document.getElementById("untagged-out-select");
  var untaggedOutQty = document.getElementById("untagged-out-qty");
  var untaggedOutAdd = document.getElementById("untagged-out-add");

  // Entries: {kind:'unit', unitId, name, code}
  //          {kind:'new', itemId, name, serial, eam}
  //          {kind:'bulk', itemId, name, sku, stock, count}
  var entries = [];
  var scanMode = "serial"; // serial | eam | both | na
  var codeModeHint = document.getElementById("code-mode-hint");
  var untaggedQtyPanel = document.getElementById("untagged-qty-panel");
  var untaggedQtyInput = document.getElementById("untagged-qty");
  var untaggedAddBtn = document.getElementById("untagged-add-btn");
  var bothCodesPanel = document.getElementById("both-codes-panel");
  var bothSerialInput = document.getElementById("both-serial");
  var bothEamInput = document.getElementById("both-eam");
  var bothAddBtn = document.getElementById("both-add-btn");
  var CODE_MODE_HINTS = {
    eam: "Scan or type each unit’s EAM / asset tag. Choose the product below first for brand-new codes.",
    serial: "New stock often has only an S/N — EAM is optional. Either one is enough.",
    both: "Enter both S/N and EAM for each new unit in the fields below, then Add.",
    na: "Untagged stock (mouse, keyboard, LAN cable, etc.) — no barcode. Set quantity below and Add.",
  };

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

  // Package checklist (stock out): tick lines as matching units / KB-MS qty
  // land in the batch. Advisory only — Finish is not blocked.
  var packageSelect = document.getElementById("package-select");
  var packageChecklist = document.getElementById("package-checklist");
  var packageProgress = document.getElementById("package-progress");
  var packagesData = [];
  var packagesEl = document.getElementById("packages-data");
  if (packagesEl) {
    try { packagesData = JSON.parse(packagesEl.textContent || "[]"); }
    catch (e) { packagesData = []; }
  }
  // Category labels like "Mouse (Wired)" map to the same SKUs as Match-by-SKU.
  var accessoryLabelSkus = {};
  var accessoryLabelEl = document.getElementById("accessory-label-skus");
  if (accessoryLabelEl) {
    try { accessoryLabelSkus = JSON.parse(accessoryLabelEl.textContent || "{}"); }
    catch (e) { accessoryLabelSkus = {}; }
  }
  var activePackage = null;

  function entryMatchesLine(entry, line) {
    if (line.match_type === "category") {
      var want = (line.match_value || "").toLowerCase();
      if ((entry.category || "").toLowerCase() === want) return true;
      if ((entry.name || "").toLowerCase() === want) return true;
      // Keyboard/Mouse/LAN category lines ≡ accessory SKU lines.
      var mappedSku = accessoryLabelSkus[want];
      if (mappedSku && (entry.sku || "").toUpperCase() === String(mappedSku).toUpperCase()) {
        return true;
      }
      return false;
    }
    if (line.match_type === "sku") {
      return (entry.sku || "").toUpperCase() === line.match_value.toUpperCase();
    }
    if (line.match_type === "item") {
      return String(entry.itemId || "") === String(line.match_value);
    }
    return false;
  }

  function countForLine(line) {
    var n = 0;
    entries.forEach(function (e) {
      if (!entryMatchesLine(e, line)) return;
      n += e.kind === "bulk" ? e.count : 1;
    });
    return n;
  }

  function lineQty(line) {
    var q = parseInt(line && line.quantity, 10);
    return q > 0 ? q : 1;
  }

  // Scale every line by how many kits the first (primary) line implies —
  // e.g. 3 × OA PC in the batch → KB/MS/LAN denominators become ×3.
  function packageKitCount() {
    if (!activePackage || !activePackage.lines.length) return 1;
    var anchor = activePackage.lines[0];
    var perKit = lineQty(anchor);
    var have = countForLine(anchor);
    if (have < 1) return 1;
    return Math.max(1, Math.ceil(have / perKit));
  }

  function updatePackageChecklist() {
    if (!packageChecklist || !packageSelect) return;
    if (!activePackage || !activePackage.lines.length) {
      packageChecklist.hidden = true;
      packageChecklist.innerHTML = "";
      if (packageProgress) {
        packageProgress.hidden = true;
        packageProgress.textContent = "";
      }
      return;
    }
    packageChecklist.hidden = false;
    packageChecklist.innerHTML = "";
    var kits = packageKitCount();
    var done = 0;
    activePackage.lines.forEach(function (line) {
      var have = countForLine(line);
      var need = lineQty(line) * kits;
      var li = el("li", "pkg-check-item");
      if (have >= need) {
        li.className += " done";
        done += 1;
      } else if (have > 0) {
        li.className += " partial";
      }
      var mark = el("span", "pkg-check-mark", have >= need ? "✓" : (have > 0 ? "…" : "○"));
      var label = el("span", "pkg-check-label", line.label);
      var count = el("span", "pkg-check-count", have + " / " + need);
      li.appendChild(mark);
      li.appendChild(label);
      li.appendChild(count);
      packageChecklist.appendChild(li);
    });
    if (packageProgress) {
      packageProgress.hidden = false;
      var kitNote = kits > 1 ? " · " + kits + " kits" : "";
      packageProgress.textContent = done + " of " + activePackage.lines.length +
        " checklist item" + (activePackage.lines.length === 1 ? "" : "s") +
        " complete" + kitNote;
    }
  }

  if (packageSelect) {
    packageSelect.addEventListener("change", function () {
      var id = packageSelect.value;
      activePackage = null;
      packagesData.forEach(function (p) {
        if (String(p.id) === id) activePackage = p;
      });
      updatePackageChecklist();
    });
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
        ? "Remove bulk rows with × (or turn Condemnation on to drop the OA PC kit lines)"
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

  // OA PC / desktop PC stock-outs bring wired KB + MS + 3m LAN cable.
  // Users can remove those kit lines (needed for Condemnation — units only).
  var OA_PC_KIT_SKUS = ["KB-WIRED", "MS-WIRED", "LAN-3M"];
  var skippedOaPcSkus = {};

  function isOaPcCategory(cat) {
    var c = (cat || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (!c) return false;
    if (/\blaptop\b|\bnotebook\b|\bmacbook\b/.test(c)) return false;
    if (c === "pc" || c === "oa pc" || c === "oapc") return true;
    if (c === "desktop" || c === "desktop pc" || c === "computer") return true;
    return /^oa[\s-]*pc\b/.test(c);
  }

  function isOaPcKitSku(sku) {
    var s = (sku || "").toUpperCase();
    return OA_PC_KIT_SKUS.indexOf(s) !== -1;
  }

  function skipOaPcSku(sku) {
    var s = (sku || "").toUpperCase();
    if (isOaPcKitSku(s)) skippedOaPcSkus[s] = true;
  }

  function clearSkipOaPcSku(sku) {
    var s = (sku || "").toUpperCase();
    if (s) delete skippedOaPcSkus[s];
  }

  function stripOaPcKitBulk() {
    for (var i = entries.length - 1; i >= 0; i--) {
      var e = entries[i];
      if (e.kind === "bulk" && (e.autoOaPc || isOaPcKitSku(e.sku))) {
        skipOaPcSku(e.sku);
        entries.splice(i, 1);
      }
    }
  }

  function countOaPcs() {
    var n = 0;
    entries.forEach(function (e) {
      if (e.kind === "unit" && isOaPcCategory(e.category)) n += 1;
    });
    return n;
  }

  function accMetaForSku(sku) {
    var found = null;
    var want = (sku || "").toUpperCase();
    accSkuMetas.forEach(function (el) {
      if ((el.getAttribute("data-sku") || "").toUpperCase() === want) found = el;
    });
    return found;
  }

  function oaPcManualExtra(entry) {
    if (!entry) return 0;
    if (typeof entry._oaPcManualExtra === "number") {
      return Math.max(0, entry._oaPcManualExtra);
    }
    var prevAuto = entry._oaPcAutoQty || 0;
    // Fully manual row (added before any OA PC) → whole count is extra.
    if (!entry.autoOaPc && !prevAuto) return Math.max(0, entry.count || 0);
    return Math.max(0, (entry.count || 0) - prevAuto);
  }

  function syncOaPcAccessories() {
    if (!isOut) return;
    // Condemnation is unit-only — never force KB/MS/LAN onto a condemn batch.
    if (condemnToggle && condemnToggle.checked) {
      stripOaPcKitBulk();
      return;
    }
    var n = countOaPcs();
    var short = [];
    OA_PC_KIT_SKUS.forEach(function (sku) {
      var meta = accMetaForSku(sku);
      if (!meta) return;
      var itemId = parseInt(meta.getAttribute("data-item-id"), 10);
      if (!itemId) {
        if (n > 0) short.push(sku);
        return;
      }
      var name = meta.getAttribute("data-name") || sku;
      var category = meta.getAttribute("data-category") || "";
      var stock = parseInt(meta.getAttribute("data-qty"), 10) || 0;

      var existing = null;
      var existingIdx = -1;
      entries.forEach(function (e, i) {
        if (e.kind === "bulk" && e.itemId === itemId) {
          existing = e;
          existingIdx = i;
        }
      });

      // User removed this kit line (× / − to 0) — leave it out until they add it again.
      if (skippedOaPcSkus[sku]) {
        if (existing && existing.autoOaPc && oaPcManualExtra(existing) === 0) {
          entries.splice(existingIdx, 1);
        }
        return;
      }

      var manual = existing ? oaPcManualExtra(existing) : 0;
      // Kit need = OA PCs; stack on top of any qty the user already added.
      var need = manual + n;
      var total = Math.min(stock, need);
      var autoQty = Math.max(0, total - manual);
      if (need > stock) {
        short.push(sku + " (need " + need + ", have " + stock + ")");
      }

      if (total === 0) {
        if (existing) entries.splice(existingIdx, 1);
        return;
      }

      if (existing) {
        existing.count = total;
        existing.stock = stock;
        existing._oaPcManualExtra = manual;
        existing._oaPcAutoQty = autoQty;
        existing.autoOaPc = autoQty > 0 && manual === 0;
        existing.sku = existing.sku || sku;
      } else {
        entries.push({
          kind: "bulk",
          itemId: itemId,
          name: name,
          sku: sku,
          category: category,
          stock: stock,
          count: total,
          autoOaPc: manual === 0,
          _oaPcAutoQty: autoQty,
          _oaPcManualExtra: manual,
        });
      }
    });
    if (n > 0 && short.length) {
      setStatus("OA PC kit needs more stock: " + short.join(", "), false);
    }
  }

  function addUntaggedOut() {
    if (!untaggedOutSelect || !isOut) return;
    var opt = untaggedOutSelect.options[untaggedOutSelect.selectedIndex];
    var itemId = parseInt(untaggedOutSelect.value, 10);
    if (!itemId || !opt || opt.disabled) {
      setStatus("Choose an untagged product first.", false);
      beep(false);
      return;
    }
    var name = opt.getAttribute("data-name") || opt.textContent;
    var sku = opt.getAttribute("data-sku") || "";
    var category = opt.getAttribute("data-category") || "";
    var stock = parseInt(opt.getAttribute("data-qty"), 10) || 0;
    var qty = untaggedOutQty ? parseInt(untaggedOutQty.value, 10) : 1;
    if (isNaN(qty) || qty < 1) qty = 1;
    if (stock < 1) {
      setStatus("No untagged × " + name + " on the shelf.", false);
      beep(false);
      return;
    }
    var existing = null;
    entries.forEach(function (e) {
      if (e.kind === "bulk" && e.itemId === itemId) existing = e;
    });
    var next = (existing ? existing.count : 0) + qty;
    var kit = isOaPcKitSku(sku);
    var prevManual = existing ? oaPcManualExtra(existing) : 0;
    if (next > stock) {
      var addCap = stock - (existing ? existing.count : 0);
      if (existing) {
        existing.count = stock;
        existing.autoOaPc = false;
        if (kit && addCap > 0) {
          existing._oaPcManualExtra = prevManual + addCap;
        }
      } else if (stock > 0) {
        entries.push({
          kind: "bulk", itemId: itemId, name: name, sku: sku,
          category: category, stock: stock, count: stock, autoOaPc: false,
          _oaPcManualExtra: kit ? stock : 0,
          _oaPcAutoQty: 0,
        });
      }
      if (kit) clearSkipOaPcSku(sku);
      setStatus("Only " + stock + " × " + name + " on the shelf.", false);
      beep(false);
    } else if (existing) {
      existing.count = next;
      existing.stock = stock;
      existing.autoOaPc = false;
      if (kit) {
        existing._oaPcManualExtra = prevManual + qty;
        clearSkipOaPcSku(sku);
      }
      setStatus("Added: " + name + " (×" + existing.count + ")", true);
      beep(true);
    } else {
      entries.push({
        kind: "bulk", itemId: itemId, name: name, sku: sku,
        category: category, stock: stock, count: qty, autoOaPc: false,
        _oaPcManualExtra: kit ? qty : 0,
        _oaPcAutoQty: 0,
      });
      if (kit) clearSkipOaPcSku(sku);
      setStatus("Added: " + name + " (×" + qty + ")", true);
      beep(true);
    }
    if (untaggedOutQty) untaggedOutQty.value = "1";
    render();
  }

  function render() {
    syncOaPcAccessories();
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
        if (entry.eam) {
          detailTd.appendChild(codeChip("EAM", entry.eam));
          if (entry.serial) detailTd.appendChild(document.createTextNode(" "));
        }
        if (entry.serial) {
          detailTd.appendChild(codeChip("S/N", entry.serial));
        } else if (!entry.eam && entry.code) {
          // Fallback for older in-memory rows that only stored one code.
          detailTd.appendChild(codeChip(
            entry.codeIsSerial ? "S/N" : "EAM", entry.code
          ));
        }
        if (entry.condemned) {
          detailTd.appendChild(document.createTextNode(" "));
          detailTd.appendChild(el("span", "tag-badge tag-condemn", "Condemnation"));
        }
      } else if (entry.kind === "new") {
        detailTd.appendChild(el("span", "tag-badge tag-new", "New"));
        detailTd.appendChild(document.createTextNode(" "));
        if (entry.category) {
          detailTd.appendChild(el("span", "muted small", entry.category));
          detailTd.appendChild(document.createTextNode(" "));
        }
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
          if (entry.count > 1) {
            entry.count -= 1;
            if (isOaPcKitSku(entry.sku)) {
              // Prefer trimming manual extras before cutting the OA kit portion.
              var manual = oaPcManualExtra(entry);
              if (manual > 0) entry._oaPcManualExtra = manual - 1;
              else {
                entry._oaPcManualExtra = 0;
                skipOaPcSku(entry.sku);
              }
              entry.autoOaPc = false;
            }
          } else {
            skipOaPcSku(entry.sku);
            entries.splice(idx, 1);
          }
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
          if (isOaPcKitSku(entry.sku)) {
            clearSkipOaPcSku(entry.sku);
            entry._oaPcManualExtra = oaPcManualExtra(entry) + 1;
            entry.autoOaPc = false;
          }
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
        if (entry.kind === "bulk") skipOaPcSku(entry.sku);
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
    updatePackageChecklist();
  }

  function addBulk(item, qty) {
    var addQty = qty == null ? 1 : qty;
    if (isNaN(addQty) || addQty < 1) addQty = 1;
    var existing = null;
    entries.forEach(function (e) {
      if (e.kind === "bulk" && e.itemId === item.id) existing = e;
    });
    var stock = typeof item.quantity === "number" ? item.quantity : (item.stock || 0);
    var sku = item.sku || "";
    var kit = isOaPcKitSku(sku);
    if (existing) {
      var next = existing.count + addQty;
      if (isOut && next > existing.stock) {
        setStatus("Only " + existing.stock + " untagged × " + existing.name + " available.", false);
        beep(false);
        return;
      }
      var prevManual = oaPcManualExtra(existing);
      existing.count = next;
      if (kit) {
        existing._oaPcManualExtra = prevManual + addQty;
        existing.autoOaPc = false;
        clearSkipOaPcSku(sku);
      }
      setStatus("Added: " + existing.name + " (×" + existing.count + ")", true);
    } else {
      if (isOut && stock < 1) {
        setStatus("No untagged × " + item.name + " on the shelf.", false);
        beep(false);
        return;
      }
      if (isOut && addQty > stock) {
        setStatus("Only " + stock + " × " + item.name + " on the shelf.", false);
        beep(false);
        addQty = stock;
      }
      entries.push({
        kind: "bulk", itemId: item.id, name: item.name, sku: sku,
        category: item.category || "", stock: stock, count: addQty,
        autoOaPc: false,
        _oaPcManualExtra: kit ? addQty : 0,
        _oaPcAutoQty: 0,
      });
      if (kit) clearSkipOaPcSku(sku);
      setStatus("Added: " + item.name + " (×" + addQty + ")", true);
    }
    beep(true);
    render();
  }

  function addUntaggedQty() {
    if (isOut) return;
    var qty = untaggedQtyInput ? parseInt(untaggedQtyInput.value, 10) : 1;
    if (isNaN(qty) || qty < 1) {
      setStatus("Enter a quantity of at least 1.", false);
      beep(false);
      return;
    }
    if (qty > 10000) {
      setStatus("Quantity cannot exceed 10000.", false);
      beep(false);
      return;
    }
    ensureRegisterItem()
      .then(function (item) {
        addBulk({
          id: item.id,
          name: item.name,
          sku: item.sku || "",
          category: item.category || "",
          quantity: typeof item.quantity === "number" ? item.quantity : 0,
        }, qty);
      })
      .catch(function (err) {
        setStatus(err.message || String(err), false);
        beep(false);
      });
  }

  function syncExistingProductOptions() {
    // N/A + Existing product: only untagged catalog rows (no S/N or EAM units).
    if (!newItemSelect) return;
    var hideTagged = !isOut && scanMode === "na" && registerMode === "existing";
    var selectedHidden = false;
    Array.prototype.forEach.call(newItemSelect.options, function (opt) {
      if (!opt.value) return; // placeholder
      var tagged = opt.getAttribute("data-tagged") === "1";
      var hide = hideTagged && tagged;
      opt.hidden = hide;
      opt.disabled = hide;
      if (hide && opt.selected) selectedHidden = true;
    });
    if (selectedHidden) {
      newItemSelect.value = "";
      setStatus("Choose an untagged product — items with S/N or EAM are hidden in N/A mode.", false);
    }
  }

  function syncCodeModeUi() {
    if (codeModeHint && CODE_MODE_HINTS[scanMode]) {
      codeModeHint.textContent = CODE_MODE_HINTS[scanMode];
    }
    if (untaggedQtyPanel) {
      untaggedQtyPanel.hidden = isOut || scanMode !== "na";
    }
    if (bothCodesPanel) {
      bothCodesPanel.hidden = isOut || scanMode !== "both";
    }
    if (scanBox) {
      // S/N + EAM uses its own fields — hide the main barcode box.
      scanBox.hidden = !isOut && scanMode === "both";
    }
    if (scanInput) {
      if (scanMode === "na") {
        scanInput.placeholder = "Optional: type an accessory SKU (MS-WIRED…) or leave blank and use Add";
      } else {
        scanInput.placeholder = "EAM no., S/N or accessory SKU";
      }
      scanInput.disabled = !isOut && scanMode === "both";
    }
    syncExistingProductOptions();
  }

  function addUnit(unit) {
    var dup = false;
    entries.forEach(function (e) {
      if (e.kind === "unit" && e.unitId === unit.unit_id) dup = true;
    });
    var eam = unit.eam_no || "";
    var serial = unit.serial_no || "";
    var code = eam || serial;
    if (dup) {
      setStatus(code + " is already in the list.", false);
      beep(false);
      return;
    }
    entries.push({
      kind: "unit",
      unitId: unit.unit_id,
      itemId: unit.id,
      name: unit.name,
      sku: unit.sku || "",
      category: unit.category || "",
      eam: eam,
      serial: serial,
      code: code,
      codeIsSerial: !eam && !!serial,
      condemned: !!unit.condemned,
    });
    var msg = unit.name + " (" +
      [eam ? "EAM " + eam : "", serial ? "S/N " + serial : ""].filter(Boolean).join(" · ") +
      ")";
    var oaKit = isOut && isOaPcCategory(unit.category || "");
    if (!isOut && unit.condemned) {
      // Warn loudly on a condemned return — the unit still gets added so the
      // operator can record it, but it must not go straight back on a shelf.
      setStatus("Warning — condemned stock returned: " + msg +
        (unit.holder ? " — was with " + unit.holder : "") +
        ". Inspect and quarantine before shelving.", false);
      beep(false);
    } else {
      setStatus(
        isOut
          ? ("Added: " + msg + (oaKit ? " — wired KB + MS + 3m LAN added for OA PC" : ""))
          : ("Return: " + msg + (unit.holder ? " — was with " + unit.holder : "") +
            (unit.on_loan ? " · on loan" : "")),
        true
      );
      beep(true);
    }
    render();
  }

  function setRegisterMode(mode) {
    registerMode = mode === "new" ? "new" : "existing";
    if (registerModeToggle) {
      registerModeToggle.querySelectorAll(".mode-btn").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-reg-mode") === registerMode);
      });
    }
    if (regExisting) regExisting.hidden = registerMode !== "existing";
    if (regNew) regNew.hidden = registerMode !== "new";
    // Name/category are only for New product — clear required feel when hidden.
    if (newItemCategory) newItemCategory.disabled = registerMode !== "new";
    if (newItemCategoryOther) newItemCategoryOther.disabled = registerMode !== "new";
    if (newItemBrand) newItemBrand.disabled = registerMode !== "new";
    if (newItemName) {
      newItemName.disabled = registerMode !== "new" || selectedCategoryIsAccessory();
    }
    if (registerMode === "new") syncCategoryOther();
    syncExistingProductOptions();
  }

  if (registerModeToggle) {
    registerModeToggle.querySelectorAll(".mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;
        setRegisterMode(btn.getAttribute("data-reg-mode"));
        scanInput.focus();
      });
    });
    setRegisterMode(registerMode);
  }

  function csrfToken() {
    return csrfInput ? csrfInput.value : "";
  }

  function addOptionToItemSelect(item) {
    if (!newItemSelect || !item || !item.id) return;
    var exists = false;
    Array.prototype.forEach.call(newItemSelect.options, function (opt) {
      if (opt.value === String(item.id)) exists = true;
    });
    if (!exists) {
      var opt = document.createElement("option");
      opt.value = String(item.id);
      opt.setAttribute("data-name", item.name || "");
      opt.setAttribute("data-category", item.category || "");
      // Newly created / accessory rows are untagged unless the API says otherwise.
      opt.setAttribute("data-tagged", item.has_tagged ? "1" : "0");
      opt.textContent = item.name +
        (item.category ? " · " + item.category : "") +
        (item.sku ? " (" + item.sku + ")" : "");
      newItemSelect.appendChild(opt);
    }
    syncExistingProductOptions();
    var optNow = newItemSelect.querySelector('option[value="' + item.id + '"]');
    if (optNow && !optNow.disabled) {
      newItemSelect.value = String(item.id);
    }
    // Enabling "Existing product" once a catalog row exists.
    if (registerModeToggle) {
      registerModeToggle.querySelectorAll('.mode-btn[data-reg-mode="existing"]').forEach(function (btn) {
        btn.disabled = false;
      });
    }
  }

  function ensureRegisterItem() {
    if (registerMode === "existing") {
      if (!newItemSelect || !newItemSelect.value) {
        return Promise.reject(new Error(
          "Choose an existing product under “Register new units as”, or switch to New product."
        ));
      }
      var opt = newItemSelect.options[newItemSelect.selectedIndex];
      return Promise.resolve({
        id: parseInt(newItemSelect.value, 10),
        name: opt.getAttribute("data-name") || opt.textContent,
        category: opt.getAttribute("data-category") || "",
        created: false,
      });
    }
    var isAcc = selectedCategoryIsAccessory();
    var category = selectedNewCategory();
    var name = newItemName ? newItemName.value.trim() : "";
    var brand = newItemBrand ? newItemBrand.value.trim() : "";
    if (!category) {
      return Promise.reject(new Error(
        newItemCategory && newItemCategory.value === "__other__"
          ? "Type the custom category before continuing."
          : "Choose an item category before continuing."
      ));
    }
    if (!isAcc && !name) {
      return Promise.reject(new Error("Enter a product name before continuing."));
    }
    if (isAcc && !name) {
      name = category; // server maps category → fixed accessory name/SKU
    }
    if (ensureItemPromise) return ensureItemPromise;
    ensureItemPromise = fetch("/api/items", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({ name: name, category: category, brand: brand }),
    })
      .then(function (resp) {
        return resp.json().then(function (body) {
          if (!resp.ok) {
            throw new Error(body.error || "Could not create the product.");
          }
          return body;
        });
      })
      .then(function (item) {
        addOptionToItemSelect(item);
        if (newItemName && !isAcc) newItemName.value = item.name || name;
        if (newItemBrand && item.brand) newItemBrand.value = item.brand;
        // Keep the wired/wireless / LAN category selected so the UI stays clear.
        if (!isAcc) setNewCategoryValue(item.category || category);
        return item;
      })
      .finally(function () {
        ensureItemPromise = null;
      });
    return ensureItemPromise;
  }

  function codesAlreadyInBatch(serial, eam) {
    var hit = false;
    entries.forEach(function (e) {
      if (e.kind !== "new" && e.kind !== "unit") return;
      if (serial && (
        (e.serial && e.serial.toLowerCase() === serial.toLowerCase()) ||
        (e.code && e.code.toLowerCase() === serial.toLowerCase())
      )) hit = true;
      if (eam && (
        (e.eam && e.eam.toLowerCase() === eam.toLowerCase()) ||
        (e.code && e.code.toLowerCase() === eam.toLowerCase())
      )) hit = true;
    });
    return hit;
  }

  function addNewUnit(serial, eam) {
    serial = (serial || "").trim();
    eam = (eam || "").trim();
    if (!serial && !eam) {
      setStatus("Enter at least an S/N or an EAM number.", false);
      beep(false);
      return;
    }
    if (scanMode === "both" && (!serial || !eam)) {
      setStatus("S/N + EAM mode needs both identifiers.", false);
      beep(false);
      return;
    }
    if (codesAlreadyInBatch(serial, eam)) {
      setStatus((serial || eam) + " is already in the list.", false);
      beep(false);
      return;
    }
    ensureRegisterItem()
      .then(function (item) {
        var entry = {
          kind: "new",
          itemId: item.id,
          name: item.name,
          category: item.category || "",
          serial: serial,
          eam: eam,
        };
        entries.push(entry);
        var bits = [];
        if (eam) bits.push("EAM " + eam);
        if (serial) bits.push("S/N " + serial);
        setStatus(
          (item.created ? "New product · " : "") +
            "New unit: " + entry.name + " — " + bits.join(" · "),
          true
        );
        beep(true);
        if (bothSerialInput) bothSerialInput.value = "";
        if (bothEamInput) bothEamInput.value = "";
        render();
      })
      .catch(function (err) {
        setStatus(err.message || String(err), false);
        beep(false);
      });
  }

  function fillBothCodeField(code) {
    if (!bothSerialInput || !bothEamInput) return;
    if (!bothSerialInput.value.trim()) {
      bothSerialInput.value = code;
      setStatus("S/N set — now scan or enter the EAM, then Add.", true);
      beep(true);
      bothEamInput.focus();
      return;
    }
    if (!bothEamInput.value.trim()) {
      bothEamInput.value = code;
      setStatus("EAM set — click Add (or press Enter) to put the unit on the list.", true);
      beep(true);
      if (bothAddBtn) bothAddBtn.focus();
      return;
    }
    setStatus("Both fields are filled — click Add, or clear a field to rescan.", false);
    beep(false);
  }

  function addBothCodes() {
    var serial = bothSerialInput ? bothSerialInput.value.trim() : "";
    var eam = bothEamInput ? bothEamInput.value.trim() : "";
    addNewUnit(serial, eam);
  }

  function handleLookup(res, code) {
    if (!res.ok) {
      if (isOut) {
        setStatus("Unknown code: " + code + " — only registered units can be handed out.", false);
        beep(false);
      } else if (scanMode === "na") {
        setStatus("Unknown code in N/A mode — use Add with a product and quantity, or pick EAM/S/N for tagged units.", false);
        beep(false);
      } else if (scanMode === "both") {
        fillBothCodeField(code);
      } else if (scanMode === "eam") {
        addNewUnit("", code);
      } else {
        addNewUnit(code, "");
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
          addOptionToItemSelect(body);
          setRegisterMode("existing");
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
        scanMode = btn.getAttribute("data-mode") || "serial";
        syncCodeModeUi();
        if (scanMode === "na" && untaggedQtyInput) untaggedQtyInput.focus();
        else if (scanMode === "both" && bothSerialInput) bothSerialInput.focus();
        else scanInput.focus();
      });
    });
    syncCodeModeUi();
  }

  if (untaggedAddBtn) {
    untaggedAddBtn.addEventListener("click", function () {
      addUntaggedQty();
    });
  }
  if (untaggedQtyInput) {
    untaggedQtyInput.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addUntaggedQty();
    });
  }
  if (bothAddBtn) {
    bothAddBtn.addEventListener("click", function () {
      addBothCodes();
    });
  }
  [bothSerialInput, bothEamInput].forEach(function (inp) {
    if (!inp) return;
    inp.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addBothCodes();
    });
  });

  if (untaggedOutAdd) {
    untaggedOutAdd.addEventListener("click", function () {
      addUntaggedOut();
    });
  }
  if (untaggedOutQty) {
    untaggedOutQty.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addUntaggedOut();
    });
  }
  if (untaggedOutSelect) {
    untaggedOutSelect.addEventListener("change", function () {
      var opt = untaggedOutSelect.options[untaggedOutSelect.selectedIndex];
      var stock = opt ? parseInt(opt.getAttribute("data-qty"), 10) || 0 : 0;
      if (untaggedOutQty && stock > 0) {
        untaggedOutQty.max = String(stock);
        if (parseInt(untaggedOutQty.value, 10) > stock) {
          untaggedOutQty.value = String(stock);
        }
      }
    });
  }

  clearBtn.addEventListener("click", function () {
    entries = [];
    skippedOaPcSkus = {};
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
      if (condemnToggle.checked) {
        // Drop auto OA kit accessories so Condemnation can stay on (units only).
        stripOaPcKitBulk();
        loanToggle.checked = false;
        setStatus(
          "Condemnation: keyboard / mouse / LAN kit lines removed. " +
          "Only scanned units will be written off.",
          true
        );
      } else {
        // Leaving condemnation — OA PC kit auto-add can run again.
        OA_PC_KIT_SKUS.forEach(function (s) { delete skippedOaPcSkus[s]; });
      }
      syncModeToggles();
      render();
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
          " — remove bulk rows with × (OA PC kit lines stay removed).", false);
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
      setRegisterMode("existing");
      var opt = newItemSelect.options[newItemSelect.selectedIndex];
      setStatus("Product set: " + opt.getAttribute("data-name") + " — scan each unit’s S/N or EAM tag.", true);
    }
  }

  render();
  scanInput.focus();
})();
