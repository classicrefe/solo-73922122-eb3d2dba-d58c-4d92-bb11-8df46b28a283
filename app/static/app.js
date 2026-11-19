/* 病例切分平台前端（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let currentVersion = null;
let currentState = null;
let lastPreview = null; // {token, version, result, input}

// --------------------------------------------------------------------------- //
// 基础请求
// --------------------------------------------------------------------------- //
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (!resp.ok) {
    const msg = data?.detail || `请求失败（HTTP ${resp.status}）`;
    const errs = data?.errors ? `：${data.errors.join("；")}` : "";
    throw new Error(msg + errs);
  }
  return data;
}

function setMsg(id, text, ok = false) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "msg " + (ok ? "ok" : "err");
}

function downloadCsv(filename, content) {
  const blob = new Blob(["﻿" + content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 标签页
// --------------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// --------------------------------------------------------------------------- //
// 模板下载
// --------------------------------------------------------------------------- //
$("tplCases").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("cases_template.csv", "病例号,患者号,标签\nC001,P001,1\nC002,P001,0\nC003,P002,1\n");
});
$("tplRel").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("relatives_template.csv", "患者A,患者B\nP001,P002\n");
});
$("tplDevices").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCsv("devices_template.csv", "病例号,设备型号\nC001,A\nC002,B\n");
});

// --------------------------------------------------------------------------- //
// 上传
// --------------------------------------------------------------------------- //
async function uploadCases() {
  const f = $("casesFile").files[0];
  if (!f) return setMsg("casesMsg", "请先选择病例 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("casesMsg", "上传中…");
  try {
    const r = await api("/api/upload/cases", { method: "POST", body: fd });
    setMsg("casesMsg", `已上传 ${r.case_count} 个病例，新版本 v${r.dataset_version}。${r.notice}`, true);
    $("casesFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("casesMsg", err.message); }
}

async function uploadRelatives() {
  const f = $("relFile").files[0];
  if (!f) return setMsg("relMsg", "请先选择亲属关系 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("relMsg", "上传中…");
  try {
    const r = await api("/api/upload/relatives", { method: "POST", body: fd });
    setMsg("relMsg", `已上传 ${r.pair_count} 对亲属关系，新版本 v${r.dataset_version}`, true);
    $("relFile").value = "";
    await refreshAll();
  } catch (err) { setMsg("relMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 渲染当前数据
// --------------------------------------------------------------------------- //
function renderState() {
  const version = currentState.dataset_version;
  const input = currentState.input;
  currentVersion = version;
  $("currentVersion").textContent = "v" + version;

  const totals = currentState.label_totals;
  $("totL0").textContent = totals[0];
  $("totL1").textContent = totals[1];
  $("t_l0").textContent = totals[0];
  $("t_l1").textContent = totals[1];

  $("overview").innerHTML = [
    ["病例数", input.cases.length],
    ["患者数", currentState.patient_count],
    ["亲属对数", input.relatives.length],
    ["锁定数", Object.keys(input.locks).length],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  $("caseCount").textContent = input.cases.length;
  $("caseTable").querySelector("tbody").innerHTML = input.cases
    .map((c) => `<tr><td>${escapeHtml(c.case_id)}</td><td>${escapeHtml(c.patient_id)}</td><td>${c.label}</td></tr>`)
    .join("");

  $("relCount").textContent = input.relatives.length;
  $("relTable").querySelector("tbody").innerHTML = input.relatives
    .map((r) => `<tr><td>${escapeHtml(r.patient_a)}</td><td>${escapeHtml(r.patient_b)}</td></tr>`)
    .join("");

  // 配额回填（仅当输入框未被聚焦时，避免打断输入）
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const el = $("q_" + key);
    if (document.activeElement !== el) el.value = input.quotas[key] ?? 0;
  }
  renderLocks();
  renderManifestTable();
  renderBiasManifestSelect();
}

// --------------------------------------------------------------------------- //
// 配额
// --------------------------------------------------------------------------- //
async function saveQuotas() {
  const payload = {};
  for (const key of ["g1_label0", "g1_label1", "g2_label0", "g2_label1"]) {
    const v = Number($("q_" + key).value);
    if (!Number.isInteger(v) || v < 0) {
      return setMsg("quotaMsg", `配额 ${key} 必须是非负整数`);
    }
    payload[key] = v;
  }
  try {
    const r = await api("/api/quotas", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMsg("quotaMsg", `配额已保存，新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("quotaMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 锁定表
// --------------------------------------------------------------------------- //
function renderLocks() {
  if (!currentState) return;
  const input = currentState.input;
  // 轮询重建前保留用户尚未保存的选择，避免被 4 秒刷新冲掉
  const prior = {};
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    prior[sel.dataset.lockCase] = sel.value;
  });
  const filter = $("lockFilter").value.trim().toLowerCase();
  const rows = input.cases
    .filter((c) => !filter || c.case_id.toLowerCase().includes(filter) ||
      c.patient_id.toLowerCase().includes(filter))
    .map((c) => {
      const saved = input.locks[c.case_id];
      const effective = Object.prototype.hasOwnProperty.call(prior, c.case_id)
        ? prior[c.case_id]
        : (saved === undefined ? "" : String(saved));
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td><select data-lock-case="${escapeHtml(c.case_id)}">
          <option value=""${effective === "" ? " selected" : ""}>不锁定</option>
          <option value="1"${effective === "1" ? " selected" : ""}>训练集（组1）</option>
          <option value="2"${effective === "2" ? " selected" : ""}>评估集（组2）</option>
        </select></td></tr>`;
    }).join("");
  $("lockTable").querySelector("tbody").innerHTML = rows;
}

async function saveLocks() {
  const locks = [];
  document.querySelectorAll("[data-lock-case]").forEach((sel) => {
    if (sel.value !== "") locks.push({ case_id: sel.dataset.lockCase, group: Number(sel.value) });
  });
  try {
    const r = await api("/api/locks", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locks }),
    });
    setMsg("lockMsg", `锁定已保存（${r.lock_count} 项），新版本 v${r.dataset_version}`, true);
    await refreshAll();
  } catch (err) { setMsg("lockMsg", err.message); }
}

// --------------------------------------------------------------------------- //
// 求解
// --------------------------------------------------------------------------- //
async function runSolve() {
  $("solveStatus").textContent = "z3 求解中…";
  $("solveStatus").className = "msg";
  $("satBox").classList.add("hidden");
  $("unsatBox").classList.add("hidden");
  try {
    const r = await api("/api/solve", { method: "POST" });
    lastPreview = {
      token: r.preview_token,
      version: r.dataset_version,
      result: r.result,
      input: r.input_snapshot,
    };
    renderSolveResult();
    $("solveStatus").textContent =
      r.result.status === "sat" ? `求解成功（依据 v${r.dataset_version}）` : "约束无解";
    $("solveStatus").className = "msg " + (r.result.status === "sat" ? "ok" : "err");
  } catch (err) {
    $("solveStatus").textContent = err.message;
    $("solveStatus").className = "msg err";
  }
}

function previewIsStale() {
  return !lastPreview || lastPreview.version !== currentVersion;
}

function renderSolveResult() {
  if (!lastPreview) return;
  const { result } = lastPreview;
  if (result.status === "sat") {
    $("satBox").classList.remove("hidden");
    $("unsatBox").classList.add("hidden");
    const counts = result.stats.counts;
    $("solveStats").innerHTML = [
      ["连通分量数", result.stats.component_count],
      ["约束总数", result.constraint_count],
      ["训练集 标签0/1", `${counts.g1_label0} / ${counts.g1_label1}`],
      ["评估集 标签0/1", `${counts.g2_label0} / ${counts.g2_label1}`],
    ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");
    const stale = previewIsStale();
    $("previewWarn").classList.toggle("hidden", !stale);
    $("publishBtn").disabled = stale;
    renderAssignments();
  } else {
    $("satBox").classList.add("hidden");
    $("unsatBox").classList.remove("hidden");
    $("unsatSummary").textContent = result.summary;
    $("conflictTable").querySelector("tbody").innerHTML = result.conflicts
      .map((c, i) => `<tr>
        <td>${i + 1}</td>
        <td><span class="pill ${c.kind === "quota" ? "dead" : "warn"}">${escapeHtml(c.kind_name)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td><code>${escapeHtml(JSON.stringify(c.detail))}</code></td>
      </tr>`).join("");
  }
}

function renderAssignments() {
  if (!lastPreview || lastPreview.result.status !== "sat") return;
  const grpFilter = $("filterGroup").value;
  const labFilter = $("filterLabel").value;
  const locks = lastPreview.input.locks || {};
  const assigns = lastPreview.result.assignments;
  const rows = lastPreview.input.cases
    .filter((c) => (!grpFilter || String(assigns[c.case_id]) === grpFilter) &&
      (!labFilter || String(c.label) === labFilter))
    .map((c) => {
      const g = assigns[c.case_id];
      return `<tr>
        <td>${escapeHtml(c.case_id)}</td>
        <td>${escapeHtml(c.patient_id)}</td>
        <td>${c.label}</td>
        <td>${g}</td>
        <td>${g === 1 ? "训练集" : "评估集"}</td>
        <td>${c.case_id in locks ? "是 → " + (locks[c.case_id] === 1 ? "训练集" : "评估集") : "否"}</td>
      </tr>`;
    }).join("");
  $("assignTable").querySelector("tbody").innerHTML = rows;
}

// --------------------------------------------------------------------------- //
// 发布
// --------------------------------------------------------------------------- //
async function publishManifest() {
  if (!lastPreview || !lastPreview.token) return;
  setMsg("publishMsg", "发布中…");
  try {
    await api("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_token: lastPreview.token }),
    });
    setMsg("publishMsg", "清单已发布并与输入依据一起持久化", true);
    await refreshAll();
  } catch (err) {
    setMsg("publishMsg", err.message);
    if (String(err.message).includes("失效")) {
      $("publishBtn").disabled = true;
      $("previewWarn").classList.remove("hidden");
    }
  }
}

// --------------------------------------------------------------------------- //
// 已发布清单
// --------------------------------------------------------------------------- //
function renderManifestTable() {
  if (!currentState) return;
  const tbody = $("manifestTable").querySelector("tbody");
  if (!currentState.manifests.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">暂无已发布清单</td></tr>`;
    return;
  }
  tbody.innerHTML = currentState.manifests.map((m) => {
    const status = m.expired
      ? `<span class="pill dead">已永久失效${m.invalidated ? "（输入已变动）" : "（版本不匹配）"}</span>`
      : `<span class="pill ok">有效（v${m.dataset_version}）</span>`;
    return `<tr>
      <td><code>${escapeHtml(m.manifest_id.slice(0, 8))}…</code></td>
      <td>v${m.dataset_version}</td>
      <td>${escapeHtml(m.created_at)}</td>
      <td>${m.case_count ?? ""}</td>
      <td>${status}</td>
      <td>
        <button onclick="showManifest('${m.manifest_id}')">查看依据</button>
        <button ${m.expired ? "disabled" : ""} onclick="downloadManifest('${m.manifest_id}')">下载 CSV</button>
      </td></tr>`;
  }).join("");
}

async function showManifest(id) {
  try {
    const m = await api(`/api/manifests/${id}`);
    $("manifestDetail").classList.remove("hidden");
    $("manifestJson").textContent = JSON.stringify(
      { manifest_id: m.manifest_id, dataset_version: m.dataset_version,
        created_at: m.created_at, expired: m.expired,
        input_basis: m.input, split_result: m.result }, null, 2);
  } catch (err) { alert(err.message); }
}

async function downloadManifest(id) {
  const resp = await fetch(`/api/manifests/${id}/download`);
  if (!resp.ok) {
    let msg = `下载被拒绝（HTTP ${resp.status}）`;
    try { msg = (await resp.json()).detail || msg; } catch { /* ignore */ }
    alert(msg);
    await refreshAll();
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `split_manifest_${id.slice(0, 8)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// 设备偏差分析
// --------------------------------------------------------------------------- //
function renderBiasManifestSelect() {
  if (!currentState) return;
  const sel = $("biasManifest");
  const previous = sel.value;
  const manifests = currentState.manifests || [];
  const eligible = manifests.filter((m) => m.bias_eligible);
  if (!manifests.length) {
    sel.innerHTML = `<option value="">（暂无已发布清单，请先在③求解并发布）</option>`;
    sel.disabled = true;
    $("biasManifestHint").textContent = "请先发布一个清单。";
    return;
  }
  sel.disabled = false;
  sel.innerHTML = manifests.map((m) => {
    const label = `${m.manifest_id.slice(0, 8)}…（v${m.dataset_version}，`
      + `${m.case_count ?? "?"} 病例，分量 ${m.component_count ?? "?"}）`
      + (m.bias_eligible ? "" : m.expired ? "［已失效］" : "［分量超 16］");
    return `<option value="${m.manifest_id}"${m.bias_eligible ? "" : " disabled"}>`
      + `${label}</option>`;
  }).join("");
  if (previous && [...sel.options].some((o) => o.value === previous && !o.disabled)) {
    sel.value = previous;
  } else if (eligible.length) {
    sel.value = eligible[0].manifest_id;
  }
  const chosen = manifests.find((m) => m.manifest_id === sel.value);
  $("biasManifestHint").textContent = chosen
    ? (chosen.bias_eligible
      ? `该清单含 ${chosen.component_count} 个亲属连通分量，可精确穷举 ${2 ** chosen.component_count} 种以内的分量组合。`
      : (chosen.expired ? "该清单已永久失效，不能分析，请选择有效清单。"
        : `分量数 ${chosen.component_count} 超过 16，无法精确穷举。`))
    : "没有可分析的有效清单。";
}

function _fracText(f) {
  if (!f) return "";
  return `${f.numerator}/${f.denominator}`;
}

async function runDeviceBias() {
  const manifestId = $("biasManifest").value;
  if (!manifestId) return setMsg("biasMsg", "请先选择一个有效的已发布清单");
  const f = $("devicesFile").files[0];
  if (!f) return setMsg("biasMsg", "请先选择设备型号 CSV 文件");
  const fd = new FormData();
  fd.append("file", f);
  setMsg("biasMsg", "枚举合法切分中（分量 ≤ 16，精确穷举）…");
  $("biasResult").classList.add("hidden");
  try {
    const r = await api(`/api/manifests/${manifestId}/device-bias`,
      { method: "POST", body: fd });
    setMsg("biasMsg", `分析完成（清单 v${r.dataset_version}，${r.device_row_count} 个病例）`, true);
    renderBiasResult(r.result);
  } catch (err) {
    setMsg("biasMsg", err.message);
  }
}

function renderBiasResult(res) {
  $("biasResult").classList.remove("hidden");
  $("biasSingleWarn").classList.toggle("hidden", !res.single_legal_split);

  $("biasStats").innerHTML = [
    ["现值偏差（组1−组2 A占比）", _fracText(res.current_bias)],
    ["现值偏差绝对值", _fracText(res.current_bias_abs)],
    ["罕见切分数量", res.rare_count],
    ["合法切分总数", res.legal_split_total],
  ].map(([k, v]) => `<div class="card"><b>${v}</b><span>${k}</span></div>`).join("");

  const rows = [
    ["组1 A 占比", _fracText(res.device_a_proportions["1"]),
     res.device_a_proportions["1"].decimal,
     `${res.group_a_counts["1"]} / ${res.group_sizes["1"]} 个病例为 A`],
    ["组2 A 占比", _fracText(res.device_a_proportions["2"]),
     res.device_a_proportions["2"].decimal,
     `${res.group_a_counts["2"]} / ${res.group_sizes["2"]} 个病例为 A`],
    ["现值偏差（带符号）", _fracText(res.current_bias), res.current_bias.decimal,
     "组1 A占比 − 组2 A占比"],
    ["现值偏差绝对值", _fracText(res.current_bias_abs), res.current_bias_abs.decimal,
     "罕见度比较阈值：|偏差| ≥ 该值"],
    ["罕见切分数量", res.rare_count, "",
     "所有合法切分中 |偏差| 不小于现值的数量"],
    ["合法切分总数", res.legal_split_total, "",
     "满足亲属同组、四格精确配额、锁定规则的切分总数"],
    ["精确比例", _fracText(res.rare_ratio), res.rare_ratio.decimal,
      `${res.rare_count} / ${res.legal_split_total}（已约分）`],
  ].map(([k, frac, dec, note]) => `<tr><td>${k}</td><td><code>${frac}</code></td>`
    + `<td>${dec === "" ? "" : dec}</td><td class="hint">${escapeHtml(note)}</td></tr>`)
    .join("");
  $("biasTable").querySelector("tbody").innerHTML = rows;

  $("biasCompCount").textContent = res.components.length;
  $("biasCompTable").querySelector("tbody").innerHTML = res.components.map((c) => `<tr>
    <td><code>${escapeHtml(c.root)}</code></td>
    <td>${c.case_count}</td>
    <td>${c.label_counts["0"]} / ${c.label_counts["1"]}</td>
    <td>${c.device_counts.A} / ${c.device_counts.B}</td>
    <td>${c.locked_group ? `组${c.locked_group}` : "未锁定"}</td>
  </tr>`).join("");

  $("biasJudgement").textContent = "结论：" + res.judgement;
}

// --------------------------------------------------------------------------- //
// 版本日志
// --------------------------------------------------------------------------- //
async function loadVersionLog() {
  try {
    const r = await api("/api/version-log");
    $("logTable").querySelector("tbody").innerHTML = r.log.map((e) => `<tr>
      <td>v${e.dataset_version}</td>
      <td>${escapeHtml(e.change_type)}</td>
      <td><code>${escapeHtml(JSON.stringify(e.detail))}</code></td>
      <td>${escapeHtml(e.created_at)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="hint">暂无变动记录</td></tr>`;
  } catch (err) { /* 轮询场景静默 */ }
}

// --------------------------------------------------------------------------- //
// 全局刷新与后台轮询（支持两个窗口交错操作）
// --------------------------------------------------------------------------- //
async function refreshAll(showStaleBanner = false) {
  try {
    const prevVersion = currentVersion;
    currentState = await api("/api/state");
    renderState();
    $("healthBadge").textContent = "已连接";
    $("healthBadge").className = "badge";
    const changed = prevVersion !== null && prevVersion !== currentState.dataset_version;
    if (changed || showStaleBanner) {
      $("staleBanner").classList.toggle("hidden", !changed && !showStaleBanner);
      if (lastPreview && lastPreview.version !== currentVersion) {
        $("previewWarn").classList.remove("hidden");
        $("publishBtn").disabled = true;
      }
    }
    if (lastPreview) renderSolveResult();
  } catch (err) {
    $("healthBadge").textContent = "连接中断";
    $("healthBadge").className = "badge down";
  }
}

setInterval(refreshAll, 4000);
setInterval(() => {
  if ($("tab-log").classList.contains("active")) loadVersionLog();
}, 5000);

refreshAll();
