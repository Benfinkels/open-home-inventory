(() => {
  const state = {
    tab: "ledger",
    items: [],
    proposals: [],
    receipts: [],
    stats: null,
    selectedIds: new Set(),
    activeItemId: null,
    activeItem: null,
  };

  const $ = (id) => document.getElementById(id);

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtMoney(n) {
    const num = Number(n || 0);
    return "$" + num.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function fmtMoneyExact(n) {
    const num = Number(n || 0);
    return "$" + num.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  // Theme management (Light Archival Paper default, Graphite Vault dark toggle)
  function initTheme() {
    const saved = localStorage.getItem("ohi-theme") || "light";
    applyTheme(saved);
    $("themeToggle").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      localStorage.setItem("ohi-theme", next);
      applyTheme(next);
    });
  }

  function applyTheme(theme) {
    if (theme === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
      $("themeToggle").textContent = "Paper";
    } else {
      document.documentElement.removeAttribute("data-theme");
      $("themeToggle").textContent = "Dark";
    }
  }

  async function refreshAll() {
    await Promise.all([loadStats(), loadItems(), loadProposals(), loadReceipts()]);
    renderActiveTab();
  }

  async function loadStats() {
    state.stats = await api("/api/stats");
    const s = state.stats;
    $("statRcv").textContent = fmtMoney(s.total_replacement_cost);
    $("statItemsSub").textContent = `${s.total_items} cataloged assets`;
    $("statAcv").textContent = fmtMoney(s.total_acv_usd);
    $("statVerified").textContent = `${s.verified_percentage}%`;
    $("statVerifiedSub").textContent = `${fmtMoney(s.verified_valuation_usd)} verified (${s.verified_items} items)`;
    $("statMissing").textContent = String(s.missing_evidence_count);
    $("statProposals").textContent = String(s.pending_proposals_count);

    $("tabCountLedger").textContent = String(s.total_items);
    $("tabCountEvidence").textContent = String(s.missing_evidence_count);
    $("tabCountProposals").textContent = String(s.pending_proposals_count);
    $("tabCountProposals").classList.toggle("alert", s.pending_proposals_count > 0);
    $("tabCountReceipts").textContent = String(s.receipts_count);

    populateSelect("roomFilter", Object.keys(s.rooms_breakdown || {}).sort(), "All Rooms");
    populateSelect("categoryFilter", Object.keys(s.category_breakdown || {}).sort(), "All Categories");
  }

  function populateSelect(id, options, placeholder) {
    const sel = $(id);
    const prev = sel.value;
    sel.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      options.map((opt) => `<option value="${esc(opt)}">${esc(opt)}</option>`).join("");
    if (options.includes(prev)) sel.value = prev;
  }

  async function loadItems() {
    const params = new URLSearchParams();
    const q = $("searchInput").value.trim();
    const room = $("roomFilter").value;
    const cat = $("categoryFilter").value;
    const status = $("statusFilter").value;
    if (q) params.set("query", q);
    if (room) params.set("room", room);
    if (cat) params.set("category", cat);
    if (status) params.set("review_status", status);
    if (state.tab === "evidence") params.set("missing_evidence", "true");
    state.items = await api(`/api/items?${params.toString()}`);
  }

  async function loadProposals() {
    state.proposals = await api("/api/proposals?status=pending");
  }

  async function loadReceipts() {
    state.receipts = await api("/api/receipts");
  }

  function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".nav-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $("viewLedger").style.display = (tab === "ledger" || tab === "evidence") ? "" : "none";
    $("viewProposals").style.display = tab === "proposals" ? "" : "none";
    $("viewReceipts").style.display = tab === "receipts" ? "" : "none";
    if (tab === "ledger" || tab === "evidence") {
      loadItems().then(renderLedger);
    } else if (tab === "proposals") {
      renderProposalsTab();
    } else if (tab === "receipts") {
      renderReceiptsTab();
    }
  }

  function renderActiveTab() {
    if (state.tab === "proposals") renderProposalsTab();
    else if (state.tab === "receipts") renderReceiptsTab();
    else renderLedger();
  }

  function renderLedger() {
    const tbody = $("ledgerBody");
    if (!state.items.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No assets match the current filter.</td></tr>`;
      updateBulkBar();
      return;
    }

    tbody.innerHTML = state.items.map((it) => {
      const checked = state.selectedIds.has(it.id) ? "checked" : "";
      const active = state.activeItemId === it.id ? "selected-row" : "";
      const thumb = it.crop_url
        ? `<img class="item-thumb" src="${esc(it.crop_url)}" alt="" loading="lazy" onerror="this.style.display='none'">`
        : `<div class="item-thumb-placeholder">NO IMG</div>`;

      const modelSerial = [it.model_or_identifiers, it.serial_number ? `S/N ${it.serial_number}` : ""]
        .filter(Boolean)
        .join(" · ") || "—";

      const pills = [];
      if (it.pending_proposal_count > 0) pills.push(`<span class="pill pill-escalate">Escalate</span>`);
      if (it.receipt_verified) pills.push(`<span class="pill pill-verified">Verified</span>`);
      else if ((it.estimated_replacement_value_usd || 0) >= 500) pills.push(`<span class="pill pill-warn">Needs Proof</span>`);
      else pills.push(`<span class="pill pill-muted">Visual Est.</span>`);
      if (it.badge_verified) pills.push(`<span class="pill pill-muted">Badge OCR</span>`);
      if (it.review_status === "confirmed") pills.push(`<span class="pill pill-verified">Confirmed</span>`);

      return `<tr class="${active}" data-id="${it.id}">
        <td class="col-check"><input type="checkbox" class="row-check" data-id="${it.id}" ${checked}></td>
        <td class="col-thumb">${thumb}</td>
        <td>
          <div class="item-name-cell">${esc(it.item_name)}</div>
          <div class="item-sub-cell">${esc(it.brand || "Unknown")} · ${esc(it.category || "General")}${it.photo_count > 1 ? ` · ${it.photo_count} photos` : ""}</div>
        </td>
        <td class="mono-cell">${esc(modelSerial)}</td>
        <td>${esc(it.room || "General")}${it.sub_location ? `<div class="item-sub-cell">${esc(it.sub_location)}</div>` : ""}</td>
        <td><div class="pill-stack">${pills.join("")}</div></td>
        <td class="num-col">
          <div class="rcv-val">${fmtMoney(it.estimated_replacement_value_usd)}</div>
          <div class="acv-val">ACV ${fmtMoney(it.estimated_acv_usd)}</div>
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.matches("input.row-check")) return;
        openDrawer(Number(tr.dataset.id));
      });
    });

    tbody.querySelectorAll("input.row-check").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        const id = Number(cb.dataset.id);
        if (cb.checked) state.selectedIds.add(id);
        else state.selectedIds.delete(id);
        updateBulkBar();
      });
    });

    updateBulkBar();
  }

  function updateBulkBar() {
    const n = state.selectedIds.size;
    $("bulkBar").classList.toggle("visible", n > 0);
    $("bulkCount").textContent = `${n} selected`;
    $("btnBulkMerge").disabled = n < 2;
  }

  function renderProposalsTab() {
    const host = $("proposalsList");
    if (!state.proposals.length) {
      host.innerHTML = `<div class="empty-state">No pending escalation proposals. Run <code>inventory-ai escalate-high-value</code> or import an agent bundle to queue diffs here.</div>`;
      return;
    }
    host.innerHTML = state.proposals.map((p) => `
      <div class="ledger-card">
        <div style="display:flex; gap:14px; align-items:center; flex:1; min-width:0;">
          ${p.crop_url ? `<img class="item-thumb" src="${esc(p.crop_url)}" alt="">` : `<div class="item-thumb-placeholder">IMG</div>`}
          <div style="min-width:0; flex:1;">
            <div class="item-name-cell">${esc(p.item_name)} <span class="mono-cell">(#${p.item_id} · ${esc(p.room || "General")})</span></div>
            <div class="item-sub-cell" style="margin-top:4px;">
              ${renderDiffInline("Brand", p.current_brand, p.proposed_brand)}
              ${renderDiffInline("Model", p.current_model, p.proposed_model)}
              ${renderDiffInline("Serial", p.current_serial, p.proposed_serial)}
              ${p.proposed_value_usd != null ? renderDiffInline("RCV", fmtMoney(p.current_value_usd), fmtMoney(p.proposed_value_usd)) : ""}
            </div>
            ${p.reasoning ? `<div class="diff-reason" style="margin:5px 0 0;">${esc(p.reasoning)}</div>` : ""}
          </div>
        </div>
        <div style="display:flex; gap:8px; flex-shrink:0;">
          <button class="btn btn-sm" data-open-item="${p.item_id}">Inspect</button>
          <button class="btn btn-primary btn-sm" data-accept-prop="${p.id}">Accept</button>
          <button class="btn btn-sm" data-reject-prop="${p.id}">Reject</button>
        </div>
      </div>
    `).join("");

    host.querySelectorAll("[data-open-item]").forEach((b) =>
      b.addEventListener("click", () => openDrawer(Number(b.dataset.openItem)))
    );
    host.querySelectorAll("[data-accept-prop]").forEach((b) =>
      b.addEventListener("click", () => resolveProposal(Number(b.dataset.acceptProp), true))
    );
    host.querySelectorAll("[data-reject-prop]").forEach((b) =>
      b.addEventListener("click", () => resolveProposal(Number(b.dataset.rejectProp), false))
    );
  }

  function renderDiffInline(label, oldVal, newVal) {
    if (!newVal || String(oldVal || "") === String(newVal)) return "";
    return `<span style="margin-right:12px;"><strong>${esc(label)}:</strong> <span class="diff-old">${esc(oldVal || "—")}</span><span class="diff-new">${esc(newVal)}</span></span>`;
  }

  function renderReceiptsTab() {
    const host = $("receiptsList");
    if (!state.receipts.length) {
      host.innerHTML = `<div class="empty-state">No receipts ingested yet. Run <code>inventory-ai scrape-receipts --dir ./local_receipts</code> to ingest PDFs, EMLs, or Gmail orders.</div>`;
      return;
    }
    host.innerHTML = state.receipts.map((r) => {
      const lines = (r.items || []).map((li) => `${li.name} (${fmtMoneyExact(li.price_usd)})`).join(" · ");
      const pill = r.linked_item_count > 0
        ? `<span class="pill pill-verified">Linked (${r.linked_item_count})</span>`
        : `<span class="pill pill-muted">Unmatched</span>`;
      return `<div class="ledger-card">
        <div style="min-width:0; flex:1;">
          <div class="item-name-cell">${esc(r.merchant)} <span class="mono-cell">#${esc(r.order_number)}</span></div>
          <div class="item-sub-cell">${esc(r.purchase_date || "Unknown date")} · ${esc(lines || r.notes || "No line items parsed")}</div>
        </div>
        <div style="display:flex; align-items:center; gap:12px;">
          ${pill}
          <span class="num-col rcv-val">${fmtMoneyExact(r.total_usd)}</span>
        </div>
      </div>`;
    }).join("");
  }

  // Slide-over inspection & edit drawer
  async function openDrawer(itemId) {
    state.activeItemId = itemId;
    const item = await api(`/api/items/${itemId}`);
    state.activeItem = item;
    $("itemDrawer").classList.add("open");
    $("workspace").classList.add("drawer-open");
    renderLedger();
    populateDrawer(item);
    loadReceiptCandidates(item);
  }

  function closeDrawer() {
    state.activeItemId = null;
    state.activeItem = null;
    $("itemDrawer").classList.remove("open");
    $("workspace").classList.remove("drawer-open");
    renderLedger();
  }

  function populateDrawer(item) {
    $("drawerTitle").textContent = item.item_name || "Untitled Asset";
    $("drawerMeta").textContent = `ID #${item.id} · ${item.room || "General"} · ${item.review_status || "unreviewed"}`;

    // Photo stage + filmstrip
    const photos = (item.photos && item.photos.length)
      ? item.photos
      : (item.crop_url ? [{ url: item.crop_url, photo_role: "optical_crop" }] : []);

    const mainImg = $("drawerMainPhoto");
    const strip = $("drawerFilmstrip");
    if (photos.length) {
      setStagePhoto(photos[0]);
      strip.innerHTML = photos.map((p, idx) =>
        `<img class="filmstrip-thumb ${idx === 0 ? "active" : ""}" src="${esc(p.url)}" data-idx="${idx}" alt="${esc(p.photo_role)}">`
      ).join("");
      strip.querySelectorAll(".filmstrip-thumb").forEach((img) => {
        img.addEventListener("click", () => {
          strip.querySelectorAll(".filmstrip-thumb").forEach((t) => t.classList.remove("active"));
          img.classList.add("active");
          setStagePhoto(photos[Number(img.dataset.idx)]);
        });
      });
    } else {
      mainImg.removeAttribute("src");
      $("photoRoleTag").textContent = "No Photo";
      strip.innerHTML = "";
    }

    $("badgeOcrBanner").style.display = "none";

    // Escalation diff card
    const slot = $("drawerProposalSlot");
    const pending = item.pending_proposals || [];
    if (pending.length) {
      const p = pending[0];
      slot.innerHTML = `
        <div class="diff-card">
          <div class="diff-card-title">
            <span>Escalation Proposal #${p.id}</span>
            <span class="mono-cell">${esc(p.provider)}/${esc(p.model)}</span>
          </div>
          ${renderDiffRow("Brand", item.brand, p.proposed_brand)}
          ${renderDiffRow("Model", item.model_or_identifiers, p.proposed_model)}
          ${renderDiffRow("Serial", item.serial_number, p.proposed_serial)}
          ${p.proposed_value_usd != null ? renderDiffRow("RCV", fmtMoney(item.estimated_replacement_value_usd), fmtMoney(p.proposed_value_usd)) : ""}
          ${p.reasoning ? `<div class="diff-reason">${esc(p.reasoning)}</div>` : ""}
          <div class="diff-actions">
            <button class="btn btn-primary btn-sm" id="btnDrawerAccept" type="button">Accept Proposal</button>
            <button class="btn btn-sm" id="btnDrawerReject" type="button">Reject</button>
          </div>
        </div>
      `;
      $("btnDrawerAccept").addEventListener("click", () => resolveProposal(p.id, true));
      $("btnDrawerReject").addEventListener("click", () => resolveProposal(p.id, false));
    } else {
      slot.innerHTML = "";
    }

    // Form fields
    $("fItemName").value = item.item_name || "";
    $("fBrand").value = item.brand || "";
    $("fModel").value = item.model_or_identifiers || "";
    $("fSerial").value = item.serial_number || "";
    $("fRoom").value = item.room || "General";
    $("fSubLoc").value = item.sub_location || "";
    $("fCategory").value = item.category || "General";
    $("fCondition").value = item.condition || "Good";
    $("fRcv").value = item.estimated_replacement_value_usd ?? "";
    $("fAcv").value = item.estimated_acv_usd ?? "";
    $("fQty").value = item.quantity || 1;
    $("fStatus").value = item.review_status || "unreviewed";
    $("fDesc").value = item.description || "";
  }

  function setStagePhoto(photo) {
    $("drawerMainPhoto").src = photo.url || "";
    const labels = {
      optical_crop: "1:1 Optical Crop",
      room_overlay: "Room Overlay",
      closeup_badge: "Close-Up Badge",
      receipt_scan: "Receipt Scan",
    };
    $("photoRoleTag").textContent = labels[photo.photo_role] || photo.photo_role || "Photo";
  }

  function renderDiffRow(label, oldVal, newVal) {
    if (!newVal) return "";
    return `<div class="diff-row">
      <span class="diff-key">${esc(label)}</span>
      <span class="diff-val"><span class="diff-old">${esc(oldVal || "—")}</span><span class="diff-new">${esc(newVal)}</span></span>
    </div>`;
  }

  async function loadReceiptCandidates(item) {
    const box = $("drawerReceiptBox");
    if (item.receipt_verified) {
      box.innerHTML = `
        <div class="candidate-row">
          <div class="candidate-info">
            <div class="candidate-title">${esc(item.receipt_evidence || `${item.merchant} #${item.order_number}`)}</div>
            <div class="candidate-meta">Paid ${fmtMoneyExact(item.purchase_price_usd)} · ${esc(item.purchase_date || "")} · ${esc(item.receipt_match_type || "auto")}</div>
          </div>
          <button class="btn btn-sm" id="btnUnlinkReceipt" type="button">Unlink</button>
        </div>
      `;
      $("btnUnlinkReceipt").addEventListener("click", async () => {
        await api(`/api/items/${item.id}/receipt-link`, { method: "DELETE" });
        showToast("Receipt unlinked");
        await refreshAll();
        openDrawer(item.id);
      });
      return;
    }

    const candidates = await api(`/api/items/${item.id}/receipt-candidates?top_k=3`);
    if (!candidates.length) {
      box.innerHTML = `<div class="item-sub-cell">No ingested receipts available to link yet.</div>`;
      return;
    }
    box.innerHTML = candidates.map((c) => `
      <div class="candidate-row">
        <div class="candidate-info">
          <div class="candidate-title">${esc(c.merchant)} — ${esc(c.matched_line || `#${c.order_number}`)}</div>
          <div class="candidate-meta">#${esc(c.order_number)} · ${esc(c.purchase_date)} · ${fmtMoneyExact(c.line_price_usd)} · ${Math.round(c.confidence * 100)}% match</div>
        </div>
        <button class="btn btn-primary btn-sm" data-link-rec="${c.receipt_id}" data-price="${c.line_price_usd}" data-sn="${esc(c.line_serial || "")}" type="button">Link</button>
      </div>
    `).join("");

    box.querySelectorAll("[data-link-rec]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/items/${item.id}/receipt-link`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            receipt_id: Number(btn.dataset.linkRec),
            price_usd: Number(btn.dataset.price) || null,
            serial_number: btn.dataset.sn || null,
          }),
        });
        showToast("Receipt linked");
        await refreshAll();
        openDrawer(item.id);
      });
    });
  }

  async function saveActiveItem(overrideStatus = null) {
    if (!state.activeItemId) return;
    const payload = {
      item_name: $("fItemName").value.trim(),
      brand: $("fBrand").value.trim(),
      model_or_identifiers: $("fModel").value.trim(),
      serial_number: $("fSerial").value.trim(),
      room: $("fRoom").value.trim() || "General",
      sub_location: $("fSubLoc").value.trim(),
      category: $("fCategory").value.trim() || "General",
      condition: $("fCondition").value,
      estimated_replacement_value_usd: $("fRcv").value === "" ? null : Number($("fRcv").value),
      estimated_acv_usd: $("fAcv").value === "" ? null : Number($("fAcv").value),
      quantity: Number($("fQty").value) || 1,
      review_status: overrideStatus || $("fStatus").value,
      description: $("fDesc").value.trim(),
    };
    const updated = await api(`/api/items/${state.activeItemId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.activeItem = updated;
    showToast(overrideStatus === "confirmed" ? "Asset confirmed" : "Changes saved");
    await refreshAll();
    populateDrawer(updated);
  }

  async function resolveProposal(propId, accept) {
    const endpoint = accept ? `/api/proposals/${propId}/accept` : `/api/proposals/${propId}/reject`;
    await api(endpoint, { method: "POST" });
    showToast(accept ? `Applied proposal #${propId}` : `Rejected proposal #${propId}`);
    await refreshAll();
    if (state.activeItemId) openDrawer(state.activeItemId);
  }

  async function uploadBadgeFile(file) {
    if (!state.activeItemId || !file) return;
    const fd = new FormData();
    fd.append("file", file);
    showToast("Uploading close-up badge photo...");
    const res = await api(`/api/items/${state.activeItemId}/badge-photo`, { method: "POST", body: fd });
    if (res.item) populateDrawer(res.item);
    const banner = $("badgeOcrBanner");
    if (res.suggested_fields && (res.suggested_fields.brand || res.suggested_fields.model || res.suggested_fields.serial_number)) {
      const s = res.suggested_fields;
      banner.style.display = "";
      banner.innerHTML = `
        <div class="diff-card">
          <div class="diff-card-title"><span>Badge OCR Suggestions</span></div>
          ${renderDiffRow("Brand", state.activeItem?.brand, s.brand)}
          ${renderDiffRow("Model", state.activeItem?.model_or_identifiers, s.model)}
          ${renderDiffRow("Serial", state.activeItem?.serial_number, s.serial_number)}
          <div class="diff-actions" style="margin-top:8px;">
            <button class="btn btn-primary btn-sm" id="btnApplyOcr" type="button">Apply to Form</button>
          </div>
        </div>
      `;
      $("btnApplyOcr").addEventListener("click", () => {
        if (s.brand) $("fBrand").value = s.brand;
        if (s.model) $("fModel").value = s.model;
        if (s.serial_number) $("fSerial").value = s.serial_number;
        banner.style.display = "none";
        showToast("Applied OCR fields — click Save Changes");
      });
    } else {
      showToast(res.ocr_error ? "Badge photo attached (local OCR offline)" : "Badge photo attached");
    }
    await loadItems();
    renderLedger();
  }

  function navigateRow(delta) {
    if (!state.items.length) return;
    const idx = state.items.findIndex((x) => x.id === state.activeItemId);
    const nextIdx = idx < 0 ? 0 : Math.max(0, Math.min(state.items.length - 1, idx + delta));
    openDrawer(state.items[nextIdx].id);
  }

  function wireEvents() {
    document.querySelectorAll(".nav-tab").forEach((btn) =>
      btn.addEventListener("click", () => switchTab(btn.dataset.tab))
    );
    $("cellEvidence").addEventListener("click", () => switchTab("evidence"));
    $("cellProposals").addEventListener("click", () => switchTab("proposals"));

    let searchTimer = null;
    $("searchInput").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadItems().then(renderLedger), 180);
    });
    ["roomFilter", "categoryFilter", "statusFilter"].forEach((id) =>
      $(id).addEventListener("change", () => loadItems().then(renderLedger))
    );

    $("selectAllBox").addEventListener("change", (e) => {
      state.selectedIds.clear();
      if (e.target.checked) state.items.forEach((it) => state.selectedIds.add(it.id));
      renderLedger();
    });

    // Bulk actions
    $("btnBulkClear").addEventListener("click", () => {
      state.selectedIds.clear();
      $("selectAllBox").checked = false;
      renderLedger();
    });

    $("btnBulkMerge").addEventListener("click", async () => {
      const ids = Array.from(state.selectedIds);
      if (ids.length < 2) return;
      const [primaryId, ...mergeIds] = ids;
      await api("/api/items/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ primary_id: primaryId, merge_ids: mergeIds }),
      });
      state.selectedIds.clear();
      showToast(`Merged ${ids.length} multi-angle items`);
      await refreshAll();
      openDrawer(primaryId);
    });

    $("btnBulkConfirm").addEventListener("click", async () => {
      for (const id of state.selectedIds) {
        await api(`/api/items/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ review_status: "confirmed" }),
        });
      }
      const n = state.selectedIds.size;
      state.selectedIds.clear();
      showToast(`Confirmed ${n} items`);
      await refreshAll();
    });

    $("btnBulkArchive").addEventListener("click", async () => {
      const ids = Array.from(state.selectedIds);
      await api("/api/items/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_ids: ids, archive: true }),
      });
      state.selectedIds.clear();
      showToast(`Archived ${ids.length} items`);
      await refreshAll();
    });

    $("btnBulkDelete").addEventListener("click", async () => {
      const ids = Array.from(state.selectedIds);
      if (!confirm(`Permanently delete ${ids.length} selected items?`)) return;
      await api("/api/items/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_ids: ids, archive: false }),
      });
      if (ids.includes(state.activeItemId)) closeDrawer();
      state.selectedIds.clear();
      showToast(`Deleted ${ids.length} items`);
      await refreshAll();
    });

    // Drawer controls
    $("btnCloseDrawer").addEventListener("click", closeDrawer);
    $("btnSaveItem").addEventListener("click", () => saveActiveItem());
    $("btnConfirmItem").addEventListener("click", () => saveActiveItem("confirmed"));
    $("btnDeleteItem").addEventListener("click", async () => {
      if (!state.activeItemId) return;
      if (!confirm("Delete this asset from the ledger?")) return;
      await api(`/api/items/${state.activeItemId}`, { method: "DELETE" });
      closeDrawer();
      showToast("Asset deleted");
      await refreshAll();
    });

    // Badge photo dropzone
    const dz = $("badgeDropzone");
    const fileInput = $("badgeFileInput");
    $("btnPickBadge").addEventListener("click", () => fileInput.click());
    dz.addEventListener("click", (e) => { if (e.target === dz) fileInput.click(); });
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) uploadBadgeFile(fileInput.files[0]);
      fileInput.value = "";
    });
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
      if (e.dataTransfer.files[0]) uploadBadgeFile(e.dataTransfer.files[0]);
    });

    // Keyboard triage shortcuts (ignored while typing in form inputs)
    document.addEventListener("keydown", (e) => {
      const tag = (e.target.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(tag)) {
        if (e.key === "Escape") e.target.blur();
        return;
      }
      if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        navigateRow(1);
      } else if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        navigateRow(-1);
      } else if (e.key === "Escape" && state.activeItemId) {
        closeDrawer();
      } else if (e.key === "Enter" && state.activeItemId) {
        e.preventDefault();
        saveActiveItem("confirmed");
      }
    });
  }

  initTheme();
  wireEvents();
  refreshAll().catch((err) => showToast(`Load error: ${err.message}`));
})();
