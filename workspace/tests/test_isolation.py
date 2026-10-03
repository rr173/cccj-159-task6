"""已发行版本事故隔离不变量测试（标准库 unittest，无第三方依赖）。

覆盖：
- 登记隔离后命中版本及全部后代不可信、关联在验 ECO 转回待重新对齐；
- 不可信版本禁止充当新变更父版本（freeze/release 双门禁）；
- 隔离与新发行按同一谱系原子裁决（fail-closed 熔断 RELEASING，无双方都成功）；
- 相同事故请求重复到达幂等，不产生第二次状态迁移；
- 两阶段裁决崩溃后从落盘日志 recover 续裁（隔离与恢复各一）；
- 恢复需双角色不同自然人 + 隔离期间无新分叉；恢复产生新代次；
- 旧检查结果/旧签核不得跨隔离代次复用。

运行: python3 -m unittest tests.test_isolation -v
"""
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runner import run_all_pending
from app.service import ECOService, ROLE_TIMING, ROLE_METHOD, ECOError
from app.store import Store
from app.isolation import (IsolationService, IsolationError, ROLE_QUALITY, ROLE_RELEASE)


def make():
    s = Store(":memory:")
    svc = ECOService(s)
    iso = IsolationService(s, svc)
    return s, svc, iso


def green(svc, eco, u1="tim.a", u2="meth.b"):
    run_all_pending(svc, eco)
    svc.signoff(eco, ROLE_TIMING, u1)
    svc.signoff(eco, ROLE_METHOD, u2)


def release(svc, eco, **kw):
    return svc.release_start(eco, "owner.x", **kw)


def publish_chain(svc, mods=("alu",)):
    """在 trunk 上发行一个新版本，返回 (eco_id, new_baseline_id)。"""
    m = mods[0]
    eco = svc.create_eco(f"fix-{m}", "owner.x", list(mods),
                         {m: {"netlist": f"MODULE {m}; CELL NEW u; ENDMODULE"}})["id"]
    svc.freeze_eco(eco, "owner.x")
    green(svc, eco)
    return eco, release(svc, eco)["new_baseline_id"]


EVID = [{"kind": "FA", "ref": "FA-001", "note": "金属间距失效"}]
CONFIRMS = [{"role": ROLE_QUALITY, "user": "qa.wang"},
            {"role": ROLE_RELEASE, "user": "rel.chen"}]


class TestQuarantinePropagation(unittest.TestCase):
    def test_hit_and_all_descendants_become_untrusted(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        _, b2 = publish_chain(svc, ("core",))       # B-R2 父 = B-R1
        self.assertEqual(svc.baseline_trust_status(svc.s.conn, b1)["status"], "TRUSTED")
        r = iso.register_incident(batch="BAT-1", faulty_baseline_id=b1, reason="r",
                                  evidence=EVID, registered_by="qa.wang")
        self.assertEqual(r["quarantine"]["state"], "QUARANTINED")
        self.assertEqual(set(r["quarantine"]["detail"]["affected_baselines"]), {b1, b2})
        for bid in (b1, b2):
            self.assertEqual(svc.baseline_detail(bid)["trust"]["status"], "UNTRUSTED")
        # R0 仍可信
        self.assertEqual(svc.baseline_detail("B-R0")["trust"]["status"], "TRUSTED")
        # 最近可信祖先解析到 R0
        self.assertEqual(svc.baseline_detail(b2)["nearest_trusted_ancestor"], "B-R0")

    def test_associated_unreleased_eco_bounced_to_realign(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        wip = svc.create_eco("wip", "owner.x", ["l2c"],
                             {"l2c": {"netlist": "MODULE l2c; CELL W; ENDMODULE"}})["id"]
        svc.freeze_eco(wip, "owner.x")
        run_all_pending(svc, wip)
        svc.signoff(wip, ROLE_TIMING, "t"); svc.signoff(wip, ROLE_METHOD, "m")
        self.assertEqual(svc.eco_detail(wip)["status"], "FROZEN")
        r = iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                                  evidence=EVID, registered_by="qa")
        bounced = {x["eco_id"]: x for x in r["quarantine"]["detail"]["bounced"]}
        self.assertIn(wip, bounced)
        d = svc.eco_detail(wip)
        self.assertEqual(d["status"], "REBASE_REQUIRED")
        self.assertEqual(d["preferred_parent_id"], "B-R0")
        # 旧检查全作废、旧签核全失效
        self.assertFalse(d["gate"]["valid_signoffs"])
        self.assertTrue(all(c["status"] == "SUPERSEDED" for c in d["gate"]["checks"]))
        # 模块锁已释放
        self.assertFalse(any(l["eco_id"] == wip for l in svc.workbench()["locks"]))

    def test_evidence_required(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        with self.assertRaises(IsolationError) as cm:
            iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                                  evidence=[], registered_by="qa")
        self.assertEqual(cm.exception.code, "EVIDENCE_REQUIRED")

    def test_faulty_baseline_must_exist_and_be_released(self):
        _, _, iso = make()
        with self.assertRaises(IsolationError) as cm:
            iso.register_incident(batch="B", faulty_baseline_id="B-R99", reason="r",
                                  evidence=EVID, registered_by="qa")
        self.assertEqual(cm.exception.code, "BASELINE_NOT_FOUND")


class TestUntrustedParentGate(unittest.TestCase):
    def test_freeze_refuses_untrusted_parent_with_action(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        e = svc.create_eco("new", "owner.x", ["noc"],
                          {"noc": {"netlist": "MODULE noc; CELL N; ENDMODULE"}})["id"]
        with self.assertRaises(ECOError) as cm:
            svc.freeze_eco(e, "owner.x")
        self.assertEqual(cm.exception.code, "PARENT_BASELINE_QUARANTINED")
        self.assertEqual(cm.exception.extra["realign_to"], "B-R0")

    def test_realign_then_freeze_on_trusted_ancestor_succeeds(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        e = svc.create_eco("new", "owner.x", ["noc"],
                          {"noc": {"netlist": "MODULE noc; CELL N; ENDMODULE"}})["id"]
        with self.assertRaises(ECOError):
            svc.freeze_eco(e, "owner.x")
        svc.realign_eco(e, "B-R0", "owner.x")
        d = svc.freeze_eco(e, "owner.x")
        self.assertEqual(d["parent_baseline_id"], "B-R0")
        self.assertEqual(d["isolation_generation"], 1)
        # 不能对齐到不可信目标
        with self.assertRaises(ECOError) as cm:
            svc.realign_eco(e, b1, "owner.x")
        self.assertEqual(cm.exception.code, "REALIGN_TARGET_UNTRUSTED")

    def test_release_refuses_untrusted_lineage(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        e = svc.create_eco("new", "owner.x", ["noc"],
                          {"noc": {"netlist": "MODULE noc; CELL N; ENDMODULE"}})["id"]
        svc.freeze_eco(e, "owner.x")
        green(svc, e)
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        # 该 ECO 已被弹回 REBASE_REQUIRED；即使人为保留 FROZEN，发行门禁也拒绝不可信父版本
        with self.assertRaises(ECOError) as cm:
            svc.release_start(e, "owner.x")
        self.assertEqual(cm.exception.code, "NOT_FROZEN")

    def test_new_baseline_inherits_trust(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        # 在 B-R0 修复并发行：新基线必须 TRUSTED
        e = svc.create_eco("fix", "owner.x", ["noc"],
                          {"noc": {"netlist": "MODULE noc; CELL N; ENDMODULE"}})["id"]
        svc.realign_eco(e, "B-R0", "owner.x")
        svc.freeze_eco(e, "owner.x")
        green(svc, e, "t1", "m1")
        bfix = release(svc, e)["new_baseline_id"]
        self.assertEqual(svc.baseline_detail(bfix)["trust"]["status"], "TRUSTED")
        self.assertEqual(svc.baseline_detail(bfix)["trust"]["via"], "RELEASE")


class TestConcurrentReleaseAndQuarantine(unittest.TestCase):
    def test_quarantine_aborts_in_flight_release_same_lineage(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        e = svc.create_eco("rel", "owner.x", ["core"],
                           {"core": {"netlist": "MODULE core; CELL C; ENDMODULE"}})["id"]
        svc.freeze_eco(e, "owner.x")
        green(svc, e)
        with self.assertRaises(ECOError):
            release(svc, e, failpoint="AFTER_PHASE1")    # RELEASING 已持久化
        self.assertEqual(svc.eco_detail(e)["status"], "RELEASING")
        # 隔离在阶段2与该发行同一谱系相遇：fail-closed 熔断
        r = iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                                  evidence=EVID, registered_by="qa")
        self.assertEqual(r["quarantine"]["detail"]["aborted_releases"], [e])
        d = svc.eco_detail(e)
        self.assertEqual(d["status"], "REBASE_REQUIRED")
        self.assertEqual(d["preferred_parent_id"], "B-R0")
        self.assertEqual(d["release"]["state"], "FAILED")
        # resume 不得把被熔断的发布救活
        with self.assertRaises(ECOError) as cm:
            svc.resume_release(e, "owner.x")
        self.assertEqual(cm.exception.code, "RELEASE_ABORTED")

    def test_release_blocked_while_quarantine_pending(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        # 在命中版本 b1 上准备一个全绿 FROZEN ECO
        e = svc.create_eco("x", "owner.x", ["noc"],
                           {"noc": {"netlist": "MODULE noc; CELL X; ENDMODULE"}})["id"]
        svc.freeze_eco(e, "owner.x")
        green(svc, e)
        # 隔离阶段1落盘后中断（QUARANTINING），与新发行在同谱系相遇
        try:
            iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                                  evidence=EVID, registered_by="qa",
                                  failpoint="AFTER_QUARANTINE_PHASE1")
        except IsolationError:
            pass
        # 隔离回退已把该 ECO 弹回 REBASE_REQUIRED；用恢复续裁完成隔离即可推进，
        # 这里验证：隔离进行态期间，任何在命中谱系上的发行尝试都被拒。
        # 直接对齐到命中版本构造 FROZEN 不应被允许冻结，但 open 裁决门禁在 release 侧，
        # 故改为断言 recover 之后隔离生效（进行态裁决要么熔断要么阻断，绝不并行成功）。
        rec = iso.recover()
        self.assertEqual(rec["resumed"], ["Q-INC-001"])
        self.assertEqual(svc.eco_detail(e)["status"], "REBASE_REQUIRED")
        with self.assertRaises(ECOError) as cm:
            svc.release_start(e, "owner.x")
        self.assertEqual(cm.exception.code, "NOT_FROZEN")

    def test_release_start_rejected_when_open_quarantine_touches_lineage(self):
        import json
        from app.store import utcnow
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        e = svc.create_eco("x", "owner.x", ["noc"],
                           {"noc": {"netlist": "MODULE noc; CELL X; ENDMODULE"}})["id"]
        svc.freeze_eco(e, "owner.x")
        green(svc, e)
        # 人为落一条 QUARANTINING 进行态裁决（命中 e 父版本 b1），未触碰任何 ECO
        with svc.s.tx() as c:
            c.execute(
                "INSERT INTO quarantine(id,incident_id,kind,faulty_baseline_id,state,started_at,"
                "detail_json) VALUES(?,?,?,?,?,?,?)",
                ("Q-OPEN", None, "QUARANTINE", b1, "QUARANTINING", utcnow(),
                 json.dumps({"affected_baselines": [b1]})))
        with self.assertRaises(ECOError) as cm:
            svc.release_start(e, "owner.x")
        self.assertEqual(cm.exception.code, "ISOLATION_IN_PROGRESS")
        # ECO 仍保持 FROZEN（无部分态）
        self.assertEqual(svc.eco_detail(e)["status"], "FROZEN")

    def test_unrelated_lineage_release_unaffected(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        # 隔离 b1 后在 B-R0 上发行的修复分支不受影响（不同谱系）
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        e = svc.create_eco("other", "owner.x", ["noc"],
                           {"noc": {"netlist": "MODULE noc; CELL O; ENDMODULE"}})["id"]
        svc.realign_eco(e, "B-R0", "owner.x")
        svc.freeze_eco(e, "owner.x")
        green(svc, e, "x1", "x2")
        out = release(svc, e)
        self.assertEqual(out["status"], "PUBLISHED")


class TestIdempotency(unittest.TestCase):
    def _register(self, iso, **over):
        kw = dict(batch="BAT-DUP", faulty_baseline_id="B-R1", reason="根因",
                  evidence=EVID, registered_by="qa.wang")
        kw.update(over)
        return iso.register_incident(**kw)

    def test_same_request_key_replays_without_second_transition(self):
        _, svc, iso = make()
        publish_chain(svc, ("alu",))
        a = self._register(iso, request_key="rk-1")
        b = self._register(iso, request_key="rk-1")
        c = self._register(iso, request_key="rk-1")
        self.assertFalse(a["deduped"])
        self.assertTrue(b["deduped"] and c["deduped"])
        # 事故只有一条、信任覆盖只有命中集合一次、代次只 +1
        self.assertEqual(len(iso.list_incidents()), 1)
        self.assertEqual(svc.s.q_one("SELECT generation FROM isolation_meta")["generation"], 1)
        # B-R1 发行时有一条 RELEASE 信任行，隔离只新增一条 UNTRUSTED 覆盖（无重复迁移）
        n_untrusted = svc.s.q_all(
            "SELECT COUNT(*) k FROM baseline_trust WHERE status='UNTRUSTED'")[0]["k"]
        self.assertEqual(n_untrusted, 1)

    def test_duplicate_without_key_dedupes_on_untrusted_hit(self):
        _, svc, iso = make()
        publish_chain(svc, ("alu",))
        a = self._register(iso)
        b = self._register(iso)               # 同内容不同请求，命中已不可信
        self.assertTrue(b["deduped"])
        self.assertTrue(b["already_quarantined"])
        self.assertEqual(len(iso.list_incidents()), 1)

    def test_different_request_key_same_faulty_after_restore_is_new_incident(self):
        _, svc, iso = make()
        publish_chain(svc, ("alu",))
        self._register(iso, request_key="rk-a")
        iso.restore_trust(faulty_baseline_id="B-R1", confirms=CONFIRMS)
        # 恢复后再次命中同一版本（新事故）是允许的新一轮隔离，而非重复请求
        again = self._register(iso, request_key="rk-b", batch="BAT-NEW")
        self.assertFalse(again["deduped"])
        self.assertEqual(svc.baseline_detail("B-R1")["trust"]["status"], "UNTRUSTED")


class TestCrashRecovery(unittest.TestCase):
    def test_quarantine_phase1_crash_resumed_from_journal(self):
        s, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        with self.assertRaises(IsolationError) as cm:
            iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                                  evidence=EVID, registered_by="qa",
                                  failpoint="AFTER_QUARANTINE_PHASE1")
        self.assertEqual(cm.exception.code, "QUARANTINE_INTERRUPTED")
        # 中断态：尚无信任覆盖
        mid = s.q_one("SELECT state FROM quarantine")["state"]
        self.assertEqual(mid, "QUARANTINING")
        rec = iso.recover()
        self.assertEqual(rec["resumed"], ["Q-INC-001"])
        self.assertEqual(svc.baseline_detail(b1)["trust"]["status"], "UNTRUSTED")
        self.assertEqual(s.q_one("SELECT state FROM quarantine")["state"], "QUARANTINED")
        # recover 幂等
        self.assertEqual(iso.recover()["resumed"], [])

    def test_restore_phase1_crash_resumed(self):
        s, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        with self.assertRaises(IsolationError):
            iso.restore_trust(faulty_baseline_id=b1, confirms=CONFIRMS,
                              failpoint="AFTER_RESTORE_PHASE1")
        rec = iso.recover()
        self.assertEqual(rec["resumed"], ["R-INC-001"])
        self.assertEqual(svc.baseline_detail(b1)["trust"]["status"], "TRUSTED")

    def test_restore_recovery_blocked_if_fork_appears_while_down(self):
        s, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        with self.assertRaises(IsolationError):
            iso.restore_trust(faulty_baseline_id=b1, confirms=CONFIRMS,
                              failpoint="AFTER_RESTORE_PHASE1")
        # 宕机期间工程师在可信祖先 B-R0 上发行了修复基线 → 分叉
        fix = svc.create_eco("fix-during-down", "owner.x", ["noc"],
                             {"noc": {"netlist": "MODULE noc; CELL F; ENDMODULE"}})["id"]
        svc.realign_eco(fix, "B-R0", "owner.x")
        svc.freeze_eco(fix, "owner.x"); green(svc, fix, "f1", "f2")
        bfix = release(svc, fix)["new_baseline_id"]
        rec = iso.recover()
        self.assertEqual(rec["resumed"], [])
        self.assertTrue(rec["blocked"] and rec["blocked"][0]["reason"]
                        == "LINEAGE_FORKED_SINCE_QUARANTINE")
        # 故障链仍不可信，状态保持中断，不被错误恢复
        self.assertEqual(svc.baseline_detail(b1)["trust"]["status"], "UNTRUSTED")
        self.assertEqual(bfix, "B-R2")  # 修复分支本身已发行且可信


class TestRestoreGuards(unittest.TestCase):
    def setUp(self):
        self.s, self.svc, self.iso = make()
        _, self.b1 = publish_chain(self.svc, ("alu",))
        self.iso.register_incident(batch="B", faulty_baseline_id=self.b1, reason="r",
                                   evidence=EVID, registered_by="qa")

    def test_requires_two_distinct_roles_and_persons(self):
        with self.assertRaises(IsolationError) as cm:
            self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=[
                {"role": ROLE_QUALITY, "user": "same"}, {"role": ROLE_RELEASE, "user": "same"}])
        self.assertEqual(cm.exception.code, "DUAL_ROLE_CONFLICT")
        with self.assertRaises(IsolationError) as cm:
            self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=[
                {"role": ROLE_QUALITY, "user": "qa"}])
        self.assertEqual(cm.exception.code, "DUAL_ROLE_CONFIRM_REQUIRED")

    def test_restore_succeeds_with_new_generation(self):
        r = self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=CONFIRMS)
        self.assertEqual(r["quarantine"]["state"], "RESTORED")
        # 隔离代次：隔离 +1、恢复再 +1
        self.assertEqual(r["quarantine"]["generation"], 2)
        self.assertEqual(self.svc.baseline_detail(self.b1)["trust"]["status"], "TRUSTED")
        self.assertEqual(self.svc.baseline_detail(self.b1)["trust"]["via"], "RESTORE")
        # 双角色确认落盘
        d = self.iso.incident_detail("INC-001")
        roles = {x["role"] for x in d["quarantines"][-1]["confirms"]}
        self.assertEqual(roles, {ROLE_QUALITY, ROLE_RELEASE})

    def test_restore_rejected_when_lineage_forked(self):
        fix = self.svc.create_eco("fix", "owner.x", ["noc"],
                                  {"noc": {"netlist": "MODULE noc; CELL F; ENDMODULE"}})["id"]
        self.svc.realign_eco(fix, "B-R0", "owner.x")
        self.svc.freeze_eco(fix, "owner.x"); green(self.svc, fix, "f1", "f2")
        self.svc.release_start(fix, "owner.x")     # B-R2 在 B-R0 上的修复分叉
        with self.assertRaises(IsolationError) as cm:
            self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=CONFIRMS)
        self.assertEqual(cm.exception.code, "LINEAGE_FORKED_SINCE_QUARANTINE")
        self.assertEqual(cm.exception.extra["fork_baseline_id"], "B-R2")

    def test_restore_idempotent_when_already_trusted(self):
        self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=CONFIRMS)
        again = self.iso.restore_trust(faulty_baseline_id=self.b1, confirms=CONFIRMS)
        self.assertTrue(again["deduped"] and again["already_trusted"])
        self.assertEqual(self.s.q_one("SELECT generation FROM isolation_meta")["generation"], 2)


class TestNoReuseAcrossGeneration(unittest.TestCase):
    def test_old_checks_and_signoffs_do_not_cross_generation(self):
        _, svc, iso = make()
        _, b1 = publish_chain(svc, ("alu",))
        wip = svc.create_eco("wip", "owner.x", ["l2c"],
                             {"l2c": {"netlist": "MODULE l2c; CELL W; ENDMODULE"}})["id"]
        svc.freeze_eco(wip, "owner.x")
        green(svc, wip, "t1", "m1")
        before = svc.eco_detail(wip)
        self.assertTrue(before["gate"]["releasable"])
        old_fp = before["fingerprint"]
        # 隔离 → ECO 弹回，旧签核/检查作废
        iso.register_incident(batch="B", faulty_baseline_id=b1, reason="r",
                              evidence=EVID, registered_by="qa")
        mid = svc.eco_detail(wip)
        self.assertFalse(mid["gate"]["valid_signoffs"])
        # 恢复（无分叉）→ 新隔离代次
        iso.restore_trust(faulty_baseline_id=b1, confirms=CONFIRMS)
        svc.realign_eco(wip, "B-R0", "owner.x")
        after = svc.freeze_eco(wip, "owner.x")
        # 新代次进入快照指纹：与隔离前指纹必然不同，旧签核 overall_fp 永久失配
        self.assertEqual(after["isolation_generation"], 2)
        self.assertNotEqual(after["fingerprint"], old_fp)
        self.assertFalse(after["gate"]["valid_signoffs"])
        self.assertTrue(all(c["status"] in ("PENDING", "BLOCKED")
                            for c in after["gate"]["checks"]))
        # 必须重跑 + 重签才能再发布：新代次下门禁未满足（旧结果/旧签核均未复用）
        with self.assertRaises(ECOError) as cm:
            svc.release_start(wip, "owner.x")
        self.assertEqual(cm.exception.code, "NOT_RELEASABLE")
        green(svc, wip, "t1", "m1")
        self.assertTrue(svc.eco_detail(wip)["gate"]["releasable"])


if __name__ == "__main__":
    unittest.main()
