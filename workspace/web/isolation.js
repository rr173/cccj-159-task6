// 已发行版本事故隔离操作页（零依赖原生 JS）
const $ = (id) => document.getElementById(id);

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = msg;
  $("toast").appendChild(t);
  setTimeout(() => t.remove(), 8000);
}
async function api(path, method = "GET", body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.message), { resp: j });
  return j;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtT(isoS) { return isoS ? new Date(isoS).toLocaleString() : ""; }
function uuid() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "rk-" + Date.now() + "-" + Math.random().toString(16).slice(2, 10);
}
function genRk() { $("rk").value = uuid(); }

let EVIDENCE = [];
function addEvidence() {
  const kind = $("evKind").value.trim(), ref = $("evRef").value.trim();
  const note = $("evNote").value.trim();
  if (!kind || !ref) { toast("证据类型与引用必填", "err"); return; }
  EVIDENCE.push({ kind, ref, ...(note ? { note } : {}) });
  $("evKind").value = ""; $("evRef").value = ""; $("evNote").value = "";
  renderEvidence();
}
function removeEvidence(i) { EVIDENCE.splice(i, 1); renderEvidence(); }
function renderEvidence() {
  $("evidence").innerHTML = EVIDENCE.length
    ? "<table><tr><th>类型</th><th>引用</th><th>说明</th><th></th></tr>" +
      EVIDENCE.map((e, i) => `<tr><td>${esc(e.kind)}</td><td class="mono">${esc(e.ref)}</td>
        <td>${esc(e.note || "")}</td><td><button class="sec" onclick="removeEvidence(${i})">移除</button></td></tr>`)
        .join("") + "</table>"
    : `<span class="mut">尚无证据。</span>`;
}

async function registerIncident() {
  const body = {
    batch: $("batch").value.trim(),
    faulty_baseline_id: $("faulty").value,
    reason: $("reason").value.trim(),
    evidence: EVIDENCE,
    registered_by: $("qa").value.trim(),
  };
  const rk = $("rk").value.trim();
  if (rk) body.request_key = rk;
  try {
    const d = await api("/api/isolation/incidents", "POST", body);
    if (d.deduped) {
      toast(`<b>幂等回放</b>：相同事故请求重复到达，未产生第二次状态迁移。`, "ok");
    } else {
      const q = d.quarantine.detail;
      toast(`<b>隔离已提交</b>（代次 ${d.quarantine.generation}）：命中 ${esc(q.affected_baselines.join(", "))}；`
        + `重对齐 ${q.bounced.length} 个在验变更，熔断 ${q.aborted_releases.length} 个进行中发行。`, "ok");
    }
    $("reason").value = ""; EVIDENCE = []; renderEvidence();
    await refresh();
  } catch (e) {
    const c = e.resp?.detail || {};
    toast(`<b class="bad">隔离失败：${esc(e.resp?.error || "ERROR")}</b><br/>${esc(e.message)}`
      + (c.realign_to ? `<br/>建议重新对齐 → <b>${esc(c.realign_to)}</b>` : ""), "err");
  }
}

async function restoreTrust() {
  const body = {
    faulty_baseline_id: $("restoreTarget").value,
    confirms: [
      { role: "QUALITY_LEAD", user: $("confQa").value.trim() },
      { role: "RELEASE_LEAD", user: $("confRel").value.trim() },
    ],
    note: $("restoreNote").value.trim(),
  };
  try {
    const d = await api("/api/isolation/restore", "POST", body);
    if (d.already_trusted) {
      toast(`<b>幂等回放</b>：该谱系当前已可信，未产生第二次迁移。`, "ok");
    } else {
      toast(`<b>可信已恢复</b>（新隔离代次 ${d.quarantine.generation}）：`
        + `旧检查结果与旧签核不得跨代次复用，须在新代次重新验证签核。`, "ok");
    }
    await refresh();
  } catch (e) {
    const c = e.resp?.detail || {};
    toast(`<b class="bad">恢复失败：${esc(e.resp?.error || "ERROR")}</b><br/>${esc(e.message)}`
      + (c.fork_baseline_id ? `<br/>检测到分叉基线 <b>${esc(c.fork_baseline_id)}</b>，`
        + `应以修复分支 <b>${esc(c.trusted_anchor)}</b> 为准重建。` : ""), "err");
  }
}

function trustTag(t) {
  if (!t) return `<span class="mut">无</span>`;
  const cls = t.status === "TRUSTED" ? "t-TRUSTED" : "t-UNTRUSTED";
  return `<span class="tag ${cls}">${t.status === "TRUSTED" ? "可信" : "不可信"}</span>`;
}

function renderTree(b) {
  // 以 B-R0（无 parent）为根渲染谱系树
  const byId = Object.fromEntries(b.lineage.map(n => [n.id, n]));
  const roots = b.lineage.filter(n => !n.parent_id);
  function node(n, depth) {
    const forkMark = n.children.length > 1 ? ` <span class="warn">⤴${n.children.length} 分叉</span>` : "";
    const inc = n.trust.incident_id ? ` <span class="mut mono">${esc(n.trust.incident_id)}</span>` : "";
    const pad = `<div class="indent">`;
    return `<div class="node" style="margin-left:${depth * 4}px">`
      + `<span class="mono">${esc(n.id)}</span> ${trustTag(n.trust)}`
      + ` <span class="mut">rev${n.revision}${n.eco_id ? " · " + esc(n.eco_id) : ""}</span>`
      + inc + forkMark
      + (n.nearest_trusted_ancestor && n.trust.status !== "TRUSTED"
        ? ` <span class="mut">→ 可信锚点 <b class="ok">${esc(n.nearest_trusted_ancestor)}</b></span>` : "")
      + `</div>`
      + n.children.map(cid => byId[cid] ? pad + node(byId[cid], depth + 1) + "</div>" : "").join("");
  }
  $("tree").innerHTML = roots.map(r => node(r, 0)).join("") || `<span class="mut">暂无基线</span>`;
}

function renderAnchors(b) {
  const rows = b.lineage.filter(n => n.trust.status !== "TRUSTED").map(n => `
    <tr><td class="mono">${esc(n.id)}</td>
        <td>${trustTag(n.trust)}</td>
        <td>${n.nearest_trusted_ancestor
          ? `<b class="ok mono">${esc(n.nearest_trusted_ancestor)}</b>`
          : `<span class="bad">无可信祖先（需在谱系外重建）</span>`}</td></tr>`).join("");
  $("anchors").innerHTML = rows
    ? `<table><tr><th>不可信版本</th><th>当前状态</th><th>最近可信祖先</th></tr>${rows}</table>`
    : `<span class="ok">当前全部已发行版本均可信。</span>`;
}

async function realign(ecoId, target) {
  if (!target) { toast("没有可重新对齐的可信祖先", "err"); return; }
  try {
    await api(`/api/ecos/${ecoId}/realign`, "POST", { target_baseline_id: target, by: "ops" });
    toast(`<b>${esc(ecoId)}</b> 已重新对齐到 <b>${esc(target)}</b>，请重新冻结、重跑、重签。`, "ok");
    await refresh();
  } catch (e) {
    toast(`<b class="bad">${esc(e.resp?.error || "ERROR")}</b><br/>${esc(e.message)}`, "err");
  }
}

function renderPending(b) {
  const ps = b.pending_changes;
  if (!ps.length) { $("pending").innerHTML = `<span class="ok">没有待处理变更。</span>`; return; }
  $("pending").innerHTML = "<table><tr><th>ECO</th><th>标题</th><th>状态</th>"
    + "<th>原父版本</th><th>可信锚点</th><th></th></tr>"
    + ps.map(p => `<tr>
        <td class="mono">${esc(p.eco_id)}</td><td>${esc(p.title)}</td>
        <td><span class="tag s-QUARANTINED">${esc(p.status)}</span></td>
        <td class="mono">${esc(p.parent_baseline_id || "—")}</td>
        <td class="mono">${p.nearest_trusted_ancestor
          ? `<b class="ok">${esc(p.nearest_trusted_ancestor)}</b>` : `<span class="bad">无</span>`}</td>
        <td><button class="sec" onclick="realign('${esc(p.eco_id)}','${esc(p.nearest_trusted_ancestor || "")}')">
          重新对齐</button></td></tr>`).join("") + "</table>";
}

function renderIncidents(b) {
  const qstate = (q) => `<span class="tag s-${esc(q.state)}">${esc(q.state)}</span>`;
  const inc = b.incidents.map(i => {
    const qs = b.quarantines.filter(q => q.incident_id === i.id);
    return `<tr><td class="mono">${esc(i.id)}</td>
      <td>${esc(i.batch)}</td><td class="mono">${esc(i.faulty_baseline_id)}</td>
      <td>${esc(i.reason)}</td>
      <td>${i.evidence.map(e => `<span class="chip">${esc(e.kind)}:${esc(e.ref)}</span>`).join(" ")}</td>
      <td>${qs.map(qstate).join(" ") || ""}</td>
      <td class="mut">${esc(i.registered_by)}</td></tr>`;
  }).join("");
  $("incidents").innerHTML = inc
    ? `<table><tr><th>事故</th><th>批次</th><th>命中版本</th><th>原因</th><th>证据</th>
        <th>裁决</th><th>登记人</th></tr>${inc}</table>`
    : `<span class="mut">尚无事故。</span>`;
}

const AUDIT_CN = {
  QUARANTINE_PHASE1_LOCKED: "隔离阶段1已锁定",
  QUARANTINE_PHASE2_COMMITTED: "隔离已提交",
  QUARANTINE_RECOVERED: "崩溃后续裁完成",
  RESTORE_PHASE1_LOCKED: "恢复阶段1已锁定",
  RESTORE_PHASE2_COMMITTED: "可信已恢复",
  RESTORE_RECOVERED: "恢复续裁完成",
  RECOVER_BLOCKED: "恢复被阻断",
};
function renderAudit(b) {
  $("audit").innerHTML = b.audit.length
    ? "<table><tr><th>#</th><th>裁决</th><th>事件</th><th>时间</th></tr>"
      + b.audit.slice(0, 40).map(a => `<tr><td>${a.seq}</td><td class="mono">${esc(a.quarantine_id)}</td>
        <td>${esc(AUDIT_CN[a.event] || a.event)}</td><td class="mut">${fmtT(a.at)}</td></tr>`).join("")
      + "</table>"
    : `<span class="mut">尚无隔离审计。</span>`;
}

async function refresh() {
  const b = await api("/api/isolation/board");
  $("meta").textContent = `更新于 ${fmtT(b.at)}`;
  $("gen").textContent = b.isolation_generation;
  $("trunk").textContent = b.current_trunk.base_id;
  $("trunkTrust").innerHTML = trustTag(b.current_trunk.trust);

  const trusted = b.lineage.filter(n => n.trust.status === "TRUSTED").map(n => n.id);
  const untrusted = b.lineage.filter(n => n.trust.status !== "TRUSTED").map(n => n.id);
  for (const id of ["faulty", "restoreTarget"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = b.lineage.map(n => `<option value="${esc(n.id)}">${esc(n.id)}</option>`).join("");
    if (cur && b.lineage.some(n => n.id === cur)) sel.value = cur;
  }
  renderTree(b);
  renderAnchors(b);
  renderPending(b);
  renderIncidents(b);
  renderAudit(b);
}

let timer = null;
$("autoref").addEventListener("change", () => {
  clearInterval(timer);
  if ($("autoref").checked) timer = setInterval(refresh, 5000);
});
timer = setInterval(refresh, 5000);
renderEvidence();
refresh().catch(e => toast(esc(e.message), "err"));
