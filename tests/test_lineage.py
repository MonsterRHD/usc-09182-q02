"""谱系服务核心场景测试：覆盖传承人复核的完整流程。"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from service.domain import (
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Conflict,
    PermissionDenied,
    ValidationError,
)
from service.lineage import LineageService
from service.store import EventStore, MemoryStore

STEPS_V1 = [
    {"name": "和泥", "detail": "黄河胶泥醒泥三日"},
    {"name": "塑形", "detail": "手捏斑鸠身"},
    {"name": "打孔", "detail": "音孔两孔，孔径三毫米"},
    {"name": "上色", "detail": "锅底黑打底，点红绿彩"},
]


def build(store=None):
    """搭好两作坊、传承人、两位师傅、一名学徒与同名造型。"""
    svc = LineageService(store or MemoryStore())
    w1, _ = svc.register_workshop(name="东街作坊")
    w2, _ = svc.register_workshop(name="西巷作坊")
    fx = SimpleNamespace(svc=svc, w1=w1, w2=w2)
    fx.inheritor, _ = svc.register_person(
        name="张传承", role="inheritor", workshop_id=w1["id"])
    fx.master_a, _ = svc.register_person(
        name="王师傅", role="master", workshop_id=w1["id"])
    fx.master_b, _ = svc.register_person(
        name="李师傅", role="master", workshop_id=w1["id"])
    fx.apprentice, _ = svc.register_person(
        name="小赵", role="apprentice", workshop_id=w1["id"])
    fx.outsider, _ = svc.register_person(
        name="陈师傅", role="master", workshop_id=w2["id"])
    svc.link_lineage(master_id=fx.master_a["id"], apprentice_id=fx.apprentice["id"])
    fx.shape1, _ = svc.register_shape(name="斑鸠", workshop_id=w1["id"])
    fx.shape2, _ = svc.register_shape(name="斑鸠", workshop_id=w2["id"])  # 同名不同作坊
    fx.v1, _ = svc.submit_version(
        shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
        photo_summary="sha256:v1-photos", steps=STEPS_V1,
        materials=["黄河胶泥"], note="祖传底样")
    svc.confirm_version(version_id=fx.v1["id"], confirmer_id=fx.inheritor["id"])
    return fx


class ParallelEditTest(unittest.TestCase):
    """复核场景一：两位师傅并行修改同一造型。"""

    def setUp(self):
        self.fx = build()

    def test_parallel_edits_fork_branches_without_overwrite(self):
        fx = self.fx
        # 两位师傅基于同一版本 v1 并行修改
        v2, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v2-photos",
            steps=STEPS_V1[:3] + [{"name": "上色", "detail": "改施透明釉"}],
            materials=["黄河胶泥"], base_version_id=fx.v1["id"])
        v3, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_b["id"],
            photo_summary="sha256:v3-photos",
            steps=STEPS_V1[:2] + [
                {"name": "打孔", "detail": "音孔三孔，音色更亮"},
                {"name": "上色", "detail": "保留传统黑底"}],
            materials=["黄河胶泥", "细沙"], base_version_id=fx.v1["id"])

        # 自动分叉，互不覆盖
        self.assertEqual(v2["branch_id"], "main")
        self.assertEqual(v3["branch_id"], "fork-1")

        # 两条分支均可由传承人独立确认
        fx.svc.confirm_version(version_id=v2["id"], confirmer_id=fx.inheritor["id"])
        fx.svc.confirm_version(version_id=v3["id"], confirmer_id=fx.inheritor["id"])
        self.assertEqual(fx.svc.version_view(v2["id"])["status"], STATUS_CONFIRMED)
        self.assertEqual(fx.svc.version_view(v3["id"])["status"], STATUS_CONFIRMED)

        # 引用链：v3 的谱系为 v1 -> v3，落在 fork-1 分支
        chain = fx.svc.citation_chain(v3["id"])
        self.assertEqual([c["version_id"] for c in chain["chain"]],
                         [fx.v1["id"], v3["id"]])
        self.assertEqual(chain["chain"][-1]["branch_id"], "fork-1")
        self.assertEqual(chain["workshop_id"], fx.w1["id"])

        # 造型视图保留全部分支头
        shape = fx.svc.shape_view(fx.shape1["id"])
        self.assertEqual(shape["branches"], {"main": v2["id"], "fork-1": v3["id"]})

    def test_submit_by_apprentice_or_outsider_denied(self):
        fx = self.fx
        with self.assertRaises(PermissionDenied):
            fx.svc.submit_version(
                shape_id=fx.shape1["id"], author_id=fx.apprentice["id"],
                photo_summary="x", steps=STEPS_V1, base_version_id=fx.v1["id"])
        with self.assertRaises(PermissionDenied):
            fx.svc.submit_version(
                shape_id=fx.shape1["id"], author_id=fx.outsider["id"],
                photo_summary="x", steps=STEPS_V1, base_version_id=fx.v1["id"])

    def test_confirm_requires_local_inheritor(self):
        fx = self.fx
        v2, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v2", steps=STEPS_V1, base_version_id=fx.v1["id"])
        with self.assertRaises(PermissionDenied):
            fx.svc.confirm_version(version_id=v2["id"], confirmer_id=fx.master_a["id"])
        with self.assertRaises(PermissionDenied):
            fx.svc.confirm_version(version_id=v2["id"], confirmer_id=fx.outsider["id"])


class CorrectionTest(unittest.TestCase):
    """复核场景二：旧版纠正不覆盖历史。"""

    def test_correction_keeps_old_version(self):
        fx = build()
        v2, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v2", steps=STEPS_V1, base_version_id=fx.v1["id"])
        fx.svc.confirm_version(version_id=v2["id"], confirmer_id=fx.inheritor["id"])
        # 发现 v2 音孔描述有误，提交纠正版本
        v4, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v4",
            steps=STEPS_V1[:2] + [
                {"name": "打孔", "detail": "音孔两孔，孔径四毫米（纠正）"},
                {"name": "上色", "detail": "传统黑底"}],
            materials=["黄河胶泥"], base_version_id=v2["id"],
            corrects_version_id=v2["id"])
        fx.svc.confirm_version(version_id=v4["id"], confirmer_id=fx.inheritor["id"])

        # 旧版仍在，状态与内容不变
        old = fx.svc.version_view(v2["id"])
        self.assertEqual(old["status"], STATUS_CONFIRMED)
        self.assertEqual(old["corrections"], [v4["id"]])
        # 引用链完整：v1 -> v2 -> v4，v2 节点标注了纠正关系
        chain = fx.svc.citation_chain(v4["id"])
        self.assertEqual([c["version_id"] for c in chain["chain"]],
                         [fx.v1["id"], v2["id"], v4["id"]])
        self.assertEqual(chain["chain"][1]["corrected_by"], [v4["id"]])
        self.assertEqual(chain["chain"][2]["corrects_version_id"], v2["id"])


class PromotionTest(unittest.TestCase):
    """复核场景三：学徒跨阶段晋级。"""

    def test_cross_stage_promotion_requires_inheritor_with_reason(self):
        fx = build()
        # 师傅不能跨阶段批准
        with self.assertRaises(PermissionDenied):
            fx.svc.promote_apprentice(
                person_id=fx.apprentice["id"], to_stage=2,
                approved_by=fx.master_a["id"], reason="天赋好")
        # 传承人跨阶段批准必须说明理由
        with self.assertRaises(ValidationError):
            fx.svc.promote_apprentice(
                person_id=fx.apprentice["id"], to_stage=2,
                approved_by=fx.inheritor["id"])
        person, _ = fx.svc.promote_apprentice(
            person_id=fx.apprentice["id"], to_stage=2,
            approved_by=fx.inheritor["id"], reason="已能独立完成全套工序")
        self.assertEqual(person["stage_name"], "进阶")
        self.assertEqual(person["stage_history"][0]["cross_stage"], True)
        self.assertEqual(person["stage_history"][0]["from_stage"], 0)
        # 晋级历史入审计
        types = [e["type"] for e in fx.svc.audit_log(requester_id=fx.inheritor["id"])]
        self.assertIn("apprentice_promoted", types)

    def test_single_stage_promotion_by_own_master(self):
        fx = build()
        person, _ = fx.svc.promote_apprentice(
            person_id=fx.apprentice["id"], to_stage=1,
            approved_by=fx.master_a["id"])
        self.assertEqual(person["stage_name"], "基础")
        # 非其师傅不能批准逐阶晋级
        with self.assertRaises(PermissionDenied):
            fx.svc.promote_apprentice(
                person_id=fx.apprentice["id"], to_stage=2,
                approved_by=fx.master_b["id"])

    def test_apprentice_stage_gates_reading(self):
        fx = build()
        # 入门阶段不可见内部版本
        d = fx.svc.explain(version_id=fx.v1["id"], person_id=fx.apprentice["id"])
        self.assertFalse(d["allowed"])
        # 晋到基础后可见本作坊已确认版本
        fx.svc.promote_apprentice(
            person_id=fx.apprentice["id"], to_stage=1,
            approved_by=fx.master_a["id"])
        d = fx.svc.explain(version_id=fx.v1["id"], person_id=fx.apprentice["id"])
        self.assertTrue(d["allowed"])


class LicenseTest(unittest.TestCase):
    """复核场景四：回收公开许可，只影响未来展示，审计保留最小事实。"""

    def test_revoke_blocks_future_display_but_keeps_audit(self):
        fx = build()
        lic, _ = fx.svc.grant_license(
            shape_id=fx.shape1["id"], scope="网络展示",
            granted_by=fx.inheritor["id"])
        # 授权期间公众可见
        self.assertTrue(fx.svc.explain(version_id=fx.v1["id"])["allowed"])
        self.assertEqual(fx.svc.public_version_view(fx.v1["id"])["public_scopes"],
                         ["网络展示"])

        fx.svc.revoke_license(license_id=lic["id"], revoked_by=fx.inheritor["id"],
                              reason="图样涉及祭仪，暂缓公开")
        # 撤回后公众不可见，理由可解释
        d = fx.svc.explain(version_id=fx.v1["id"])
        self.assertFalse(d["allowed"])
        self.assertTrue(any("撤回" in r for r in d["reasons"]))
        with self.assertRaises(PermissionDenied):
            fx.svc.public_version_view(fx.v1["id"])
        # 内部师傅不受影响
        self.assertTrue(fx.svc.explain(
            version_id=fx.v1["id"], person_id=fx.master_a["id"])["allowed"])
        # 审计保留授权与撤回两条最小事实
        audit = fx.svc.audit_log(requester_id=fx.inheritor["id"])
        types = [e["type"] for e in audit]
        self.assertIn("license_granted", types)
        self.assertIn("license_revoked", types)
        # 重复撤回报错（除非走幂等键）
        with self.assertRaises(Conflict):
            fx.svc.revoke_license(license_id=lic["id"],
                                  revoked_by=fx.inheritor["id"])

    def test_audit_requires_inheritor(self):
        fx = build()
        with self.assertRaises(PermissionDenied):
            fx.svc.audit_log(requester_id=fx.master_a["id"])


class ForbiddenPatternTest(unittest.TestCase):
    """禁用图样：不删历史，只影响公开展示。"""

    def test_forbidden_pattern_blocks_public_display(self):
        fx = build()
        fx.svc.forbid_pattern(motif="饕餮", reason="涉及外姓祭仪",
                              banned_by=fx.inheritor["id"])
        v5, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v5",
            steps=STEPS_V1[:3] + [{"name": "绘纹", "detail": "腹部绘饕餮纹"}],
            materials=["黄河胶泥"], base_version_id=fx.v1["id"])
        fx.svc.confirm_version(version_id=v5["id"], confirmer_id=fx.inheritor["id"])
        fx.svc.grant_license(shape_id=fx.shape1["id"], scope="展览",
                             granted_by=fx.inheritor["id"])
        # 有许可仍因禁用图样被拒，理由可解释
        d = fx.svc.explain(version_id=v5["id"])
        self.assertFalse(d["allowed"])
        self.assertTrue(any("禁用图样" in r for r in d["reasons"]))
        # 内部仍可见（带警告），历史版本未受影响
        internal = fx.svc.explain(version_id=v5["id"], person_id=fx.master_a["id"])
        self.assertTrue(internal["allowed"])
        self.assertTrue(internal["warnings"])
        self.assertTrue(fx.svc.explain(version_id=fx.v1["id"])["allowed"])
        # 非传承人不能禁用图样
        with self.assertRaises(PermissionDenied):
            fx.svc.forbid_pattern(motif="云纹", reason="", banned_by=fx.master_a["id"])


class CitationTest(unittest.TestCase):
    """引用谱系须标明版本与适用作坊。"""

    def test_citation_requires_matching_workshop(self):
        fx = build()
        # 作坊不符 → 拒绝
        with self.assertRaises(ValidationError):
            fx.svc.cite(version_id=fx.v1["id"], workshop_id=fx.w2["id"],
                        purpose="出版", cited_by=fx.master_a["id"])
        # 标明正确作坊 → 记录
        c, _ = fx.svc.cite(version_id=fx.v1["id"], workshop_id=fx.w1["id"],
                           purpose="出版图录", cited_by=fx.master_a["id"])
        self.assertEqual(c["version_id"], fx.v1["id"])
        self.assertEqual(c["workshop_id"], fx.w1["id"])

    def test_cannot_cite_pending_version(self):
        fx = build()
        v2, _ = fx.svc.submit_version(
            shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
            photo_summary="sha256:v2", steps=STEPS_V1, base_version_id=fx.v1["id"])
        self.assertEqual(v2["status"], STATUS_PENDING)
        with self.assertRaises(ValidationError):
            fx.svc.cite(version_id=v2["id"], workshop_id=fx.w1["id"],
                        purpose="展览", cited_by=fx.master_a["id"])

    def test_same_name_shapes_are_distinct_per_workshop(self):
        fx = build()
        self.assertNotEqual(fx.shape1["id"], fx.shape2["id"])
        # 西巷作坊的同名造型独立演进
        w2v, _ = fx.svc.submit_version(
            shape_id=fx.shape2["id"], author_id=fx.outsider["id"],
            photo_summary="sha256:w2v1",
            steps=[{"name": "打孔", "detail": "音孔一孔"}], materials=["红胶泥"])
        chain = fx.svc.citation_chain(w2v["id"])
        self.assertEqual(chain["workshop_id"], fx.w2["id"])
        self.assertEqual(len(chain["chain"]), 1)


class IdempotencyTest(unittest.TestCase):
    """重复提交与离线同步幂等。"""

    def test_duplicate_submission_replays(self):
        fx = build()
        payload = dict(shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
                       photo_summary="sha256:v2", steps=STEPS_V1,
                       base_version_id=fx.v1["id"])
        first, replayed1 = fx.svc.submit_version(idempotency_key="k-v2", **payload)
        second, replayed2 = fx.svc.submit_version(idempotency_key="k-v2", **payload)
        self.assertFalse(replayed1)
        self.assertTrue(replayed2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(fx.svc.versions), 2)  # v1 + v2，无重复
        # 同一幂等键换操作 → 冲突
        with self.assertRaises(Conflict):
            fx.svc.confirm_version(version_id=first["id"],
                                   confirmer_id=fx.inheritor["id"],
                                   idempotency_key="k-v2")

    def test_offline_sync_is_idempotent(self):
        fx = build()
        ops = [
            {"op": "submit_version", "idempotency_key": "op-1",
             "payload": {"shape_id": fx.shape1["id"], "author_id": fx.master_b["id"],
                         "photo_summary": "sha256:offline-1", "steps": STEPS_V1,
                         "base_version_id": fx.v1["id"]}},
            {"op": "promote_apprentice", "idempotency_key": "op-2",
             "payload": {"person_id": fx.apprentice["id"], "to_stage": 1,
                         "approved_by": fx.master_a["id"]}},
        ]
        r1, replayed1 = fx.svc.sync(device_id="kiln-pad", batch_id="b-1", ops=ops)
        self.assertFalse(replayed1)
        self.assertTrue(all(r["ok"] for r in r1["results"]))
        # 同批次重传 → 整体回放，状态不变
        r2, replayed2 = fx.svc.sync(device_id="kiln-pad", batch_id="b-1", ops=ops)
        self.assertTrue(replayed2)
        self.assertEqual(r1, r2)
        self.assertEqual(len(fx.svc.versions), 2)
        self.assertEqual(fx.svc.person_view(fx.apprentice["id"])["stage"], 1)
        # 换批次号但操作键相同 → 单操作级回放
        r3, _ = fx.svc.sync(device_id="kiln-pad", batch_id="b-2", ops=ops)
        self.assertTrue(all(r["replayed"] for r in r3["results"]))
        self.assertEqual(len(fx.svc.versions), 2)
        # 批次内个别失败不影响其他操作
        bad = [{"op": "confirm_version", "idempotency_key": "op-9",
                "payload": {"version_id": "ver_none", "confirmer_id": fx.inheritor["id"]}},
               {"op": "register_workshop", "idempotency_key": "op-10",
                "payload": {"name": "南坡作坊"}}]
        r4, _ = fx.svc.sync(device_id="kiln-pad", batch_id="b-3", ops=bad)
        self.assertFalse(r4["results"][0]["ok"])
        self.assertTrue(r4["results"][1]["ok"])


class PersistenceTest(unittest.TestCase):
    """事件日志可重建状态，幂等键跨重启有效。"""

    def test_reload_from_event_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lineage.jsonl")
            fx = build(EventStore(path))
            fx.svc.submit_version(
                shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
                photo_summary="sha256:v2", steps=STEPS_V1,
                base_version_id=fx.v1["id"], idempotency_key="k-persist")
            svc2 = LineageService(EventStore(path))
            self.assertEqual(len(svc2.versions), 2)
            self.assertEqual(svc2.version_view(fx.v1["id"])["status"], STATUS_CONFIRMED)
            # 重启后同一幂等键仍回放
            _, replayed = svc2.submit_version(
                shape_id=fx.shape1["id"], author_id=fx.master_a["id"],
                photo_summary="sha256:v2", steps=STEPS_V1,
                base_version_id=fx.v1["id"], idempotency_key="k-persist")
            self.assertTrue(replayed)
            self.assertEqual(len(svc2.versions), 2)


if __name__ == "__main__":
    unittest.main()
