"""已发行版本事故隔离服务：信任熔断、谱系裁决、落盘恢复与双角色恢复。

与 ECOService 的关系
--------------------
本服务只负责“信任代次 + 两阶段隔离/恢复裁决 + 谱系原子并发 + 幂等 + 恢复”，
所有冻结指纹 / 纪元 / 依赖 / 双签门禁仍由 ECOService 裁决。隔离提交时通过
ECOService 的冻结指纹与纪元机制（freeze_epoch + 快照内嵌隔离代次）保证：

  任何旧检查结果、旧签核都绑定其产生代次，不可能跨隔离代次复用。

关键不变量
----------
1. 质量负责人登记事故（故障批次/命中版本/原因/证据）后，命中版本及其**全部后代**
   标 UNTRUSTED；它们禁止继续充当新变更父版本（freeze/release 门禁拒绝），
   尚未发行的关联 ECO 转回 REBASE_REQUIRED（待重新对齐可信祖先）。
2. 隔离与新发行并发：同一谱系在单事务（BEGIN IMMEDIATE）内原子裁决；
   隔离 fail-closed 熔断进行中（RELEASING）的发行，无部分态、无“双方都成功”。
3. 相同事故请求（request_key）重复到达：只返回首次裁决结果，不产生第二次状态迁移。
4. 两阶段裁决（阶段1锁定落盘 → 阶段2提交），服务中断后启动 recover() 续裁。
5. 恢复可信状态：质量负责人 + 发行负责人**两个不同角色、不同自然人**确认，
   且谱系在隔离期间未发生新分叉；恢复同样产生新隔离代次，旧检查/旧签核不得复用。
"""
from __future__ import annotations

import json

from .service import ECOError, fp_json
from .store import utcnow

# 恢复可信状态所需的两个角色（必须两个不同自然人，复用签核的双角色规则）
ROLE_QUALITY = "QUALITY_LEAD"
ROLE_RELEASE = "RELEASE_LEAD"
RESTORE_ROLES = [ROLE_QUALITY, ROLE_RELEASE]
ROLE_LABELS = {ROLE_QUALITY: "质量负责人", ROLE_RELEASE: "发行负责人"}

QUARANTINE = "QUARANTINE"
RESTORE = "RESTORE"

# 两阶段状态
Q_QUARANTINING, Q_QUARANTINED = "QUARANTINING", "QUARANTINED"
Q_RESTORING, Q_RESTORED = "RESTORING", "RESTORED"

# 隔离后 ECO 转回的状态（复用既有状态机：待重新对齐）
REALIGN_STATE = "REBASE_REQUIRED"


class IsolationError(ECOError):
    pass


class IsolationService:
    def __init__(self, store, svc):
        self.s = store
        self.svc = svc            # ECOService：指纹/纪元/发布/谱系原语的唯一裁决者

    # ---- 通用 ----
    def _journal(self, c, quarantine_id, incident_id, event, detail=None):
        c.execute(
            "INSERT INTO isolation_journal(quarantine_id,incident_id,event,at,detail_json)"
            " VALUES(?,?,?,?,?)",
            (quarantine_id, incident_id, event, utcnow(),
             json.dumps(detail or {}, ensure_ascii=False)))

    def _audit(self, c, action, entity, actor, detail=None):
        c.execute("INSERT INTO audit_log(actor,action,entity,at,detail_json) VALUES(?,?,?,?,?)",
                  (actor, action, entity, utcnow(),
                   json.dumps(detail or {}, ensure_ascii=False)))

    def _incident_dict(self, row):
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    def _quarantine_dict(self, row, c=None):
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        if c is not None:
            d["confirms"] = [dict(r) for r in c.execute(
                "SELECT role,user,at FROM restore_confirm WHERE quarantine_id=? ORDER BY seq",
                (row["id"],)).fetchall()]
        return d

    # ===== 事故登记 + 隔离（两阶段、幂等）=====
    def register_incident(self, *, batch, faulty_baseline_id, reason, evidence=None,
                          registered_by, request_key=None, failpoint=None):
        """质量负责人登记事故并原子触发隔离裁决。

        - batch/reason/evidence/registered_by 必填；faulty_baseline_id 必须是已发行基线；
        - 相同 request_key（缺省由请求内容指纹派生）重复到达：返回首次裁决（deduped:true），
          不产生第二次状态迁移；
        - failpoint=AFTER_QUARANTINE_PHASE1 模拟阶段1提交后崩溃（可 recover 续裁）。
        """
        evidence = evidence if evidence is not None else []
        self._validate_registration(batch, faulty_baseline_id, reason, evidence, registered_by)
        req_fp = fp_json({"batch": batch, "faulty_baseline_id": faulty_baseline_id,
                          "reason": reason, "evidence": evidence,
                          "registered_by": registered_by})
        rk = (request_key or f"inc:{req_fp}").strip()
        with self.s.tx() as c:
            # ① 幂等：同 request_key 直接回放首次登记 + 裁决结果（无第二次迁移）
            prev = c.execute("SELECT * FROM incident WHERE request_key=?", (rk,)).fetchone()
            if prev:
                q = c.execute(
                    "SELECT * FROM quarantine WHERE incident_id=? AND kind=? ORDER BY rowid",
                    (prev["id"], QUARANTINE)).fetchone()
                out = {"deduped": True, "incident": self._incident_dict(prev),
                       "request_key": rk}
                if q:
                    out["quarantine"] = self._finish_if_needed(c, q)
                    out["already_quarantined"] = q["state"] == Q_QUARANTINED
                return out

            base = c.execute("SELECT * FROM baseline WHERE id=?",
                             (faulty_baseline_id,)).fetchone()
            if not base:
                raise IsolationError("BASELINE_NOT_FOUND",
                                     f"命中版本 {faulty_baseline_id} 不存在", 404)

            # ② 命中版本已不可信：相同事故（不同 request_key 的重复到达）整体回放首次
            # 裁决，不新增事故行 / 信任覆盖 / 代次——绝不产生第二次状态迁移。
            trust = self.svc.baseline_trust_status(c, faulty_baseline_id)
            if trust["status"] != "TRUSTED":
                existing_inc = c.execute(
                    "SELECT * FROM incident WHERE id=?", (trust["incident_id"],)).fetchone()
                if not existing_inc:
                    raise IsolationError(
                        "LINEAGE_ALREADY_QUARANTINED",
                        f"命中版本 {faulty_baseline_id} 已处于不可信隔离状态", 409,
                        {"trust": trust})
                existing_q = c.execute(
                    "SELECT * FROM quarantine WHERE incident_id=? AND kind=? ORDER BY rowid",
                    (trust["incident_id"], QUARANTINE)).fetchone()
                out = {"deduped": True, "already_quarantined": True, "request_key": rk,
                       "incident": self._incident_dict(existing_inc)}
                if existing_q:
                    out["quarantine"] = self._finish_if_needed(c, existing_q)
                return out

            n = c.execute("SELECT COUNT(*) k FROM incident").fetchone()["k"]
            incident_id = f"INC-{n + 1:03d}"
            c.execute(
                "INSERT INTO incident(id,batch,faulty_baseline_id,reason,evidence_json,"
                "registered_by,created_at,request_key,request_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (incident_id, batch, faulty_baseline_id, reason,
                 json.dumps(evidence, ensure_ascii=False), registered_by, utcnow(), rk, req_fp))
            self._audit(c, "incident.register", incident_id, registered_by,
                        {"batch": batch, "faulty_baseline_id": faulty_baseline_id,
                         "evidence_count": len(evidence)})

            qid = f"Q-{incident_id}"
            affected = self.svc._descendant_baselines(c, faulty_baseline_id)
            # 阶段1：锁定裁决意图并落盘（此后崩溃可续裁）
            c.execute(
                "INSERT INTO quarantine(id,incident_id,kind,faulty_baseline_id,state,started_at,"
                "failpoint,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                (qid, incident_id, QUARANTINE, faulty_baseline_id, Q_QUARANTINING, utcnow(),
                 failpoint, json.dumps({"affected_baselines": affected, "batch": batch},
                                       ensure_ascii=False)))
            self._journal(c, qid, incident_id, "QUARANTINE_PHASE1_LOCKED",
                          {"faulty_baseline_id": faulty_baseline_id,
                           "affected_baselines": affected})
            self._audit(c, "isolation.quarantine_phase1", qid, registered_by,
                        {"affected_baselines": affected})

        # 阶段1已提交（QUARANTINING 持久化）。此处故障 = 真实崩溃：阶段2未执行，recover 可续。
        if failpoint == "AFTER_QUARANTINE_PHASE1":
            raise IsolationError(
                "QUARANTINE_INTERRUPTED",
                "模拟隔离在阶段1落盘后崩溃：QUARANTINING 已持久化，启动 recover 续裁", 500,
                {"quarantine_id": qid, "incident_id": incident_id})

        qd = self.quarantine_finish(qid, registered_by)
        return {"deduped": False,
                "incident": self._incident_dict(
                    self.s.q_one("SELECT * FROM incident WHERE id=?", (incident_id,))),
                "quarantine": qd, "request_key": rk}

    def _validate_registration(self, batch, faulty_baseline_id, reason, evidence, registered_by):
        if not batch:
            raise IsolationError("BAD_REQUEST", "batch（故障批次）必填", 400)
        if not faulty_baseline_id:
            raise IsolationError("BAD_REQUEST", "faulty_baseline_id（命中版本）必填", 400)
        if not reason:
            raise IsolationError("BAD_REQUEST", "reason（故障原因）必填", 400)
        if not registered_by:
            raise IsolationError("BAD_REQUEST", "registered_by（质量负责人）必填", 400)
        if not isinstance(evidence, list) or not evidence:
            raise IsolationError("EVIDENCE_REQUIRED",
                                 "必须随事故登记至少一条证据（报告/日志/失效记录引用）", 400)
        for ev in evidence:
            if not isinstance(ev, dict) or not ev.get("ref") or not ev.get("kind"):
                raise IsolationError(
                    "BAD_EVIDENCE",
                    "每条证据须形如 {'kind': 类型, 'ref': 引用/定位, 'note'?: 说明}", 400)

    def _bounce_associated_ecos(self, c, affected_baselines, *, quarantine_id, incident_id,
                                faulty_root_id, reason_code="LINEAGE_QUARANTINED"):
        """尚未发行的关联 ECO 转回待重新对齐：作废检查与签核、释放锁、引导可信祖先。

        关联范围：parent_baseline_id 落在命中谱系，且处于非终态
        （DRAFT 无父版本时仅在 trunk 命中时连带；FROZEN/QUEUED/REBASE_REQUIRED）。
        进行中的 RELEASING 发布由调用方先行 fail-closed 熔断。
        返回 {bounced, aborted_releases}。
        """
        affected = set(affected_baselines)
        bounced, aborted = [], []
        trunk = self.svc._trunk(c)
        # 命中谱系统一重新对齐到“隔离后仍可信”的锚点（故障根父级向上的最近可信祖先）。
        anchor = self.svc.post_quarantine_anchor(c, faulty_root_id)
        eco_rows = c.execute(
            "SELECT e.* FROM eco e WHERE e.status NOT IN "
            "('RELEASED','MERGED','ABORTED','RELEASING') "
            "AND NOT EXISTS (SELECT 1 FROM release r WHERE r.eco_id=e.id AND r.state='FAILED')"
        ).fetchall()
        for eco in eco_rows:
            parent = eco["parent_baseline_id"]
            hits = parent in affected
            safe = anchor
            # 无父版本（DRAFT/QUEUED 尚未冻结）：仅当当前 trunk 本身命中时关联，
            # 重新对齐到 trunk 的最近可信祖先。
            if not parent and trunk["base_id"] in affected:
                hits = True
            if not hits:
                continue
            c.execute("UPDATE eco SET status=?, preferred_parent_id=? WHERE id=?",
                      (REALIGN_STATE, safe, eco["id"]))
            n_runs = self.svc._supersede_runs(c, eco["id"], reason_code)
            self.svc._void_signoffs(c, eco["id"], f"{reason_code}: incident {incident_id}")
            c.execute("DELETE FROM module_lock WHERE eco_id=?", (eco["id"],))
            c.execute("UPDATE queue_entry SET resolved_at=?, resolution='AUTO_PROMOTE' "
                      "WHERE blocker_eco_id=? AND resolved_at IS NULL", (utcnow(), eco["id"]))
            c.execute("UPDATE queue_entry SET resolved_at=?, resolution='AUTO_PROMOTE' "
                      "WHERE eco_id=? AND resolved_at IS NULL", (utcnow(), eco["id"]))
            c.execute(
                "INSERT INTO rejected_result(run_id,eco_id,check_type,reason,detail_json,"
                "received_at) VALUES(?,?,?,?,?,?)",
                (None, eco["id"], None, reason_code,
                 json.dumps({"incident_id": incident_id, "quarantine_id": quarantine_id,
                             "realign_to": safe, "superseded_runs": n_runs}, ensure_ascii=False),
                 utcnow()))
            self._audit(c, "eco.quarantine_realign", eco["id"], "system",
                        {"incident_id": incident_id, "quarantine_id": quarantine_id,
                         "realign_to": safe, "superseded_runs": n_runs})
            bounced.append({"eco_id": eco["id"], "previous_status": eco["status"],
                            "realign_to": safe, "superseded_runs": n_runs})
        self.svc._promote_waiting(c)
        return {"bounced": bounced, "aborted_releases": aborted}

    def quarantine_finish(self, quarantine_id, by="system"):
        """阶段2：在同一谱系原子落定隔离（信任覆盖 + 关联 ECO 重对齐 + 代次 +1）。"""
        with self.s.tx() as c:
            return self._quarantine_finish_locked(
                c, c.execute("SELECT * FROM quarantine WHERE id=?",
                             (quarantine_id,)).fetchone(), by)

    def _finish_if_needed(self, c, q):
        """幂等重放/恢复辅助：已完成则直接返回，中断态则在当前事务续裁。"""
        if q["state"] in (Q_QUARANTINED, Q_RESTORED):
            return self._quarantine_dict(q, c)
        if q["kind"] == QUARANTINE:
            return self._quarantine_finish_locked(c, q, "system", resumed=True)
        return self._restore_finish_locked(c, q, "system", resumed=True)

    def _quarantine_finish_locked(self, c, q, by, resumed=False):
        if not q:
            raise IsolationError("QUARANTINE_NOT_FOUND", "隔离裁决不存在", 404)
        if q["state"] == Q_QUARANTINED:
            return self._quarantine_dict(q, c)
        if q["kind"] != QUARANTINE or q["state"] != Q_QUARANTINING:
            raise IsolationError("BAD_QUARANTINE_STATE",
                                 f"裁决 {q['id']} 状态 {q['state']} 不可执行隔离提交", 409)
        affected = self.svc._descendant_baselines(c, q["faulty_baseline_id"])
        # 熔断命中谱系上进行中的发行（与新发行并发时的原子裁决，fail-closed）。
        # 必须先于待重对齐扫描，使 RELEASING 单只计入 aborted_releases 而不重复 bounce。
        aborted_releases = []
        anchor = self.svc.post_quarantine_anchor(c, q["faulty_baseline_id"])
        for eco in c.execute(
                "SELECT * FROM eco WHERE status='RELEASING'").fetchall():
            if self.svc._quarantine_touches(c, q["faulty_baseline_id"], eco["parent_baseline_id"]):
                self.svc._abort_release_due_to_quarantine(c, eco, q, anchor)
                aborted_releases.append(eco["id"])
        bounce = self._bounce_associated_ecos(
            c, affected, quarantine_id=q["id"], incident_id=q["incident_id"],
            faulty_root_id=q["faulty_baseline_id"])
        bounce["aborted_releases"] = aborted_releases
        gen = self.svc._bump_isolation_generation(c)
        for bid in affected:
            c.execute(
                "INSERT INTO baseline_trust(baseline_id,status,generation,incident_id,via,by,at)"
                " VALUES(?,?,?,?,?,?,?)",
                (bid, "UNTRUSTED", gen, q["incident_id"], "QUARANTINE", by, utcnow()))
        c.execute("UPDATE quarantine SET state=?,generation=?,finished_at=? WHERE id=?",
                  (Q_QUARANTINED, gen, utcnow(), q["id"]))
        detail = json.loads(q["detail_json"] or "{}")
        detail.update({"generation": gen, **bounce})
        c.execute("UPDATE quarantine SET detail_json=? WHERE id=?",
                  (json.dumps(detail, ensure_ascii=False), q["id"]))
        self._journal(c, q["id"], q["incident_id"],
                      "QUARANTINE_RECOVERED" if resumed else "QUARANTINE_PHASE2_COMMITTED",
                      {"generation": gen, "affected_baselines": affected, **bounce})
        self._audit(c, "isolation.quarantine_commit", q["id"], by,
                    {"generation": gen, "affected_baselines": affected, "resumed": resumed,
                     **bounce})
        return self._quarantine_dict(
            c.execute("SELECT * FROM quarantine WHERE id=?", (q["id"],)).fetchone(), c)

    # ===== 双角色可信恢复 =====
    def restore_trust(self, *, faulty_baseline_id, confirms, restored_by=None,
                      request_key=None, failpoint=None, note=""):
        """恢复命中谱系的可信状态。

        confirms: [{role: QUALITY_LEAD|RELEASE_LEAD, user}]，必须两个角色齐备且为
        两个不同自然人；谱系在隔离期间未发生新分叉（无可信修复分支），否则 409
        LINEAGE_FORKED_SINCE_QUARANTINE。恢复产生**新隔离代次**，旧检查/旧签核不复用。
        """
        self._validate_confirms(confirms)
        by = restored_by or confirms[0]["user"]
        with self.s.tx() as c:
            base = c.execute("SELECT * FROM baseline WHERE id=?",
                             (faulty_baseline_id,)).fetchone()
            if not base:
                raise IsolationError("BASELINE_NOT_FOUND",
                                     f"版本 {faulty_baseline_id} 不存在", 404)
            trust = self.svc.baseline_trust_status(c, faulty_baseline_id)
            if trust["status"] == "TRUSTED":
                # 已可信：恢复幂等（无第二次迁移）
                return {"deduped": True, "already_trusted": True,
                        "faulty_baseline_id": faulty_baseline_id, "trust": trust}
            incident_id = trust["incident_id"]
            qroot = c.execute(
                "SELECT * FROM quarantine WHERE incident_id=? AND kind=? AND state=?",
                (incident_id, QUARANTINE, Q_QUARANTINED)).fetchone()
            if not qroot:
                raise IsolationError("NO_COMPLETED_QUARANTINE",
                                     "该版本的事故隔离尚未落定，不能恢复", 409)
            # 恢复针对事故命中根：整棵命中谱系一次性恢复，不支持只恢复某个后代
            if faulty_baseline_id != qroot["faulty_baseline_id"]:
                raise IsolationError(
                    "RESTORE_TARGET_MUST_BE_ROOT",
                    "请对事故命中的根版本发起恢复（整条命中谱系会一并恢复）", 409,
                    {"requested": faulty_baseline_id,
                     "quarantine_root": qroot["faulty_baseline_id"],
                     "incident_id": incident_id})

            # 幂等：相同恢复请求重放（同事故 + 同确认人）
            rk = (request_key or f"restore:{incident_id}:"
                  + fp_json(sorted((x["role"], x["user"]) for x in confirms))).strip()
            prev = c.execute(
                "SELECT * FROM quarantine WHERE kind=? AND incident_id=? "
                "AND state IN (?,?) ORDER BY rowid",
                (RESTORE, incident_id, Q_RESTORING, Q_RESTORED)).fetchone()
            if prev:
                return {"deduped": True,
                        "quarantine": self._finish_if_needed(c, prev)}

            # 分叉检测：隔离期间不得在可信祖先上长出绕过命中链的新发行（修复分支）
            fork = self._detect_fork(c, faulty_baseline_id, qroot)
            if fork:
                self._audit(c, "isolation.restore_rejected_fork", faulty_baseline_id, by,
                            {"incident_id": incident_id, **fork})
                raise IsolationError(
                    "LINEAGE_FORKED_SINCE_QUARANTINE",
                    "谱系在隔离期间发生了新分叉（可信祖先上已有修复发行），"
                    "不能直接恢复旧谱系可信；应以修复分支为准重建", 409,
                    {"incident_id": incident_id, **fork})

            rid = f"R-{incident_id}"
            affected = self.svc._descendant_baselines(c, faulty_baseline_id)
            c.execute(
                "INSERT INTO quarantine(id,incident_id,kind,faulty_baseline_id,root_baseline_id,"
                "state,started_at,failpoint,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, incident_id, RESTORE, faulty_baseline_id, faulty_baseline_id,
                 Q_RESTORING, utcnow(), failpoint,
                 json.dumps({"affected_baselines": affected, "note": note,
                             "request_key": rk}, ensure_ascii=False)))
            for cf in confirms:
                c.execute(
                    "INSERT INTO restore_confirm(quarantine_id,role,user,at) VALUES(?,?,?,?)",
                    (rid, cf["role"], cf["user"], utcnow()))
            self._journal(c, rid, incident_id, "RESTORE_PHASE1_LOCKED",
                          {"faulty_baseline_id": faulty_baseline_id,
                           "affected_baselines": affected,
                           "confirms": confirms, "fork_checked": True})
            self._audit(c, "isolation.restore_phase1", rid, by,
                        {"confirms": confirms, "affected_baselines": affected})

        if failpoint == "AFTER_RESTORE_PHASE1":
            raise IsolationError(
                "RESTORE_INTERRUPTED",
                "模拟可信恢复在阶段1落盘后崩溃：RESTORING 已持久化，启动 recover 续裁", 500,
                {"quarantine_id": rid, "incident_id": incident_id})

        qd = self.restore_finish(rid, by)
        return {"deduped": False, "quarantine": qd}

    def _validate_confirms(self, confirms):
        if not isinstance(confirms, list):
            raise IsolationError("BAD_REQUEST", "confirms 必须是确认列表", 400)
        roles = {}
        for cf in confirms:
            if not isinstance(cf, dict) or cf.get("role") not in RESTORE_ROLES or not cf.get("user"):
                raise IsolationError(
                    "BAD_CONFIRM",
                    f"每条确认须为 role∈{RESTORE_ROLES} 且 user 非空", 400)
            roles[cf["role"]] = cf["user"]
        if set(roles) != set(RESTORE_ROLES):
            raise IsolationError(
                "DUAL_ROLE_CONFIRM_REQUIRED",
                f"恢复可信必须同时具备 {RESTORE_ROLES} 两个角色确认", 409,
                {"required_roles": RESTORE_ROLES, "provided_roles": sorted(roles)})
        if len(set(roles.values())) != 2:
            raise IsolationError(
                "DUAL_ROLE_CONFLICT",
                "两个恢复确认角色必须由不同自然人承担（禁止同一人双确认）", 409)

    def _detect_fork(self, c, faulty_id, qroot):
        """隔离期间新分叉检测。

        若命中根的最近可信祖先（隔离发生时）在隔离之后又派生出**不在命中链上**的
        新基线（工程师在可信祖先上修复后发行的分支），即视为谱系已分叉，禁止朴素恢复。
        """
        # 命中集合（故障链）
        hit_chain = set(self.svc._descendant_baselines(c, qroot["faulty_baseline_id"]))
        anchor = self.svc.nearest_trusted_ancestor(c, qroot["faulty_baseline_id"])
        q_at = qroot["finished_at"] or qroot["started_at"]
        if not anchor:
            return None
        for bid in self.svc._descendant_baselines(c, anchor):
            if bid in hit_chain or bid == anchor:
                continue
            row = c.execute("SELECT created_at FROM baseline WHERE id=?", (bid,)).fetchone()
            # 隔离之后（或同期）出现在旁路上的发行即为修复分叉
            if row and row["created_at"] >= q_at:
                return {"fork_baseline_id": bid, "trusted_anchor": anchor,
                        "quarantined_at": q_at, "forked_at": row["created_at"]}
        return None

    def restore_finish(self, quarantine_id, by="system"):
        with self.s.tx() as c:
            q = c.execute("SELECT * FROM quarantine WHERE id=?", (quarantine_id,)).fetchone()
            return self._restore_finish_locked(c, q, by)

    def _restore_finish_locked(self, c, q, by, resumed=False):
        if not q:
            raise IsolationError("QUARANTINE_NOT_FOUND", "恢复裁决不存在", 404)
        if q["state"] == Q_RESTORED:
            return self._quarantine_dict(q, c)
        if q["kind"] != RESTORE or q["state"] != Q_RESTORING:
            raise IsolationError("BAD_QUARANTINE_STATE",
                                 f"裁决 {q['id']} 状态 {q['state']} 不可执行恢复提交", 409)
        # 提交前（及崩溃恢复后续裁时）再次确认：双角色 + 期间无新分叉
        confirms = c.execute(
            "SELECT role,user FROM restore_confirm WHERE quarantine_id=?", (q["id"],)).fetchall()
        roles = {r["role"]: r["user"] for r in confirms}
        if set(roles) != set(RESTORE_ROLES) or len(set(roles.values())) != 2:
            raise IsolationError("DUAL_ROLE_CONFIRM_REQUIRED",
                                 "恢复裁决缺少有效的双角色（不同人）确认", 409)
        trust = self.svc.baseline_trust_status(c, q["faulty_baseline_id"])
        qroot = c.execute(
            "SELECT * FROM quarantine WHERE incident_id=? AND kind=? AND state=?",
            (trust["incident_id"], QUARANTINE, Q_QUARANTINED)).fetchone()
        fork = self._detect_fork(c, q["faulty_baseline_id"], qroot)
        if fork:
            raise IsolationError("LINEAGE_FORKED_SINCE_QUARANTINE",
                                 "恢复续裁时发现谱系已在隔离期间分叉，拒绝恢复", 409, fork)

        affected = self.svc._descendant_baselines(c, q["faulty_baseline_id"])
        gen = self.svc._bump_isolation_generation(c)
        for bid in affected:
            c.execute(
                "INSERT INTO baseline_trust(baseline_id,status,generation,incident_id,via,by,at)"
                " VALUES(?,?,?,?,?,?,?)",
                (bid, "TRUSTED", gen, q["incident_id"], "RESTORE", by, utcnow()))
        c.execute("UPDATE quarantine SET state=?,generation=?,finished_at=? WHERE id=?",
                  (Q_RESTORED, gen, utcnow(), q["id"]))
        detail = json.loads(q["detail_json"] or "{}")
        detail.update({"generation": gen, "confirms": [dict(r) for r in confirms]})
        c.execute("UPDATE quarantine SET detail_json=? WHERE id=?",
                  (json.dumps(detail, ensure_ascii=False), q["id"]))
        self._journal(c, q["id"], q["incident_id"],
                      "RESTORE_RECOVERED" if resumed else "RESTORE_PHASE2_COMMITTED",
                      {"generation": gen, "affected_baselines": affected})
        self._audit(c, "isolation.restore_commit", q["id"], by,
                    {"generation": gen, "affected_baselines": affected, "resumed": resumed})
        return self._quarantine_dict(
            c.execute("SELECT * FROM quarantine WHERE id=?", (q["id"],)).fetchone(), c)

    # ===== 启动恢复：从落盘日志续裁未完成裁决 =====
    def recover(self) -> dict:
        """服务启动时调用：续裁所有 QUARANTINING / RESTORING 中断裁决。

        每个裁决在独立事务内续裁并落盘日志；恢复可信状态时重新校验双角色与无分叉，
        条件不再满足则保持中断态并在审计中记录（绝不带错误状态提交）。
        """
        finished, blocked = [], []
        pending = self.s.q_all(
            "SELECT * FROM quarantine WHERE state IN (?,?)",
            (Q_QUARANTINING, Q_RESTORING))
        for q in pending:
            try:
                with self.s.tx() as c:
                    qq = c.execute("SELECT * FROM quarantine WHERE id=?", (q["id"],)).fetchone()
                    if qq["kind"] == QUARANTINE:
                        self._quarantine_finish_locked(c, qq, "system", resumed=True)
                    else:
                        self._restore_finish_locked(c, qq, "system", resumed=True)
                finished.append(q["id"])
            except IsolationError as e:
                blocked.append({"quarantine_id": q["id"], "reason": e.code,
                                "message": e.message})
                with self.s.tx() as c:
                    self._journal(c, q["id"], q["incident_id"], "RECOVER_BLOCKED",
                                  {"reason": e.code, "message": e.message})
        with self.s.tx() as c:
            self._audit(c, "isolation.recover", "system", "system",
                        {"resumed": finished, "blocked": blocked})
        return {"resumed": finished, "blocked": blocked,
                "generation": self.svc._next_isolation_generation(self.s.conn)}

    # ===== 查询：事故 / 裁决 / 审计 =====
    def list_incidents(self):
        with self.s.lock:
            return [self._incident_dict(r) for r in self.s.q_all(
                "SELECT * FROM incident ORDER BY rowid")]

    def incident_detail(self, incident_id):
        row = self.s.q_one("SELECT * FROM incident WHERE id=?", (incident_id,))
        if not row:
            raise IsolationError("INCIDENT_NOT_FOUND", f"事故 {incident_id} 不存在", 404)
        with self.s.lock:
            d = self._incident_dict(row)
            d["quarantines"] = [self._quarantine_dict(q, self.s.conn) for q in self.s.q_all(
                "SELECT * FROM quarantine WHERE incident_id=? ORDER BY rowid", (incident_id,))]
        return d

    def list_quarantines(self):
        with self.s.lock:
            return [self._quarantine_dict(q, self.s.conn) for q in self.s.q_all(
                "SELECT * FROM quarantine ORDER BY rowid")]

    def audit_events(self, *, incident_id=None, quarantine_id=None, event=None, limit=200):
        limit = max(1, min(int(limit or 200), 1000))
        sql = "SELECT * FROM isolation_journal WHERE 1=1"
        args = []
        if incident_id:
            sql += " AND incident_id=?"
            args.append(incident_id)
        if quarantine_id:
            sql += " AND quarantine_id=?"
            args.append(quarantine_id)
        if event:
            sql += " AND event=?"
            args.append(event)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        with self.s.lock:
            rows = [dict(r) for r in self.s.q_all(sql, tuple(args))]
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json") or "{}")
        return {"events": rows}

    def _trust_generation(self, c, baseline_id):
        t = self.svc.baseline_trust_status(c, baseline_id)
        return t["generation"], t["status"]

    # ===== 操作页聚合：受影响谱系 / 当前可信祖先 / 待处理变更 / 隔离审计 =====
    def board(self):
        with self.s.lock:
            with self.s.tx() as c:
                gen = self.svc._next_isolation_generation(c)
                baselines = c.execute(
                    "SELECT * FROM baseline ORDER BY revision").fetchall()
                children = self.svc._baseline_children(c)
                lineage = []
                untrusted = []
                for b in baselines:
                    t = self.svc.baseline_trust_status(c, b["id"])
                    safe = self.svc.nearest_trusted_ancestor(c, b["id"])
                    node = {"id": b["id"], "revision": b["revision"],
                            "parent_id": b["parent_id"], "eco_id": b["eco_id"],
                            "children": children.get(b["id"], []),
                            "trust": t,
                            "nearest_trusted_ancestor": safe}
                    lineage.append(node)
                    if t["status"] != "TRUSTED":
                        untrusted.append(node)

                # 待处理变更：被转回 REBASE_REQUIRED 且父谱系（曾）命中隔离的非终态 ECO
                pending = []
                for e in c.execute(
                        "SELECT * FROM eco WHERE status NOT IN "
                        "('RELEASED','MERGED','ABORTED') ORDER BY id").fetchall():
                    t = (self.svc.baseline_trust_status(c, e["parent_baseline_id"])
                         if e["parent_baseline_id"] else None)
                    need = (e["status"] == REALIGN_STATE and
                            (t is None or t["status"] != "TRUSTED"
                             or e["preferred_parent_id"] is not None))
                    pending.append({
                        "eco_id": e["id"], "title": e["title"], "owner": e["owner"],
                        "status": e["status"], "parent_baseline_id": e["parent_baseline_id"],
                        "preferred_parent_id": e["preferred_parent_id"],
                        "freeze_epoch": e["freeze_epoch"],
                        "isolation_generation": e["isolation_generation"],
                        "parent_trust": t,
                        "nearest_trusted_ancestor": (
                            self.svc.nearest_trusted_ancestor(c, e["parent_baseline_id"])
                            if e["parent_baseline_id"] else
                            self.svc.nearest_trusted_ancestor(
                                c, self.svc._trunk(c)["base_id"])),
                        "action_required": need,
                    })
                incidents = [self._incident_dict(r) for r in c.execute(
                    "SELECT * FROM incident ORDER BY rowid").fetchall()]
                quarantines = [self._quarantine_dict(q, c) for q in c.execute(
                    "SELECT * FROM quarantine ORDER BY rowid").fetchall()]
                open_q = [q["id"] for q in c.execute(
                    "SELECT id FROM quarantine WHERE state IN (?,?)",
                    (Q_QUARANTINING, Q_RESTORING)).fetchall()]
                journal = [dict(r) for r in c.execute(
                    "SELECT * FROM isolation_journal ORDER BY seq DESC LIMIT 100").fetchall()]
                for r in journal:
                    r["detail"] = json.loads(r.pop("detail_json") or "{}")
                aborted = [dict(r) for r in c.execute(
                    "SELECT eco_id,state,failpoint,finished_at FROM release "
                    "WHERE state='FAILED' ORDER BY finished_at DESC").fetchall()]
                return {
                    "at": utcnow(),
                    "isolation_generation": gen,
                    "lineage": lineage,
                    "untrusted_lineage": untrusted,
                    "current_trunk": (lambda t: {
                        "base_id": t["base_id"], "base_revision": t["base_revision"],
                        "trust": self.svc.baseline_trust_status(c, t["base_id"]),
                        "nearest_trusted_ancestor":
                            self.svc.nearest_trusted_ancestor(c, t["base_id"])})(
                        self.svc._trunk(c)),
                    "pending_changes": [p for p in pending if p["action_required"]],
                    "all_open_changes": pending,
                    "incidents": incidents,
                    "quarantines": quarantines,
                    "open_adjudications": open_q,
                    "aborted_releases": aborted,
                    "audit": journal,
                    "restore_roles": [{"role": r, "label": ROLE_LABELS[r]} for r in RESTORE_ROLES],
                }
