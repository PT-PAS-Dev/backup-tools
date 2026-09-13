const fmtNum = (value) => {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  return n.toLocaleString("id-ID");
};

const fmtSize = (bytes) => {
  if (bytes === null || bytes === undefined) return "N/A";
  let value = Number(bytes);
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (const unit of units) {
    if (value < 1024 || unit === "TB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${bytes} B`;
};

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function setCloneOut(out, text, { error = false } = {}) {
  if (!out) return;
  out.textContent = text;
  out.classList.toggle("tone-crit", error);
}

function formatPrecheck(result) {
  const lines = [];
  if (result.ok) {
    lines.push("Pre-check OK — siap start clone.");
  } else {
    lines.push("Pre-check GAGAL (perbaiki dulu):");
    for (const name of result.blocking || []) {
      const check = (result.checks || []).find((c) => c.name === name);
      lines.push(`  • ${name}: ${check?.detail || ""}`);
    }
  }
  if (result.warnings?.length) {
    lines.push("");
    lines.push("Peringatan (tidak memblokir):");
    for (const name of result.warnings) {
      const check = (result.checks || []).find((c) => c.name === name);
      lines.push(`  • ${name}: ${check?.detail || ""}`);
    }
  }
  if (result.replace_needed) {
    const names = (result.replace_databases || []).join(", ");
    lines.push("");
    lines.push(`Centang «Ganti database…» — sudah ada di clone: ${names || "?"}`);
  }
  if (result.can_replicate === false) {
    lines.push("");
    lines.push("Tanpa CLONE_REPL_*: data tetap di-copy; START SLAVE dilewati.");
  }
  return lines.join("\n");
}

function renderTopology(overview) {
  const root = document.getElementById("topology");
  if (!root || !overview.nodes) return;
  root.innerHTML = overview.nodes
    .map((node, index) => {
      const gtid = (node.gtid && (node.gtid.current_pos || node.gtid.slave_pos)) || "n/a";
      const card = `
        <div class="node tone-${escapeHtml(node.health?.tone)}">
          <div class="node-head">
            <span class="dot ${escapeHtml(node.health?.tone)}"></span>
            <div>
              <strong>${escapeHtml(node.display_host)} · ${escapeHtml(node.host)}</strong>
              <small>${escapeHtml(node.title)} · ${escapeHtml(node.name)}</small>
            </div>
            <span class="badges">
              <span class="status mysql tone-${escapeHtml(node.mysql?.tone)}" title="${escapeHtml(node.mysql?.detail)}">MySQL ${escapeHtml(node.mysql?.label || (node.online ? "CONNECTED" : "FAILED"))}</span>
              ${node.role === "clone" ? `<span class="status clone">${escapeHtml(node.clone_status)}</span>` : ""}
            </span>
          </div>
          <dl class="facts">
            <div><dt>MySQL connect</dt><dd class="tone-${escapeHtml(node.mysql?.tone)}">${escapeHtml(node.mysql?.label || "")}${node.mysql?.status === "CONNECTED" ? " · " + escapeHtml(node.mysql?.detail || "") : ""}</dd></div>
            <div><dt>MySQL user</dt><dd class="mono">${escapeHtml(node.mysql_user || "—")}@${escapeHtml(node.host)}:${escapeHtml(node.mysql_port || 3306)}</dd></div>
            ${node.mysql?.status === "FAILED" ? `<div class="full"><dt>MySQL error</dt><dd class="mono err">${escapeHtml(node.mysql?.detail || node.last_error || "")}</dd></div>` : ""}
            <div><dt>Health</dt><dd>${escapeHtml(node.health?.label)}</dd></div>
            ${node.role === "clone" ? `<div><dt>Clone / repl</dt><dd>${escapeHtml(node.clone_status)} · lag ${escapeHtml(node.replication?.lag_text)}</dd></div>` : ""}
            <div><dt>Size</dt><dd>${escapeHtml(node.size_human)}</dd></div>
            <div><dt>Lag</dt><dd>${escapeHtml(node.replication?.lag_text)}</dd></div>
            <div><dt>GTID</dt><dd class="mono">${escapeHtml(gtid)}</dd></div>
            <div><dt>Binlog</dt><dd class="mono">${escapeHtml(node.binlog_file || "n/a")} ${escapeHtml(node.binlog_pos || "")}</dd></div>
            <div><dt>CPU</dt><dd>${escapeHtml(node.cpu_text)}</dd></div>
            <div><dt>RAM</dt><dd>${escapeHtml(node.ram_text)}</dd></div>
            <div><dt>Disk</dt><dd>${escapeHtml(node.disk_text)}</dd></div>
            <div><dt>Uptime</dt><dd>${escapeHtml(node.uptime_human)}</dd></div>
            <div><dt>Last error</dt><dd>${escapeHtml(node.last_error || "none")}</dd></div>
            <div><dt>Sampled</dt><dd>${escapeHtml(node.sampled_at || "—")}</dd></div>
          </dl>
          ${node.role === "source" ? '<p class="rw">READ/WRITE production master</p>' : ""}
        </div>`;
      const connector = index < overview.nodes.length - 1 ? '<div class="connector">Replication</div>' : "";
      return card + connector;
    })
    .join("");
}

function renderHealth(overview) {
  const root = document.getElementById("health-cards");
  if (!root) return;
  const rows = [];
  if (overview.source) {
    rows.push(overview.source);
  }
  for (const node of overview.clones || []) rows.push(node);
  root.innerHTML = rows
    .map((node) => {
      const kind = node.role === "source" ? "MASTER" : "CLONE";
      const extra = node.role === "source" ? node.host : `MySQL ${node.mysql?.label || "?"} · repl ${node.clone_status}`;
      return `<div class="health-row tone-${escapeHtml(node.health?.tone)}">
        <div><strong>${escapeHtml(node.display_host)} ${kind}</strong><small>${escapeHtml(extra)}</small></div>
        <span>${escapeHtml(node.health?.label)}</span>
      </div>`;
    })
    .join("");
}

async function refreshDashboard() {
  if (document.body.dataset.page !== "dashboard") return;
  try {
    const overview = await getJson("/api/overview");
    renderTopology(overview);
    renderHealth(overview);
  } catch (error) {
    console.warn(error);
  }
}

const SYNC_STATUS_ID = {
  synced: "Sinkron",
  partial: "Sebagian",
  missing: "Belum ada",
};

let syncTableFilter = "all";

function syncTag(status) {
  const label = SYNC_STATUS_ID[status] || status;
  return `<span class="sync-tag ${escapeHtml(status)}">${escapeHtml(label)}</span>`;
}

function renderSyncClones(rows) {
  const root = document.getElementById("sync-clones-root");
  if (!root || !rows) return;
  if (!rows.length) {
    root.innerHTML = "<p class='hint'>Belum ada node clone di config.</p>";
    return;
  }
  root.innerHTML = rows
    .map((row) => {
      const s = row.sync || {};
      if (!s.clone_online) {
        return `<article class="sync-clone-card" data-clone="${escapeHtml(row.clone)}">
          <header class="sync-clone-head">
            <div><h3>${escapeHtml(row.display_host)} · ${escapeHtml(row.host)}</h3>
            <p class="kicker">${escapeHtml(row.clone)} ← ${escapeHtml(row.upstream)} · ${escapeHtml(row.clone_status)}</p></div>
            <span class="sync-pill tone-crit">OFFLINE</span>
          </header>
          <p class="hint tone-crit">Clone offline — tidak bisa bandingkan dengan .94.</p>
        </article>`;
      }
      const sizePct = s.size_sync_percent ?? 0;
      const tablePct = s.table_sync_percent ?? 0;
      const dbs = (s.databases || [])
        .map(
          (db) => `<tr>
            <td class="mono">${escapeHtml(db.name)}</td>
            <td>${escapeHtml(fmtSize(db.source_size))}</td>
            <td>${escapeHtml(fmtSize(db.clone_size))}</td>
            <td class="num">${fmtNum(db.source_rows)}</td>
            <td class="num">${fmtNum(db.clone_rows)}</td>
            <td class="num">${db.row_percent ?? "—"}%</td>
            <td>${db.source_tables}/${db.clone_tables}</td>
            <td>${db.sync_percent ?? 0}%</td>
            <td>${syncTag(db.sync_status)}</td>
          </tr>`
        )
        .join("");
      const tables = (s.tables || [])
        .map(
          (t) => `<tr data-sync-status="${escapeHtml(t.status)}" data-rows-match="${t.rows_match ? "true" : "false"}">
            <td class="mono" title="${escapeHtml(t.full_name)}">${escapeHtml(t.full_name)}</td>
            <td class="num">${fmtNum(t.source_rows)}</td>
            <td class="num">${fmtNum(t.clone_rows)}</td>
            <td class="num ${t.rows_match ? "" : "num-warn"}">${fmtNum(t.row_delta)}</td>
            <td class="num">${t.row_percent ?? "—"}%</td>
            <td>${escapeHtml(fmtSize(t.source_bytes))}</td>
            <td>${escapeHtml(fmtSize(t.clone_bytes))}</td>
            <td>${t.percent ?? 0}%</td>
            <td>${syncTag(t.status)}</td>
          </tr>`
        )
        .join("");
      const live =
        s.live_size_percent != null
          ? `<p class="hint tone-ok">Job clone aktif — ~${s.live_size_percent}% ukuran vs .94 (estimasi poll).</p>`
          : "";
      return `<article class="sync-clone-card" data-clone="${escapeHtml(row.clone)}">
        <header class="sync-clone-head">
          <div><h3>${escapeHtml(row.display_host)} · ${escapeHtml(row.host)}</h3>
          <p class="kicker">${escapeHtml(row.clone)} ← ${escapeHtml(row.upstream)} · ${escapeHtml(row.clone_status)}</p></div>
          <span class="sync-pill tone-${s.overall === "MATCH" ? "ok" : "warn"}">${escapeHtml(s.overall || "—")}</span>
        </header>
        ${live}
        <div class="sync-meters">
          <div class="sync-meter">
            <div class="sync-meter-label"><span>Ukuran data vs .94</span><strong>${s.size_sync_percent ?? "—"}%</strong></div>
            <div class="bar lg"><span style="width:${sizePct}%"></span></div>
          </div>
          <div class="sync-meter">
            <div class="sync-meter-label"><span>Tabel identik (ukuran)</span><strong>${s.table_synced ?? 0} / ${s.table_total ?? 0} (${s.table_sync_percent ?? "—"}%)</strong></div>
            <div class="bar lg bar-muted"><span style="width:${tablePct}%"></span></div>
          </div>
          <div class="sync-meter">
            <div class="sync-meter-label"><span>Baris vs .94 (estimasi)</span><strong>${fmtNum(s.clone_row_total)} / ${fmtNum(s.source_row_total)} (${s.row_sync_percent ?? "—"}%)</strong></div>
            <div class="bar lg bar-rows"><span style="width:${s.row_sync_percent ?? 0}%"></span></div>
          </div>
          <div class="sync-meter">
            <div class="sync-meter-label"><span>Tabel baris match</span><strong>${s.table_rows_matched ?? 0} / ${s.table_total ?? 0} (${s.table_row_sync_percent ?? "—"}%)</strong></div>
            <div class="bar lg bar-muted"><span style="width:${s.table_row_sync_percent ?? 0}%"></span></div>
          </div>
        </div>
        <dl class="facts dense sync-facts">
          <div><dt>Replikasi</dt><dd>${s.replication_running ? "Jalan" : "Tidak / belum"}</dd></div>
          <div><dt>Lag</dt><dd>${s.replication_lag ?? "n/a"} d</dd></div>
          <div><dt>Sebagian</dt><dd>${s.table_partial ?? 0} tabel</dd></div>
          <div><dt>Belum ada</dt><dd>${s.table_missing ?? 0} tabel</dd></div>
        </dl>
        ${s.inventory_has_tables ? "" : "<p class='hint'>Menunggu inventory tabel dari poll berikutnya…</p>"}
        <details class="sync-details" open>
          <summary>Database (${(s.databases || []).length})</summary>
          <div class="table-wrap compact"><table class="sync-db-table">
            <thead><tr><th>Database</th><th>Src</th><th>Clone</th><th>Baris .94</th><th>Baris clone</th><th>% baris</th><th>Tabel</th><th>% ukuran</th><th>Status</th></tr></thead>
            <tbody>${dbs}</tbody>
          </table></div>
        </details>
        <details class="sync-details">
          <summary>Tabel (${s.tables_total ?? (s.tables || []).length})</summary>
          ${
            s.tables_truncated > 0
              ? `<p class="hint">Menampilkan ${(s.tables || []).length} dari ${s.tables_total} tabel — gunakan filter.</p>`
              : ""
          }
          <div class="table-wrap sync-table-scroll">
            <table class="sync-table-list"><thead><tr><th>Tabel</th><th>Baris .94</th><th>Baris clone</th><th>Δ</th><th>% baris</th><th>Ukuran</th><th>Clone</th><th>% ukuran</th><th>Status</th></tr></thead>
            <tbody>${tables}</tbody></table>
          </div>
        </details>
      </article>`;
    })
    .join("");
  applySyncTableFilter();
}

function applySyncTableFilter() {
  const filter = syncTableFilter;
  for (const tbody of document.querySelectorAll(".sync-table-list tbody")) {
    for (const row of tbody.querySelectorAll("tr[data-sync-status]")) {
      const status = row.getAttribute("data-sync-status");
      const rowsMatch = row.getAttribute("data-rows-match");
      let show = filter === "all" || status === filter;
      if (filter === "rows-diff") {
        show = rowsMatch === "false";
      }
      row.hidden = !show;
    }
  }
}

function showProgressTab(name) {
  const tab = name === "clone" ? "clone" : "progress";
  for (const panel of document.querySelectorAll(".page-tab-panel")) {
    const active = panel.dataset.tabPanel === tab;
    panel.classList.toggle("hidden", !active);
    panel.hidden = !active;
  }
  for (const btn of document.querySelectorAll("#progress-tabs .page-tab")) {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
  try {
    history.replaceState(null, "", tab === "clone" ? "#clone" : "#progress");
  } catch {
    /* ignore */
  }
}

function wireProgressPageTabs() {
  const nav = document.getElementById("progress-tabs");
  if (!nav) return;
  nav.addEventListener("click", (event) => {
    const btn = event.target.closest(".page-tab[data-tab]");
    if (!btn) return;
    showProgressTab(btn.dataset.tab);
  });
  const hash = (location.hash || "").replace("#", "");
  if (hash === "clone") {
    showProgressTab("clone");
  } else {
    showProgressTab("progress");
  }
}

function wireSyncFilters() {
  const bar = document.getElementById("sync-table-filter-global");
  if (!bar) return;
  bar.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-sync-filter]");
    if (!btn) return;
    syncTableFilter = btn.dataset.syncFilter || "all";
    for (const b of bar.querySelectorAll("button")) b.classList.toggle("on", b === btn);
    applySyncTableFilter();
  });
}

async function refreshProgress() {
  if (document.body.dataset.page !== "progress") return;
  try {
    const data = await getJson("/api/progress");
    renderSyncClones(data.sync_clones);
    const card = document.getElementById("job-card");
    const job = data.latest;
    if (card) {
      if (!job) {
        card.innerHTML = "<p class='hint'>Tidak ada job clone aktif. Gunakan form di kanan lalu Start clone.</p>";
      } else {
        const reliable = job.progress_reliable && job.percent !== null;
        let progressHtml;
        if (reliable) {
          progressHtml = `<div class="bar lg"><span style="width:${job.percent}%"></span></div>
            <p>${job.percent}% ukuran · ${escapeHtml(job.copied_human)} / ${escapeHtml(job.total_human)} · sisa ${escapeHtml(job.remaining_human)}</p>`;
        } else if (job.status === "RUNNING") {
          progressHtml = `<div class="bar lg indeterminate"><span></span></div>
            <p class="hint">${escapeHtml(job.phase_label || job.phase || "Clone berjalan")} — buka tab <strong>Progress sinkron</strong> untuk meter ukuran/baris.</p>`;
        } else {
          progressHtml = `<p class="hint">${escapeHtml(job.message || "")}</p>`;
        }
        card.innerHTML = `
          <p class="kicker">${escapeHtml(job.source)} → ${escapeHtml(job.target)}</p>
          <p class="status-lg">${escapeHtml(job.status)} · ${escapeHtml(job.phase || "")}</p>
          ${progressHtml}
          <pre class="log">${escapeHtml(job.log || "Belum ada log.")}</pre>`;
      }
    }
    const hist = document.getElementById("job-history-body");
    if (hist && data.jobs) {
      hist.innerHTML = data.jobs
        .map(
          (j) => `<tr>
            <td>${escapeHtml(j.ts)}</td>
            <td>${escapeHtml(j.source)} → ${escapeHtml(j.target)}</td>
            <td>${escapeHtml(j.status)}</td>
            <td>${escapeHtml(j.phase || "")}</td>
            <td>${escapeHtml(j.message || "")}</td>
          </tr>`
        )
        .join("");
    }
  } catch (error) {
    console.warn(error);
  }
}

function dbCheckboxes() {
  return [...document.querySelectorAll('#db-list input[type="checkbox"][name="db"]')];
}

function selectedDatabases() {
  return dbCheckboxes().filter((el) => el.checked).map((el) => el.value);
}

function setAllDbChecks(checked) {
  for (const el of dbCheckboxes()) {
    el.checked = checked;
  }
  updateDbSummary();
}

function setUnsyncedChecks() {
  for (const el of dbCheckboxes()) {
    el.checked = el.dataset.synced !== "1";
  }
  updateDbSummary();
}

function updateDbSummary() {
  const summary = document.getElementById("db-picker-summary");
  if (!summary) return;
  const boxes = dbCheckboxes();
  const checked = selectedDatabases();
  let bytes = 0;
  for (const box of boxes) {
    if (box.checked) bytes += Number(box.dataset.size || 0);
  }
  const synced = boxes.filter((el) => el.dataset.synced === "1").length;
  summary.textContent =
    boxes.length === 0
      ? "Centang database yang mau di-clone (klik Muat ulang jika kosong)."
      : `${checked.length} / ${boxes.length} dipilih · ${fmtSize(bytes)} akan disalin · ${synced} sudah sync di clone (abaikan)`;
}

async function loadDatabaseCatalog(target) {
  const list = document.getElementById("db-list");
  const summary = document.getElementById("db-picker-summary");
  if (!list) return;
  list.innerHTML = "<p class='hint'>Memuat dari source…</p>";
  try {
    const data = await getJson(`/api/clone/databases?target=${encodeURIComponent(target)}`);
    if (!data.databases?.length) {
      list.innerHTML = "<p class='hint'>No application databases on source.</p>";
      updateDbSummary();
      return;
    }
    list.innerHTML = `<table class="db-table"><thead><tr>
      <th class="col-check"></th><th class="col-name">Database</th><th class="col-status">Di clone</th><th class="col-size">Ukuran</th><th class="col-tables">Tabel</th>
    </tr></thead><tbody>${data.databases
      .map((db) => {
        const synced = Boolean(db.synced);
        const label = db.sync_label || SYNC_STATUS_ID[db.sync_status] || db.sync_status || "—";
        const title =
          synced
            ? `Sudah ada di ${data.target_host || data.target} (ukuran/tabel/baris ≈ source)`
            : db.sync_status === "partial"
              ? `Sebagian: clone ${db.clone_size_human || "?"} · ${db.row_sync_percent ?? "?"}% baris`
              : "Belum ada di clone — perlu di-clone";
        return `<tr class="${synced ? "db-synced" : ""}">
          <td class="col-check"><input type="checkbox" name="db" value="${escapeHtml(db.name)}" data-size="${Number(db.size_bytes || 0)}" data-synced="${synced ? "1" : "0"}" ${synced ? "" : "checked"} aria-label="${escapeHtml(db.name)}"></td>
          <td class="col-name"><span class="mono" title="${escapeHtml(db.name)}">${escapeHtml(db.name)}</span></td>
          <td class="col-status"><span class="sync-tag ${escapeHtml(db.sync_status || "missing")}" title="${escapeHtml(title)}">${escapeHtml(label)}</span></td>
          <td class="col-size">${escapeHtml(db.size_human)}</td>
          <td class="col-tables">${escapeHtml(db.table_count)}</td>
        </tr>`;
      })
      .join("")}</tbody></table>`;
    list.querySelectorAll('input[type="checkbox"][name="db"]').forEach((el) =>
      el.addEventListener("change", updateDbSummary)
    );
    updateDbSummary();
    if (summary && data.synced_count != null) {
      summary.textContent = `Source ${escapeHtml(data.source_host)} → clone ${escapeHtml(data.target_host || data.target)} · ${data.synced_count}/${data.databases.length} DB sudah sync · centang yang belum sync saja`;
    }
    updateDbSummary();
  } catch (error) {
    list.innerHTML = `<p class="hint">${escapeHtml(error.message)}</p>`;
  }
}

function wireCloneForm() {
  const form = document.getElementById("clone-form");
  if (!form) return;
  const out = document.getElementById("precheck-out");
  const targetSelect = document.getElementById("clone-target") || form.target;

  const reload = () => loadDatabaseCatalog(targetSelect.value);
  const dbPicker = document.querySelector(".db-picker");
  if (document.body.dataset.page === "progress") {
    dbCheckboxes().forEach((el) => el.addEventListener("change", updateDbSummary));
    updateDbSummary();
    if (!dbCheckboxes().length) {
      reload();
    }
    dbPicker?.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button || !dbPicker.contains(button)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (button.id === "db-select-all") {
        setAllDbChecks(true);
      } else if (button.id === "db-select-none") {
        setAllDbChecks(false);
      } else if (button.id === "db-select-unsynced") {
        setUnsyncedChecks();
      } else if (button.id === "db-reload") {
        reload();
      }
    });
    document.getElementById("db-list")?.addEventListener("click", (event) => {
      const row = event.target.closest("tbody tr");
      if (!row || event.target.closest('input[type="checkbox"]')) {
        return;
      }
      const box = row.querySelector('input[type="checkbox"][name="db"]');
      if (box) {
        box.checked = !box.checked;
        updateDbSummary();
      }
    });
  }

  targetSelect?.addEventListener("change", reload);

  document.getElementById("precheck-btn")?.addEventListener("click", async () => {
    setCloneOut(out, "Menjalankan pre-check…");
    const databases = selectedDatabases();
    if (!databases.length) {
      setCloneOut(out, "Pilih minimal satu database.", { error: true });
      return;
    }
    try {
      const target = targetSelect.value;
      const result = await getJson("/api/clone/precheck", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, databases }),
      });
      setCloneOut(out, formatPrecheck(result), { error: !result.ok });
    } catch (error) {
      setCloneOut(out, `Pre-check error: ${error.message}`, { error: true });
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const databases = selectedDatabases();
    if (!databases.length) {
      setCloneOut(out, "Pilih minimal satu database sebelum start clone.", { error: true });
      return;
    }
    if (!window.confirm(`Clone ${databases.length} database(s) on the CLONE host only. Production .94 stays up. Continue?`)) {
      return;
    }
    setCloneOut(out, "Memulai job clone…");
    try {
      const result = await getJson("/api/clone/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target: targetSelect.value,
          confirm: form.confirm.value,
          replace: form.replace.checked,
          databases,
        }),
      });
      setCloneOut(out, `Clone dimulai. Job #${result.job_id} — lihat «Job saat ini» di kiri.`);
      showProgressTab("clone");
      refreshProgress();
      document.getElementById("job-card")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      setCloneOut(out, `Start clone gagal:\n${error.message}`, { error: true });
    }
  });
}

const charts = {};
const palette = ["#9ad16a", "#6cb3d6", "#e0b144", "#d989ff"];

function makeChart(id, label) {
  const canvas = document.getElementById(id);
  if (!canvas || typeof Chart === "undefined") return null;
  return new Chart(canvas, {
    type: "line",
    data: { datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#93a58c" } },
        title: { display: true, text: label, color: "#e7f0e4" },
      },
      scales: {
        x: { ticks: { color: "#93a58c", maxTicksLimit: 6 }, grid: { color: "#314231" } },
        y: { ticks: { color: "#93a58c" }, grid: { color: "#314231" } },
      },
    },
  });
}

function setSeries(chart, series, formatter) {
  if (!chart) return;
  const servers = Object.keys(series || {});
  chart.data.datasets = servers.map((server, index) => ({
    label: server,
    data: (series[server] || []).map((point) => ({ x: point[0], y: point[1] })),
    borderColor: palette[index % palette.length],
    backgroundColor: "transparent",
    tension: 0.15,
    pointRadius: 0,
    spanGaps: false,
  }));
  chart.options.parsing = false;
  chart.options.scales.x.type = "category";
  if (formatter) {
    chart.options.scales.y.ticks.callback = formatter;
  }
  const labels = new Set();
  for (const server of servers) {
    for (const point of series[server] || []) labels.add(point[0]);
  }
  chart.data.labels = [...labels].sort();
  chart.data.datasets = servers.map((server, index) => ({
    label: server,
    data: chart.data.labels.map((label) => {
      const row = (series[server] || []).find((point) => point[0] === label);
      return row ? row[1] : null;
    }),
    borderColor: palette[index % palette.length],
    backgroundColor: "transparent",
    tension: 0.15,
    pointRadius: 0,
    spanGaps: false,
  }));
  chart.update();
}

async function loadHistory(range) {
  const data = await getJson(`/api/history?range=${encodeURIComponent(range)}`);
  if (!charts.lag) {
    charts.lag = makeChart("chart-lag", "Replication lag (sec)");
    charts.size = makeChart("chart-size", "Database size");
    charts.cpu = makeChart("chart-cpu", "CPU %");
    charts.ram = makeChart("chart-ram", "RAM %");
    charts.disk = makeChart("chart-disk", "Disk used %");
    charts.online = makeChart("chart-online", "Online (1/0)");
  }
  setSeries(charts.lag, data.series.lag_seconds);
  setSeries(charts.size, data.series.size_bytes, (value) => fmtSize(value));
  setSeries(charts.cpu, data.series.cpu_percent);
  setSeries(charts.ram, data.series.ram_percent);
  setSeries(charts.disk, data.series.disk_percent);
  setSeries(charts.online, data.series.online);
}

function wireHistory() {
  if (document.body.dataset.page !== "history") return;
  const pills = document.getElementById("range-pills");
  pills?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-range]");
    if (!button) return;
    for (const item of pills.querySelectorAll("button")) item.classList.remove("on");
    button.classList.add("on");
    loadHistory(button.dataset.range);
  });
  loadHistory("24h");
}

document.addEventListener("DOMContentLoaded", () => {
  wireProgressPageTabs();
  wireCloneForm();
  wireSyncFilters();
  wireHistory();
  refreshDashboard();
  refreshProgress();
  setInterval(() => {
    if (!document.hidden) {
      refreshDashboard();
    }
  }, 20000);
  setInterval(() => {
    if (document.body.dataset.page !== "progress" || document.hidden) {
      return;
    }
    refreshProgress();
  }, 30000);
});
