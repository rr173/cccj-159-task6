# API 契约

基础路径 `http://<host>:8080`，全部 JSON。错误响应统一为：

```json
{ "error": "CODE", "message": "可读说明", "detail": { "reason": "..." } }
```

## 状态与原因码

- **ECO 状态**：`DRAFT / FROZEN / QUEUED / REBASE_REQUIRED / RELEASING / RELEASED / MERGED / ABORTED`
- **检查状态**：`PENDING / BLOCKED / PASS / FAIL / SUPERSEDED`（门洁面可能显示 `MISSING`）
- **拒收原因 reason**：
  - `FINGERPRINT_MISMATCH` 结果指纹与冻结输入不符（拿错基线/输入被改）
  - `STALE_SUPERSEDED` run 已因纪元变化作废，结果迟到
  - `ALREADY_SUBMITTED` 同一 run 重复提交
  - `DEPENDENCY_BLOCKED` 依赖前置尚未全部 PASS
  - `ECO_NOT_ACTIVE` ECO 不在 FROZEN 态
  - `BASE_CONSTRAINT_CHANGED` 基础约束更新导致在验单整体作废（系统事件留痕）

### 已发行版本事故隔离（信任熔断 / 恢复）

- **版本信任态**：`TRUSTED / UNTRUSTED`。命中版本及其全部后代（沿 `parent_id` 闭包）
  在隔离提交时标 `UNTRUSTED`；信任覆盖 `baseline_trust` 不可变，每代次一行，
  无覆盖的基线沿父链继承（`B-R0` 恒可信）。
- **隔离代次 isolation_generation**：每次隔离/恢复提交 +1 并进入冻结快照指纹
  （`snapshot.trust_context`）；旧检查结果与旧签核绑定其代次，**不跨代次复用**。
- **裁决状态**：`QUARANTINING → QUARANTINED`、`RESTORING → RESTORED`；
  被隔离熔断的发行 `release.state=FAILED`。
- 拒收原因 reason：
  - `PARENT_BASELINE_QUARANTINED` 冻结时父版本已不可信（响应带 `realign_to` 最近可信祖先）
  - `LINEAGE_QUARANTINED` 发行时父谱系不可信 / 纵深防御
  - `ISOLATION_IN_PROGRESS` 谱系上有进行中的隔离/恢复裁决，发行需等待
  - `RELEASE_ABORTED_BY_QUARANTINE` 进行中发行被并发隔离 fail-closed 熔断（ECO 转待重新对齐）
  - `RELEASE_ABORTED` 被熔断的发布不可 resume
  - `REALIGN_TARGET_UNTRUSTED` 重新对齐目标不是当前可信祖先
  - `EVIDENCE_REQUIRED` / `BAD_EVIDENCE` 事故缺少合规证据（每条须 `{kind, ref, note?}`）
  - `DUAL_ROLE_CONFIRM_REQUIRED` 恢复缺少质量/发行双角色确认
  - `DUAL_ROLE_CONFLICT` 恢复的两个确认角色为同一自然人
  - `LINEAGE_FORKED_SINCE_QUARANTINE` 隔离期间可信祖先上出现修复分叉（带
    `fork_baseline_id / trusted_anchor / quarantined_at / forked_at`），禁止朴素恢复
  - `LINEAGE_ALREADY_QUARANTINED` 命中版本已不可信且无对应已登记事故
  - `RESTORE_TARGET_MUST_BE_ROOT` 恢复必须针对事故命中根版本（整条谱系一并恢复）
  - `QUARANTINE_INTERRUPTED` / `RESTORE_INTERRUPTED` 阶段1后崩溃（failpoint 注入，可续裁）
- **幂等**：登记携带 `request_key`（缺省由请求内容指纹派生），重复到达返回
  200 `{deduped:true}`，不产生第二次状态迁移；恢复在已可信谱系上同样幂等。
- **崩溃续裁**：启动自动 `recover()`，把 `QUARANTINING/RESTORING` 续裁完成；
  恢复续裁时若已分叉则保持中断并记 `RECOVER_BLOCKED`。

### 电路影响闭包（审核前自动复验分析）

影响闭包基于**版本化电路拓扑**（模块实例、传播边、时钟域、约束引用、强制策略）和
变更起点计算，输出必须复验的实例/模块集合、检查类型与每条传播边解释。

- 拓扑版本：`version/fingerprint/status`，相同拓扑内容重复发布幂等；新版本会冻结旧闭包，
  并把已 FROZEN ECO 转为 `REBASE_REQUIRED`，旧检查 SUPERSEDED、旧签核失效。
- 闭包快照：`revision=1` 是自动结论；人工排除必须填写理由并追加 `revision=n`，旧快照保留为
  `SUPERSEDED`。相同 `(scope, roots, change_summary, topology_version)` 重放返回原快照。
- 当前指针：`impact_latest(scope,input_fingerprint)` 只指向一个当前快照；并发重算/覆盖
  由唯一约束和 `BEGIN IMMEDIATE` 保证至多一个当前版本。
- **不可排除节点**：变更起点、跨时钟边后继、接口契约边后继、约束引用节点、策略强制节点。
  排除普通节点若导致强制节点路径被切断，返回 `MANUAL_EXCLUSION_CONFLICT`。
- 可操作错误：
  - `TOPOLOGY_CYCLE_DETECTED`：返回 `cycle` 与断环建议。
  - `TOPOLOGY_MISSING_NODE` / `CHANGE_ROOT_NOT_FOUND`：返回缺失节点和修正动作。
  - `IMPACT_CLOSURE_TOO_LARGE`：返回闭包大小/上限和拆分建议。
  - `MANDATORY_EXCLUSION_FORBIDDEN`：返回强制原因。
  - `MANUAL_EXCLUSION_CONFLICT`：返回被切断的强制节点、完整路径和阻断排除项。
  - `EXCLUSION_REASON_REQUIRED` / `AMBIGUOUS_MODULE_INSTANCE`。
  - `IMPACT_REQUIRED` / `IMPACT_SNAPSHOT_STALE` / `IMPACT_SNAPSHOT_NOT_CURRENT`：
    旧闭包不能冻结或发布。
- 冻结快照 `snapshot.impact_closure` 包含闭包 ID、拓扑版本/指纹、变更摘要、复验集合、
  检查类型、边解释和排除记录；整体进入 ECO 快照指纹。

### 验证执行租约（Runner 协调层）

与 ECO 服务配套的租约层负责“谁在哪一代次可以提交哪个检查”，**不改变** ECO 侧门禁：
租约凭据有效只代表“有权投递”，结果仍由原 `/results` 同一套裁决落库（指纹 / 纪元 /
依赖 / 重跑 / 签核 / 发布），接管不可能绕过任何一项。

- 租约字段：`state=HELD/COMPLETED/RELEASED/EXPIRED/INVALIDATED`、`gen`（租约代次，
  每次接管/超时重领 +1）、`runner_id`、`acquired_at/expires_at/remaining_seconds`。
- **凭据**：CLAIM/TAKEOVER 返回一次性 `lease_token`（16hex，
  `HMAC-SHA256(server_secret, run_id:gen:runner_id)`），服务端密钥落盘 `lease_meta`，
  库内只存 SHA-256 摘要；完成/续租必须携带 `gen + lease_token`。
- **领取幂等**：相同 `idempotency_key` 重放返回首次授权（HTTP 200、`replayed:true`）；
  幂等键被其他 runner/任务占用 → 409 `IDEMPOTENCY_KEY_REUSED`；并发领取同一任务只有一个
  成功，其余 409 `LEASE_HELD_BY_OTHER`。
- 租约层拒收原因：`STALE_LEASE_GEN`（代次过期：已被接管/重领，旧持有者迟到结果）、
  `LEASE_BAD_CREDENTIAL`（凭据不符/伪造，403）、`LEASE_EXPIRED`（TTL 过期）、
  `LEASE_NOT_EXPIRED`（未到期不可强制接管）、`LEASE_NOT_HELD_BY_RUNNER`（非持有者）、
  `LEASE_ALREADY_COMPLETED` / `LEASE_RELEASED`、`LEASE_NOT_FOUND`、
  `DEPENDENCY_NOT_READY`（依赖前置未 PASS，任务不在待领池）、`NO_CLAIMABLE_TASK`、
  `RUN_SUPERSEDED`；指纹/依赖等 ECO 门禁拒绝时**透传 ECO 原始原因码**
  （`FINGERPRINT_MISMATCH` / `STALE_SUPERSEDED` / `DEPENDENCY_BLOCKED` …）。
- 所有拒绝同时写入租约审计 `lease_event(event=REJECT)`（可按 run/事件/原因查询）
  与 ECO 侧 `rejected_result`。
- 进程重启：未过期租约恢复为 HELD（密钥落盘，旧凭据继续可用），已过期租约回收为
  EXPIRED 可接管；运行期每次租约操作还会惰性对账（超时回收、run 已结束/作废对账）。
- 操作页：`GET /ops`（待领任务、当前持有者与剩余时间、可接管池、接管历史、拒绝审计）。

### 临时代理签核授权

管理员可为两种签核角色（`TIMING_LEAD / METHODOLOGY_LEAD`）配置**临时代理人**及其
生效/失效时间窗，并可随时撤销。服务端在**签核动作发生的那一刻**重新校验授权
（而不是配置时或冻结时）：

- 未生效（当前时刻 < `valid_from`）→ `403 DELEGATION_NOT_STARTED`
- 已过期（当前时刻 ≥ `valid_until`，半开区间）→ `403 DELEGATION_EXPIRED`
- 已撤销（`status=REVOKED`）→ `403 DELEGATION_REVOKED`
- 授权角色与签核角色不符 → `403 DELEGATION_ROLE_MISMATCH`
- 授权代理人与操作人不符 → `403 DELEGATION_USER_MISMATCH`
- 授权记录不存在 → `404 DELEGATION_NOT_FOUND`

所有拒绝写入 `audit_log`（`signoff.denied`）。**同一自然人仍不得完成两个角色签核**，
直签与代理签合并判定（`409 DUAL_ROLE_CONFLICT`）。

代理签核成功后，签核记录固化当时的身份与代理链快照
（`signoff_kind=DELEGATE`、`delegation_id`、`auth_chain`：授权人、被代理负责人、
有效期、动作校验时刻），记录长期保留供审计；ECO 重新冻结（rebase / 合并 / 新基线发布）
仍使旧签核失效（标记 `voided_at`），新纪元须重新签核。

## 端点

### 基线与目录
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/baselines` | 发布基线列表 |
| GET | `/api/baselines/{id}` | 基线完整不可变快照 |
| GET | `/api/baselines/diff?from=&to=` | 结构化差异包 |
| GET | `/api/baselines/diff.zip?from=&to=` | ZIP 差异包（before/after/diff/矩阵/结果/签核）|
| GET | `/api/catalog` | 验证矩阵目录（必需性、依赖、指纹作用域）|
| GET | `/api/constraints` | 当前发布基线与工作约束版本 |
| GET | `/api/workbench` | 工作台聚合视图（含代理配置与当前可签人员）|
| GET | `/api/current-signers` | 每种角色当前可签代理（来源、有效期）|
| GET | `/api/admin/delegations` | 全部代理配置（含待生效/已过期/已撤销及实时状态）|
| GET | `/api/rejected-results` | 全部拒收/作废留痕 |

### ECO
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/ecos` | `{title, owner, affected_modules[], edits, root_instances?, change_summary?, compute_impact?}` → 201；默认创建时自动计算影响闭包 |
| POST | `/api/ecos/{id}/edits` | `{edits, by}` 仅 DRAFT/REBASE_REQUIRED；编辑内容变化会重算当前闭包 |
| POST | `/api/ecos/{id}/freeze` | `{by}` 先校验影响闭包仍为当前版本；冲突时转 QUEUED，否则冻结+触发矩阵；父版本不可信报 `PARENT_BASELINE_QUARANTINED`（带 `realign_to`）|
| POST | `/api/ecos/{id}/abort` | `{by}` 释放锁并推动排队单晋升 |
| POST | `/api/ecos/{id}/realign` | `{target_baseline_id, by}` 隔离后重新对齐到可信祖先；目标不可信 409 `REALIGN_TARGET_UNTRUSTED`；QUEUED 单转回 REBASE_REQUIRED |
| GET | `/api/ecos` / `/api/ecos/{id}` | 列表（含门禁/闭包摘要）/ 详情（含 gate/queue/release/impact）|
| GET | `/api/ecos/{id}/impact` | ECO 绑定的影响闭包快照（候选、边解释、人工排除）|

### 电路影响闭包
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| GET | `/api/impact/topology` | 当前版本化拓扑（nodes/edges/clock_domains/constraint_refs/policies）|
| GET | `/api/impact/topologies` | 拓扑版本列表 |
| POST | `/api/impact/topology` | `{graph, by, note}`；校验环/缺失节点/重复节点后发布新版本 |
| POST | `/api/impact/compute` | `{scope, roots[], change_summary, by}`；相同输入返回原快照 |
| GET | `/api/impact/snapshots/{id}` | 不可变闭包详情；`usable_for_review` 表示可冻结/发布 |
| POST | `/api/impact/snapshots/{id}/exclusions` | `{by, exclusions:[{node, reason}]}`；追加人工覆盖版本 |
| GET | `/api/impact/audit?scope=&snapshot_id=&limit=` | `impact.auto/impact.override/topology.publish` 审计 |
| 页面 | `/impact` | 拓扑发布、闭包计算、边解释、人工排除和审计操作页 |

### 已发行版本事故隔离
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/isolation/incidents` | `{batch, faulty_baseline_id, reason, evidence:[{kind,ref,note?}], registered_by, request_key?, failpoint?}`；201 新裁决 / 200 `deduped:true` 回放。命中版本及后代标不可信、关联在验 ECO 转 REBASE_REQUIRED、熔断同谱系 RELEASING 发行 |
| POST | `/api/isolation/restore` | `{faulty_baseline_id, confirms:[{role:QUALITY_LEAD,user},{role:RELEASE_LEAD,user}], restored_by?, request_key?, note?, failpoint?}`；双角色须不同自然人，期间无分叉，成功产生新隔离代次 |
| POST | `/api/isolation/quarantines/{id}/resume` | `{by?}` 手动续裁中断的隔离/恢复（服务启动已自动 recover）|
| GET | `/api/isolation/board` | 操作页聚合：`isolation_generation / lineage(含 trust 与 nearest_trusted_ancestor) / untrusted_lineage / current_trunk / pending_changes / incidents / quarantines / open_adjudications / aborted_releases / audit / restore_roles` |
| GET | `/api/isolation/incidents` / `/incidents/{id}` | 事故列表 / 详情（含其隔离与恢复裁决及双角色确认）|
| GET | `/api/isolation/quarantines` | 全部两阶段裁决及状态 |
| GET | `/api/isolation/audit?incident_id=&quarantine_id=&event=&limit=` | 落盘日志：`QUARANTINE_PHASE1_LOCKED/QUARANTINE_PHASE2_COMMITTED/QUARANTINE_RECOVERED/RESTORE_*/RECOVER_BLOCKED` |
| 页面 | `/isolation` | 事故登记、受影响谱系树、可信祖先、待处理变更、双角色恢复、隔离审计 |

事故隔离与新发行并发的裁决时序（同一谱系）：

```
release 阶段1 已提交 RELEASING（可崩溃） ──┐
                                          ├─ 单事务 BEGIN IMMEDIATE 原子相遇
quarantine 阶段1 QUARANTINING 落盘 ───────┘
  → 隔离阶段2：该 RELEASING 单 fail-closed 熔断为 FAILED，ECO 转 REBASE_REQUIRED，
    preferred_parent=最近可信祖先；resume 报 RELEASE_ABORTED 不救活。
  → 若隔离仍在 QUARANTINING 未提交：release 阶段2 报 ISOLATION_IN_PROGRESS。
崩溃在任一阶段1之后：启动 recover() 从 isolation_journal 续裁，结果确定且无部分态。
```

### 验证（Runner 协议）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ecos/{id}/checks/{type}/inputs` | 拉冻结输入包（含 expected_fingerprint 供核对）|
| POST | `/api/ecos/{id}/checks/{type}/results` | `{run_id, fingerprint, verdict:PASS|FAIL, result, runner_id}` |
| POST | `/api/ecos/{id}/checks/{type}/rerun` | `{by}` 仅 FAIL 可重跑；旧尝试 SUPERSEDED，新 run 绑当前指纹 |

Runner **必须**对 `inputs` 自行按作用域计算 SHA-256 摘要（紧凑规范 JSON：
键排序、无空白、`ensure_ascii`），服务端不接受缺失/不符指纹。

### 验证执行租约（Runner 协调协议）
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/leases/claims` | `{runner_id, run_id?, idempotency_key?, ttl_seconds?}`；不给 `run_id` 则原子挑一个依赖已就绪任务；成功 201，幂等重放 200。返回 `{gen, lease_token, expires_at, remaining_seconds, taken_over?, previous_holder?}` |
| POST | `/api/leases/runs/{run_id}/renew` | `{gen, lease_token, runner_id, ttl_seconds?}` 续租；旧代次/过期/非持有者拒绝 |
| POST | `/api/leases/runs/{run_id}/complete` | `{gen, lease_token, fingerprint, verdict, result?, runner_id?}` 代次+凭据校验与 ECO 指纹/依赖门禁**同一事务**裁决；拒绝原因透传 ECO 原码 |
| POST | `/api/leases/runs/{run_id}/takeover` | `{runner_id, ttl_seconds?}` 仅 EXPIRED 可接管；gen+1，旧持有者凭据立即失效 |
| POST | `/api/leases/runs/{run_id}/release` | `{gen, lease_token, runner_id, note?}` 持有者主动释放，任务回到待领池 |
| GET | `/api/leases/board` | 看板：`claimable / held(剩余秒) / takeover_available / takeover_history / recent_rejects / all_leases` |
| GET | `/api/leases/runs/{run_id}` | 单条租约状态与完整事件流 |
| GET | `/api/leases/audit?run_id=&event=&reason=&limit=` | 租约可查询审计（CLAIM/RENEW/COMPLETE/RELEASE/EXPIRE/TAKEOVER/INVALIDATE/REJECT/RECOVER）|

`ttl_seconds` 取值 30~3600，默认 600（可用 `--lease-ttl` 调整）。典型 Runner 时序：

```
POST /claims            → gen=1, lease_token（仅本次返回，客户端保存）
GET  .../inputs → 自算指纹
POST .../complete       → {gen:1, lease_token, fingerprint, verdict}
（任务卡住超过 TTL）其他 runner: POST .../takeover → gen=2, 新 token
旧 runner 用 gen=1+旧 token 迟到提交 → 409 STALE_LEASE_GEN（lease_event 留痕）
```

### 签核与发布
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ecos/{id}/signoff` | `{role, user, delegation_id?}` 门禁红时 409；同一人双签 409；带 `delegation_id` 时代理未生效/过期/撤销/角色或人不符 403/404 |
| POST | `/api/ecos/{id}/release` | `{by, failpoint?}` 门禁/双签不满足 409 且状态不变；`failpoint=AFTER_PHASE1` 注入崩溃 |
| POST | `/api/ecos/{id}/release/resume` | 从中断点幂等恢复 |

### 临时代理签核（管理员）
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/admin/delegations` | `{role, delegate_user, valid_from, valid_until, granted_by, principal_user?, note?}`；时间为 UTC ISO-8601。**相同角色+代理人+完全相同时间窗重复提交幂等**：返回既有记录（200，`deduped:true`），不生成重复有效记录；与该代理人该角色另一有效记录时间窗重叠 → 409 `DELEGATION_OVERLAP` |
| POST | `/api/admin/delegations/{id}/revoke` | `{revoked_by?}`（或 `by`）立即撤销；重复撤销幂等 |

签核记录字段：`signoff_kind=DIRECT|DELEGATE`、`delegation_id`、`auth_chain`（代理链快照）、
`voided_at/voided_reason`（被重新冻结等原因失效，记录保留）。
`GET /api/workbench` 额外包含 `delegations`（含实时 `state: PENDING/ACTIVE/EXPIRED/REVOKED`）
与 `current_signers`；工作台页面据此展示**当前可签人员、代理来源与有效期**。

### 冲突与基础约束
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ecos/{src}/merge-into/{tgt}` | `{by, module_winners:{m:TARGET_WINS|SOURCE_WINS}}`；src 须 QUEUED、tgt 须 FROZEN 且确有未决阻塞；合并后 tgt 纪元+1 整轮重跑 |
| POST | `/api/constraints` | `{delta:{...}, by, note}` 存在 RELEASING 单时 409；否则在验 FROZEN 单全部转 REBASE_REQUIRED |
| POST | `/api/admin/reset` | 重置为种子数据 |

## 独立设计签名封装 API

基础路径同主机，默认端口 `8090`，页面 `GET /envelopes`，API 前缀 `/api/envelope`。

### 密钥与签名者

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/envelope/keys/generate | `{}` → 一次性返回 `{kid, public_key, private_seed}` |
| POST | `/api/envelope/signers` | `{kid, signer, role, public_key, replaces_kid?}`；轮换会同时写旧 key 的 `REVOKE` 注册表事件 |
| POST | `/api/envelope/signers/{kid}/revoke` | `{reason?, by?}`；撤权后历史签名按 `signed_at` 验真，新签名拒绝 |
| GET | `/api/envelope/signers` | 当前每个 kid 最新注册表事件 |
| GET | `/api/envelope/root` | 注册表根公钥 |
| POST | `/api/envelope/root/rotate` | `{by?}`；新旧根交叉签名 ROTATE 事件，旧根历史可沿根链离线追溯 |

### 会话、构件与分块续传

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/envelope/sessions` | `{by, required_roles?, parent_envelope_id?, metadata?, session_id?}` |
| GET | `/api/envelope/sessions` / `.../sessions/{id}` | 会话、构件、上传、签名状态 |
| POST | `/api/envelope/sessions/{id}/artifacts` | `{slot, kind, content, detail?}`；kind 为 `netlist/constraints/modules/signoffs` |
| POST | `/api/envelope/sessions/{id}/uploads` | `{slot,kind,chunk_size,total_size}` 初始化分块上传 |
| POST | `/api/envelope/uploads/{id}/chunks/{index}` | 原始 body；可用 `X-Chunk-Hash` 声明 SHA-256。重复块幂等，同序号不同内容 409 |
| POST | `/api/envelope/uploads/{id}/chunks` | JSON：`{index,data_hex,chunk_hash?}`，便于小文件/测试 |
| GET | `/api/envelope/sessions/{sid}/uploads/{id}` | 返回已收块、`missing_indexes`、`received_size`，用于断点续传 |
| POST | `/api/envelope/uploads/{id}/complete` | 校验所有块并拼接为内容寻址 blob，再挂到构件槽位 |
| POST | `/api/envelope/sessions/{id}/prepare` | 计算 `content_hash/envelope_id`、分块叶哈希和 Merkle root；同内容并发只生成一个信封 |

### 签名、封口、发布与验真

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| GET | `/api/envelope/sessions/{id}/challenge/{kid}` | 获取固定签名挑战 `{schema,envelope_id,kid,signed_at}` |
| POST | `/api/envelope/sessions/{id}/signatures` | `{signed_payload:challenge, signature}`；服务端验签时并入 content/merkle 上下文 |
| POST | `/api/envelope/sessions/{id}/seal` | 校验全部必需角色、至少两个自然人、签名和内容哈希 |
| POST | `/api/envelope/sessions/{id}/publish` | `{name,by}`；先写 PUBLISHING，再原子落发布收据，同名不同内容 409 `NAME_CONTENT_CONFLICT` |
| GET | `/api/envelope/envelopes` / `.../{id}` | 信封列表/详情 |
| GET | `/api/envelope/envelopes/{id}/proofs` | 构件、分块、签名全部 Merkle 包含证明 |
| GET | `/api/envelope/envelopes/{id}/verify` | 在线验真内容、注册表、签名、Merkle root |
| GET | `/api/envelope/envelopes/{id}/bundle` | 下载可离线验真 ZIP |
| POST | `/api/envelope/verify-offline` | `{bundle_hex, trusted_root_kid?}` 验证 ZIP 内容 |
| GET | `/api/envelope/lineage?name=或envelope_id=` | 父信封链与发布名 |
| GET | `/api/envelope/publications` / `/audit` | 发布列表与篡改/拒绝审计 |

ZIP 内固定包含 `MANIFEST.json`、`SIGNATURES.json`、`TRUST.json`、`PROOFS.json`
和 `artifacts/*`。离线验真会重算每个 artifact 与分块 SHA-256，校验注册表根签名、
签名发生时密钥是否有效、签名角色/人是否匹配，以及最终 Merkle root；任一项失败返回
`OFFLINE_VERIFICATION_FAILED.detail.reasons`。

## 典型时序（一次合规发布）

```
create（自动计算影响闭包，设计者可附理由排除可选节点）
  → freeze（校验闭包/拓扑仍当前）→ (Runner: inputs→自算指纹→results) ×依赖图
       → TIMING_LEAD signoff → METHODOLOGY_LEAD signoff(不同人)
       → release(阶段1锁状态 → 阶段2原子写 B-R{n}、推进 trunk)
```

## 典型时序（迟到结果）

```
ECO FROZEN 全绿 → POST /api/constraints 收紧时钟
  → ECO=REBASE_REQUIRED, 旧 run 全 SUPERSEDED, 旧签核失效
迟到 Runner 回传旧 run_id → 409 STALE_SUPERSEDED（rejected_result 留痕）
工程师 freeze(refreeze, epoch=2) → 矩阵重跑 → 双签 → 才能发布
```
