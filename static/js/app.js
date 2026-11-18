(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let waterline = { min_fingerprint_len: 6, empty_message: "内容指纹为空，禁止登记" };
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let governance = { items: [], vouchers: [], withdrawals: [] };
  let dedupRuns = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t, isErr = false) {
    msgEl.textContent = t || "";
    msgEl.className = isErr ? "err" : "muted";
  }

  // 页面拦截与后台校验同一判定：规则取自 /api/meta 的 waterline，不另写一套。
  function normalizeFingerprint(v) {
    return String(v || "").trim();
  }
  function validateFingerprint(v) {
    const d = normalizeFingerprint(v);
    if (!d) return waterline.empty_message || "内容指纹为空，禁止登记";
    if (d.length < waterline.min_fingerprint_len)
      return waterline.too_short_message || `内容指纹短于水线（至少 ${waterline.min_fingerprint_len} 位）`;
    if (d.startsWith(waterline.placeholder_prefix))
      return "占位指纹不可作为正式指纹提交，请走强制复检";
    return "";
  }
  function isPlaceholder(v) {
    return normalizeFingerprint(v).startsWith(waterline.placeholder_prefix || "PLACEHOLDER");
  }

  async function refresh() {
    meta = await api("/api/meta");
    if (meta.waterline) waterline = meta.waterline;
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
  }

  async function refreshGovernance() {
    governance = await api("/api/empty-objects");
    dedupRuns = (await api("/api/dedup-runs")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    // 页面先拦：与后台 validate_fingerprint 同一水线。
    const frontErr = validateFingerprint(form.content_hash);
    if (frontErr) {
      setMsg(frontErr, true);
      return;
    }
    if (!form.object_key.trim()) {
      setMsg("对象键不能为空", true);
      return;
    }
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      // 后台判定为准：若页面与后台偏了，以后台白话报错停下。
      setMsg(e.message, true);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  // 处置：后端一次判定，track 二选一，两条轨互斥由后端裁定。
  async function doDispose(objectId, track) {
    setMsg("");
    try {
      const r = await api(`/api/empty-objects/${objectId}/dispose`, {
        method: "POST",
        body: JSON.stringify({ track }),
      });
      await refreshGovernance();
      if (r.track === "withdraw") setMsg("已撤登记并写撤回流水，请补真实指纹后重新登记");
      else setMsg(`已原子占位并落复检凭证 ${r.voucher.voucher_code}，待强制复检`);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  async function doRecheck(objectId) {
    const input = document.getElementById(`recheck-${objectId}`);
    const val = input ? input.value : "";
    const frontErr = validateFingerprint(val);
    if (frontErr) {
      setMsg(frontErr, true);
      return;
    }
    setMsg("");
    try {
      const r = await api(`/api/empty-objects/${objectId}/recheck`, {
        method: "POST",
        body: JSON.stringify({ content_hash: val }),
      });
      await refreshGovernance();
      setMsg(`复检通过，凭证 ${r.voucher.voucher_code} 已收口`);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  async function doDedup() {
    setMsg("");
    try {
      const r = await api("/api/dedup-runs", {
        method: "POST",
        body: JSON.stringify({ scope: "all" }),
      });
      await refreshGovernance();
      setMsg(r.run.detail);
      render();
    } catch (e) {
      setMsg(e.message, true);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}</td>
            <td><span class="pill">${b.status}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="pill">${b.status}</span></p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${form.object_key}" /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" placeholder="至少 ${waterline.min_fingerprint_len} 位，禁止空串" /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" /></label>
        <p class="muted waterline-hint">校验水线：指纹去空白后至少 ${waterline.min_fingerprint_len} 位；空串/占位指纹前后端同判，直接拦下。</p>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${o.object_key}</td>
            <td>${fingerprintCell(o.content_hash, o.status)}</td>
            <td>${o.byte_size}</td>
            <td>${statusPill(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function fingerprintCell(hash, status) {
    const h = String(hash || "");
    if (!h.trim()) return `<span class="chip chip-empty">空指纹</span>`;
    if (isPlaceholder(h)) return `<span class="chip chip-placeholder">占位待复检</span><div class="muted">${h}</div>`;
    if (h === "已撤回") return `<span class="chip chip-withdraw">已撤回</span>`;
    return h;
  }

  function statusPill(status) {
    const cls =
      status === "待复检" ? "pill pill-warn" : status === "已撤回" ? "pill pill-danger" : "pill";
    return `<span class="${cls}">${status}</span>`;
  }

  // 空串治理台：空串列表 + 处置芯片（撤回/占位）+ 凭证/流水，对照同一凭证。
  function renderGovernance() {
    const items = governance.items || [];
    const vouchers = governance.vouchers || [];
    const withdrawals = governance.withdrawals || [];
    return `
      <h2>空串治理</h2>
      <p class="muted">空串列表、登记附页、校验水线、去重编排台四处同查。处置二选一：撤回轨与占位复检轨互斥，后端一次裁定。</p>
      <div class="panel">
        <h3>待处置空串 / 待复检（${items.length}）</h3>
        ${
          items.length === 0
            ? `<p class="muted">暂无空串脏行，库存证干净。</p>`
            : `<table>
        <thead><tr><th>对象</th><th>批次</th><th>指纹/标记</th><th>状态</th><th>凭证</th><th>处置</th></tr></thead>
        <tbody>
          ${items
            .map((o) => {
              const busy = o.status === "待复检" || o.status === "已撤回";
              return `<tr>
              <td>${o.object_key}<div class="muted">#${o.id}</div></td>
              <td>${o.batch_id}</td>
              <td>${fingerprintCell(o.content_hash, o.status)}</td>
              <td>${statusPill(o.status)}</td>
              <td>${o.voucher_code ? `<span class="chip chip-placeholder">${o.voucher_code}</span>` : "—"}</td>
              <td class="action-cell">
                <button type="button" class="secondary" data-withdraw="${o.id}" ${busy ? "disabled" : ""}>撤回重填</button>
                <button type="button" data-placeholder="${o.id}" ${busy ? "disabled" : ""}>占位复检</button>
                ${
                  o.status === "待复检"
                    ? `<span class="recheck-row"><input id="recheck-${o.id}" placeholder="补真实指纹（≥${waterline.min_fingerprint_len}位）" />
                       <button type="button" data-recheck="${o.id}">强制复检</button></span>`
                    : ""
                }
              </td>
            </tr>`;
            })
            .join("")}
        </tbody></table>`
        }
      </div>
      <div class="panel">
        <h3>复检凭证表（${vouchers.length}）</h3>
        ${
          vouchers.length === 0
            ? `<p class="muted">无凭证</p>`
            : `<table><thead><tr><th>凭证号</th><th>对象</th><th>占位指纹</th><th>状态</th><th>复检指纹</th></tr></thead>
          <tbody>${vouchers
            .map(
              (v) => `<tr>
            <td>${v.voucher_code}</td><td>#${v.object_id}</td>
            <td class="muted">${v.placeholder_hash}</td>
            <td>${statusPill(v.status === "待复检" ? "待复检" : "已登记")}</td>
            <td>${v.final_hash || "—"}</td></tr>`
            )
            .join("")}</tbody></table>`
        }
      </div>
      <div class="panel">
        <h3>撤回流水（${withdrawals.length}）</h3>
        ${
          withdrawals.length === 0
            ? `<p class="muted">无撤回流水</p>`
            : `<table><thead><tr><th>对象</th><th>键</th><th>事由</th><th>时间</th></tr></thead>
          <tbody>${withdrawals
            .map((w) => `<tr><td>#${w.object_id}</td><td>${w.object_key}</td><td>${w.reason}</td><td class="muted">${w.created_at}</td></tr>`)
            .join("")}</tbody></table>`
        }
      </div>`;
  }

  // 去重编排台：对照同一凭证，复扫空串/占位一律拦截计数，不再吃空串。
  function renderDedup() {
    const runs = dedupRuns || [];
    const openVouchers = (governance.vouchers || []).filter((v) => v.status === "待复检");
    return `
      <h2>去重编排台</h2>
      <p class="muted">复扫同一水线：空串、撤回记号、占位指纹一律拦截，不参与去重、不落库。对照复检凭证 ${openVouchers.length} 张待复检。</p>
      <div class="row-actions"><button type="button" id="btn-dedup">立即复扫（全量）</button></div>
      <table>
        <thead><tr><th>编号</th><th>范围</th><th>扫描</th><th>重复</th><th>拦截空串/占位</th><th>结论</th><th>时间</th></tr></thead>
        <tbody>
          ${
            runs.length === 0
              ? `<tr><td colspan="7" class="muted">尚未扫库</td></tr>`
              : runs
                  .map(
                    (r) => `<tr>
                  <td>${r.id}</td><td>${r.scope}</td><td>${r.scanned_count}</td>
                  <td>${r.duplicate_count}</td>
                  <td><span class="chip chip-empty">${r.empty_blocked_count}</span></td>
                  <td class="muted">${r.detail}</td><td class="muted">${r.created_at}</td></tr>`
                  )
                  .join("")
          }
        </tbody>
      </table>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td><td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "governance") view.innerHTML = renderGovernance();
    else if (tab === "dedup") view.innerHTML = renderDedup();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    view.querySelectorAll("[data-withdraw]").forEach((btn) => {
      btn.onclick = () => doDispose(Number(btn.dataset.withdraw), "withdraw");
    });
    view.querySelectorAll("[data-placeholder]").forEach((btn) => {
      btn.onclick = () => doDispose(Number(btn.dataset.placeholder), "placeholder");
    });
    view.querySelectorAll("[data-recheck]").forEach((btn) => {
      btn.onclick = () => doRecheck(Number(btn.dataset.recheck));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const dedupBtn = document.getElementById("btn-dedup");
    if (dedupBtn) dedupBtn.onclick = doDedup;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = async () => {
      tab = btn.dataset.tab;
      if (tab === "governance" || tab === "dedup") await refreshGovernance();
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message, true));
})();
