"""泥咕咕技艺谱系核心服务。

设计要点：
- 一切变更先写追加日志（事件），再更新内存投影，历史永不覆盖；
- 版本由师傅提交、本作坊传承人确认；旧版纠正以新版本引用旧版，旧版保留；
- 并行修改同一基线版本时自动分叉工艺分支，互不覆盖；
- 公开许可可撤回，撤回只影响今后的公开展示，审计保留最小事实；
- 所有写操作支持幂等键，离线同步按批次与单操作两级幂等回放。
"""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from .domain import (
    MAIN_BRANCH,
    ROLE_APPRENTICE,
    ROLE_INHERITOR,
    ROLE_MASTER,
    ROLES,
    STAGES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Citation,
    Conflict,
    DomainError,
    ForbiddenPattern,
    License,
    Lineage,
    NotFound,
    PermissionDenied,
    Person,
    Shape,
    ValidationError,
    Version,
    Workshop,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LineageService:
    """谱系服务门面：写操作经 _execute 包裹（幂等），读操作直接查投影。"""

    # 离线同步允许的操作名 -> 服务方法名
    SYNC_OPS = {
        "register_workshop": "register_workshop",
        "register_person": "register_person",
        "link_lineage": "link_lineage",
        "register_shape": "register_shape",
        "submit_version": "submit_version",
        "confirm_version": "confirm_version",
        "forbid_pattern": "forbid_pattern",
        "promote_apprentice": "promote_apprentice",
        "grant_license": "grant_license",
        "revoke_license": "revoke_license",
        "cite": "cite",
    }

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self.workshops: dict[str, Workshop] = {}
        self.persons: dict[str, Person] = {}
        self.lineages: dict[str, Lineage] = {}
        self.shapes: dict[str, Shape] = {}
        self.versions: dict[str, Version] = {}
        self.patterns: dict[str, ForbiddenPattern] = {}
        self.licenses: dict[str, License] = {}
        self.citations: dict[str, Citation] = {}
        self.idempotency: dict[str, dict] = {}
        self.audit: list[dict] = []
        self._seq = 0
        for rec in self.store.load():
            self._apply(rec)

    # ------------------------------------------------------------------
    # 基础设施：事件、幂等、查找
    # ------------------------------------------------------------------
    def _emit(self, etype: str, *, actor: str, summary: str, **fields) -> None:
        rec = {
            "kind": "event",
            "type": etype,
            "at": _now(),
            "actor": actor,
            "summary": summary,
            **fields,
        }
        self.store.append(rec)
        self._apply(rec)

    def _apply(self, rec: dict) -> None:
        if rec.get("kind") == "idem":
            self.idempotency[rec["key"]] = {"op": rec["op"], "result": rec["result"]}
            return
        self._seq += 1
        self.audit.append({
            "seq": self._seq,
            "type": rec["type"],
            "at": rec["at"],
            "actor": rec.get("actor"),
            "summary": rec.get("summary", ""),
        })
        proj = getattr(self, f"_proj_{rec['type']}", None)
        if proj:
            proj(rec)

    def _execute(self, op: str, key, fn):
        """幂等执行：同一 key 重复调用返回首次结果，不产生新事件。"""
        with self._lock:
            if key:
                stored = self.idempotency.get(key)
                if stored:
                    if stored["op"] != op:
                        raise Conflict(f"幂等键 {key} 已用于操作 {stored['op']}")
                    return copy.deepcopy(stored["result"]), True
            result = fn()
            if key:
                rec = {"kind": "idem", "key": key, "op": op, "result": result}
                self.store.append(rec)
                self.idempotency[key] = {"op": op, "result": result}
            return copy.deepcopy(result), False

    def _workshop(self, wid: str) -> Workshop:
        w = self.workshops.get(wid)
        if not w:
            raise NotFound(f"作坊不存在: {wid}")
        return w

    def _person(self, pid: str) -> Person:
        p = self.persons.get(pid)
        if not p:
            raise NotFound(f"人员不存在: {pid}")
        return p

    def _shape(self, sid: str) -> Shape:
        s = self.shapes.get(sid)
        if not s:
            raise NotFound(f"造型不存在: {sid}")
        return s

    def _version(self, vid: str) -> Version:
        v = self.versions.get(vid)
        if not v:
            raise NotFound(f"版本不存在: {vid}")
        return v

    def _license(self, lid: str) -> License:
        lic = self.licenses.get(lid)
        if not lic:
            raise NotFound(f"许可不存在: {lid}")
        return lic

    @staticmethod
    def _stage_index(stage) -> int:
        if isinstance(stage, bool):
            raise ValidationError("阶段取值非法")
        if isinstance(stage, int):
            idx = stage
        elif isinstance(stage, str) and stage in STAGES:
            idx = STAGES.index(stage)
        else:
            raise ValidationError(f"未知学徒阶段: {stage!r}，可选 {STAGES}")
        if not 0 <= idx < len(STAGES):
            raise ValidationError(f"阶段下标超出范围: {idx}")
        return idx

    # ------------------------------------------------------------------
    # 投影：事件 -> 内存状态
    # ------------------------------------------------------------------
    def _proj_workshop_registered(self, rec):
        self.workshops[rec["workshop"]["id"]] = Workshop(**rec["workshop"])

    def _proj_person_registered(self, rec):
        self.persons[rec["person"]["id"]] = Person(**rec["person"])

    def _proj_lineage_linked(self, rec):
        self.lineages[rec["lineage"]["id"]] = Lineage(**rec["lineage"])

    def _proj_shape_registered(self, rec):
        self.shapes[rec["shape"]["id"]] = Shape(**rec["shape"])

    def _proj_version_submitted(self, rec):
        v = Version(**rec["version"])
        self.versions[v.id] = v
        shape = self.shapes[v.shape_id]
        shape.branches[v.branch_id] = v.id
        shape.fork_count = rec["fork_count"]

    def _proj_version_confirmed(self, rec):
        v = self.versions[rec["version_id"]]
        v.status = STATUS_CONFIRMED
        v.confirmed_by = rec["confirmer_id"]
        v.confirmed_at = rec["confirmed_at"]

    def _proj_pattern_forbidden(self, rec):
        self.patterns[rec["pattern"]["id"]] = ForbiddenPattern(**rec["pattern"])

    def _proj_apprentice_promoted(self, rec):
        p = self.persons[rec["person_id"]]
        p.stage = rec["to_stage"]
        p.stage_history.append({
            "from_stage": rec["from_stage"],
            "to_stage": rec["to_stage"],
            "approved_by": rec["approved_by"],
            "reason": rec.get("reason", ""),
            "cross_stage": rec["cross_stage"],
            "at": rec["at"],
        })

    def _proj_license_granted(self, rec):
        self.licenses[rec["license"]["id"]] = License(**rec["license"])

    def _proj_license_revoked(self, rec):
        lic = self.licenses[rec["license_id"]]
        lic.revoked_by = rec["revoked_by"]
        lic.revoked_at = rec["revoked_at"]
        lic.revoke_reason = rec.get("reason", "")

    def _proj_citation_recorded(self, rec):
        self.citations[rec["citation"]["id"]] = Citation(**rec["citation"])

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def register_workshop(self, *, name, idempotency_key=None):
        return self._execute("register_workshop", idempotency_key,
                             lambda: self._register_workshop(name=name))

    def _register_workshop(self, *, name):
        if not name:
            raise ValidationError("作坊名称不能为空")
        w = Workshop(id=_new_id("ws"), name=name, created_at=_now())
        self._emit("workshop_registered", actor="system",
                   summary=f"登记作坊「{name}」", workshop=asdict(w))
        return asdict(w)

    def register_person(self, *, name, role, workshop_id, stage=0, idempotency_key=None):
        return self._execute("register_person", idempotency_key,
                             lambda: self._register_person(
                                 name=name, role=role, workshop_id=workshop_id, stage=stage))

    def _register_person(self, *, name, role, workshop_id, stage=0):
        self._workshop(workshop_id)
        if role not in ROLES:
            raise ValidationError(f"未知角色: {role}，可选 {ROLES}")
        if not name:
            raise ValidationError("姓名不能为空")
        stage = self._stage_index(stage) if role == ROLE_APPRENTICE else 0
        p = Person(id=_new_id("p"), name=name, role=role,
                   workshop_id=workshop_id, stage=stage, created_at=_now())
        self._emit("person_registered", actor="system",
                   summary=f"登记人员「{name}」（{role}）", person=asdict(p))
        return self.person_view(p.id)

    def link_lineage(self, *, master_id, apprentice_id, idempotency_key=None):
        return self._execute("link_lineage", idempotency_key,
                             lambda: self._link_lineage(
                                 master_id=master_id, apprentice_id=apprentice_id))

    def _link_lineage(self, *, master_id, apprentice_id):
        m = self._person(master_id)
        a = self._person(apprentice_id)
        if m.role not in (ROLE_MASTER, ROLE_INHERITOR):
            raise ValidationError("传授方须为师傅或传承人")
        if a.role != ROLE_APPRENTICE:
            raise ValidationError("被传授方须为学徒")
        if m.workshop_id != a.workshop_id:
            raise ValidationError("师承关系须在同一作坊内")
        if any(l.master_id == master_id and l.apprentice_id == apprentice_id
               for l in self.lineages.values()):
            raise Conflict("师承关系已存在")
        rel = Lineage(id=_new_id("lin"), master_id=master_id,
                      apprentice_id=apprentice_id, created_at=_now())
        self._emit("lineage_linked", actor=master_id,
                   summary=f"{m.name} 收 {a.name} 为徒", lineage=asdict(rel))
        return asdict(rel)

    def register_shape(self, *, name, workshop_id, idempotency_key=None):
        return self._execute("register_shape", idempotency_key,
                             lambda: self._register_shape(name=name, workshop_id=workshop_id))

    def _register_shape(self, *, name, workshop_id):
        self._workshop(workshop_id)
        if not name:
            raise ValidationError("造型名称不能为空")
        # 同名造型允许存在于不同作坊；同一作坊内名称唯一
        if any(s.name == name and s.workshop_id == workshop_id for s in self.shapes.values()):
            raise Conflict(f"作坊内已存在造型「{name}」")
        s = Shape(id=_new_id("shape"), name=name, workshop_id=workshop_id, created_at=_now())
        self._emit("shape_registered", actor="system",
                   summary=f"登记造型「{name}」", shape=asdict(s))
        return asdict(s)

    # ------------------------------------------------------------------
    # 版本：提交、确认、纠正（历史不可覆盖）
    # ------------------------------------------------------------------
    def submit_version(self, *, idempotency_key=None, **payload):
        return self._execute("submit_version", idempotency_key,
                             lambda: self._submit_version(**payload))

    def _submit_version(self, *, shape_id, author_id, photo_summary, steps,
                        materials=None, note="", base_version_id=None,
                        corrects_version_id=None):
        shape = self._shape(shape_id)
        author = self._person(author_id)
        if author.workshop_id != shape.workshop_id:
            raise PermissionDenied("仅本作坊成员可提交该造型的版本")
        if author.role not in (ROLE_MASTER, ROLE_INHERITOR):
            raise PermissionDenied("学徒不能提交版本，须由师傅提交、传承人确认")
        if not photo_summary:
            raise ValidationError("版本必须附照片摘要")
        if not isinstance(steps, list) or not steps:
            raise ValidationError("版本必须包含工序步骤")
        for st in steps:
            if not isinstance(st, dict) or not st.get("name"):
                raise ValidationError("每道工序须为含 name 的对象")
        materials = list(materials or [])

        fork_count = shape.fork_count
        if not shape.branches:
            if base_version_id:
                raise ValidationError("首个版本没有基线版本")
            branch_id = MAIN_BRANCH
        else:
            if not base_version_id:
                raise ValidationError("必须指定基线版本 base_version_id")
            base = self._version(base_version_id)
            if base.shape_id != shape.id:
                raise ValidationError("基线版本不属于该造型")
            if shape.branches.get(base.branch_id) == base.id:
                branch_id = base.branch_id          # 延续原分支
            else:
                fork_count += 1                     # 并行修改 → 分叉新分支
                branch_id = f"fork-{fork_count}"

        if corrects_version_id:
            corrected = self._version(corrects_version_id)
            if corrected.shape_id != shape.id:
                raise ValidationError("被纠正版本不属于该造型")

        v = Version(
            id=_new_id("ver"), shape_id=shape.id, author_id=author.id,
            branch_id=branch_id, base_version_id=base_version_id,
            corrects_version_id=corrects_version_id,
            photo_summary=photo_summary, steps=steps, materials=materials,
            note=note or "", status=STATUS_PENDING, submitted_at=_now(),
        )
        summary = f"{author.name} 提交「{shape.name}」版本（分支 {branch_id}）"
        if corrects_version_id:
            summary += f"，纠正旧版 {corrects_version_id}"
        self._emit("version_submitted", actor=author.id, summary=summary,
                   version=asdict(v), fork_count=fork_count)
        return self.version_view(v.id)

    def confirm_version(self, *, idempotency_key=None, **payload):
        return self._execute("confirm_version", idempotency_key,
                             lambda: self._confirm_version(**payload))

    def _confirm_version(self, *, version_id, confirmer_id):
        v = self._version(version_id)
        shape = self._shape(v.shape_id)
        confirmer = self._person(confirmer_id)
        if confirmer.role != ROLE_INHERITOR or confirmer.workshop_id != shape.workshop_id:
            raise PermissionDenied("须由本作坊指定传承人确认版本")
        if v.status != STATUS_PENDING:
            raise Conflict("版本已确认，不可重复确认")
        self._emit("version_confirmed", actor=confirmer_id,
                   summary=f"{confirmer.name} 确认版本 {version_id}",
                   version_id=version_id, confirmer_id=confirmer_id,
                   confirmed_at=_now())
        return self.version_view(version_id)

    # ------------------------------------------------------------------
    # 禁用图样
    # ------------------------------------------------------------------
    def forbid_pattern(self, *, idempotency_key=None, **payload):
        return self._execute("forbid_pattern", idempotency_key,
                             lambda: self._forbid_pattern(**payload))

    def _forbid_pattern(self, *, motif, reason, banned_by):
        banner = self._person(banned_by)
        if banner.role != ROLE_INHERITOR:
            raise PermissionDenied("仅传承人可禁用图样")
        if not motif:
            raise ValidationError("图样不能为空")
        if any(p.motif == motif for p in self.patterns.values()):
            raise Conflict(f"图样「{motif}」已在禁用列表")
        p = ForbiddenPattern(id=_new_id("pat"), motif=motif, reason=reason or "",
                             banned_by=banned_by, banned_at=_now())
        self._emit("pattern_forbidden", actor=banned_by,
                   summary=f"禁用图样「{motif}」", pattern=asdict(p))
        return asdict(p)

    # ------------------------------------------------------------------
    # 学徒阶段
    # ------------------------------------------------------------------
    def promote_apprentice(self, *, idempotency_key=None, **payload):
        return self._execute("promote_apprentice", idempotency_key,
                             lambda: self._promote_apprentice(**payload))

    def _promote_apprentice(self, *, person_id, to_stage, approved_by, reason=""):
        p = self._person(person_id)
        if p.role != ROLE_APPRENTICE:
            raise ValidationError("仅学徒有阶段可晋级")
        to_idx = self._stage_index(to_stage)
        approver = self._person(approved_by)
        if approver.workshop_id != p.workshop_id:
            raise PermissionDenied("须由本作坊的师傅或传承人批准晋级")
        delta = to_idx - p.stage
        if delta <= 0:
            raise ValidationError("只能向更高阶段晋级")
        cross = delta > 1
        if cross:
            if approver.role != ROLE_INHERITOR:
                raise PermissionDenied("跨阶段晋级须由传承人批准")
            if not reason:
                raise ValidationError("跨阶段晋级须说明理由")
        else:
            linked = any(l.master_id == approver.id and l.apprentice_id == p.id
                         for l in self.lineages.values())
            if not (approver.role == ROLE_INHERITOR
                    or (approver.role == ROLE_MASTER and linked)):
                raise PermissionDenied("逐阶晋级须由其师傅或传承人批准")
        self._emit("apprentice_promoted", actor=approved_by,
                   summary=f"{p.name} 由「{STAGES[p.stage]}」晋至「{STAGES[to_idx]}」"
                           + ("（跨阶段）" if cross else ""),
                   person_id=person_id, from_stage=p.stage, to_stage=to_idx,
                   approved_by=approved_by, reason=reason or "", cross_stage=cross)
        return self.person_view(person_id)

    # ------------------------------------------------------------------
    # 公开许可（可公开范围）
    # ------------------------------------------------------------------
    def grant_license(self, *, idempotency_key=None, **payload):
        return self._execute("grant_license", idempotency_key,
                             lambda: self._grant_license(**payload))

    def _grant_license(self, *, shape_id, scope, granted_by, version_id=None):
        shape = self._shape(shape_id)
        g = self._person(granted_by)
        if g.role != ROLE_INHERITOR or g.workshop_id != shape.workshop_id:
            raise PermissionDenied("仅本作坊传承人可授予公开许可")
        if not scope:
            raise ValidationError("须指明公开范围 scope")
        if version_id:
            v = self._version(version_id)
            if v.shape_id != shape.id:
                raise ValidationError("版本不属于该造型")
            if v.status != STATUS_CONFIRMED:
                raise ValidationError("仅已确认版本可授予公开许可")
        lic = License(id=_new_id("lic"), shape_id=shape.id, version_id=version_id,
                      scope=scope, granted_by=granted_by, granted_at=_now())
        self._emit("license_granted", actor=granted_by,
                   summary=f"授予「{shape.name}」公开许可（{scope}）", license=asdict(lic))
        return asdict(lic)

    def revoke_license(self, *, idempotency_key=None, **payload):
        return self._execute("revoke_license", idempotency_key,
                             lambda: self._revoke_license(**payload))

    def _revoke_license(self, *, license_id, revoked_by, reason=""):
        lic = self._license(license_id)
        shape = self._shape(lic.shape_id)
        r = self._person(revoked_by)
        if r.role != ROLE_INHERITOR or r.workshop_id != shape.workshop_id:
            raise PermissionDenied("仅本作坊传承人可撤回公开许可")
        if not lic.active:
            raise Conflict("许可已撤回")
        # 撤回只影响今后的展示；授权与撤回两条最小事实均留在审计日志
        self._emit("license_revoked", actor=revoked_by,
                   summary=f"撤回公开许可 {license_id}（{lic.scope}）",
                   license_id=license_id, revoked_by=revoked_by,
                   revoked_at=_now(), reason=reason or "")
        return asdict(self.licenses[license_id])

    # ------------------------------------------------------------------
    # 引用与引用链
    # ------------------------------------------------------------------
    def cite(self, *, idempotency_key=None, **payload):
        return self._execute("cite", idempotency_key, lambda: self._cite(**payload))

    def _cite(self, *, version_id, workshop_id, purpose, cited_by):
        v = self._version(version_id)
        shape = self._shape(v.shape_id)
        self._workshop(workshop_id)
        self._person(cited_by)
        if shape.workshop_id != workshop_id:
            raise ValidationError(
                "引用须标明版本所属作坊：该版本属于另一作坊，请核对适用作坊")
        if v.status != STATUS_CONFIRMED:
            raise ValidationError("仅可引用已确认版本")
        c = Citation(id=_new_id("cite"), version_id=version_id, workshop_id=workshop_id,
                     purpose=purpose or "", cited_by=cited_by, cited_at=_now())
        self._emit("citation_recorded", actor=cited_by,
                   summary=f"引用「{shape.name}」版本 {version_id}", citation=asdict(c))
        return asdict(c)

    def citation_chain(self, version_id):
        """引用链：从根版本到指定版本的完整谱系，含分支、作坊与纠正关系。"""
        with self._lock:
            v = self._version(version_id)
            shape = self._shape(v.shape_id)
            line = []
            cur = v
            while cur is not None:
                line.append(cur)
                cur = self.versions.get(cur.base_version_id)
            line.reverse()
            corrections_of = {}
            for other in self.versions.values():
                if other.corrects_version_id:
                    corrections_of.setdefault(other.corrects_version_id, []).append(other.id)
            return {
                "shape_id": shape.id,
                "shape_name": shape.name,
                "workshop_id": shape.workshop_id,
                "version_id": v.id,
                "chain": [{
                    "version_id": c.id,
                    "branch_id": c.branch_id,
                    "status": c.status,
                    "author_id": c.author_id,
                    "workshop_id": shape.workshop_id,
                    "corrects_version_id": c.corrects_version_id,
                    "corrected_by": corrections_of.get(c.id, []),
                    "submitted_at": c.submitted_at,
                } for c in line],
            }

    # ------------------------------------------------------------------
    # 权限解释
    # ------------------------------------------------------------------
    def _forbidden_hits(self, v: Version) -> list:
        if not self.patterns:
            return []
        parts = [v.note or "", " ".join(v.materials or [])]
        for step in v.steps or []:
            parts.append(" ".join(str(x) for x in step.values()))
        text = " ".join(parts)
        return [p.motif for p in self.patterns.values() if p.motif and p.motif in text]

    def _covering_licenses(self, shape_id, version_id, *, active):
        out = []
        for lic in self.licenses.values():
            if lic.shape_id != shape_id:
                continue
            if lic.version_id not in (None, version_id):
                continue
            if lic.active == active:
                out.append(lic)
        return out

    @staticmethod
    def _decision(v, person, allowed, scope, reasons, warnings):
        return {
            "version_id": v.id,
            "person_id": person.id if person else None,
            "allowed": allowed,
            "scope": scope,
            "reasons": reasons,
            "warnings": warnings,
        }

    def explain(self, *, version_id, person_id=None):
        """权限解释：给出允许/拒绝及全部理由，供复核时核对。"""
        with self._lock:
            v = self._version(version_id)
            shape = self._shape(v.shape_id)
            hits = self._forbidden_hits(v)
            warnings = [f"含禁用图样：{'、'.join(hits)}"] if hits else []
            person = self._person(person_id) if person_id else None

            if person and person.workshop_id == shape.workshop_id:
                if person.role == ROLE_INHERITOR:
                    return self._decision(v, person, True, "谱系管理",
                                          ["传承人对本作坊谱系有完整查阅权"], warnings)
                if person.role == ROLE_MASTER:
                    return self._decision(v, person, True, "作坊内部",
                                          ["本作坊师傅可查阅全部版本（含待确认）"], warnings)
                if person.role == ROLE_APPRENTICE:
                    if v.status != STATUS_CONFIRMED:
                        return self._decision(v, person, False, "作坊教学",
                                              ["版本未经传承人确认，学徒不可见"], warnings)
                    if person.stage < 1:
                        return self._decision(
                            v, person, False, "作坊教学",
                            [f"学徒阶段「{STAGES[person.stage]}」仅可查阅公开内容"], warnings)
                    return self._decision(
                        v, person, True, "作坊教学",
                        [f"学徒阶段「{STAGES[person.stage]}」可查阅本作坊已确认版本"], warnings)

            # 公众与外部人员：须已确认、无禁用图样、有有效公开许可
            reasons = []
            if v.status != STATUS_CONFIRMED:
                reasons.append("版本未经传承人确认")
            if hits:
                reasons.append(f"版本含禁用图样：{'、'.join(hits)}")
            active = self._covering_licenses(shape.id, v.id, active=True)
            if not active:
                if self._covering_licenses(shape.id, v.id, active=False):
                    reasons.append("公开许可已撤回，撤回仅影响今后的展示")
                else:
                    reasons.append("该版本无有效公开许可")
            if reasons:
                return self._decision(v, person, False, "公开", reasons, warnings)
            scopes = "、".join(sorted({l.scope for l in active}))
            return self._decision(v, person, True, "公开",
                                  [f"持有效公开许可（{scopes}）"], warnings)

    def public_version_view(self, version_id):
        """公开展示视图：仅当公开权限允许时返回可公开内容。"""
        decision = self.explain(version_id=version_id, person_id=None)
        if not decision["allowed"]:
            raise PermissionDenied(
                "该版本当前不可公开展示：" + "；".join(decision["reasons"]))
        with self._lock:
            v = self._version(version_id)
            shape = self._shape(v.shape_id)
            scopes = sorted({l.scope for l in self._covering_licenses(shape.id, v.id, active=True)})
            return {
                "version_id": v.id,
                "shape_id": shape.id,
                "shape_name": shape.name,
                "workshop_id": shape.workshop_id,
                "photo_summary": v.photo_summary,
                "steps": v.steps,
                "materials": v.materials,
                "public_scopes": scopes,
            }

    # ------------------------------------------------------------------
    # 审计与离线同步
    # ------------------------------------------------------------------
    def audit_log(self, *, requester_id):
        """内部审计：全部事件的最小事实（时间、操作者、摘要），含已撤回许可。"""
        requester = self._person(requester_id)
        if requester.role != ROLE_INHERITOR:
            raise PermissionDenied("仅传承人可查阅审计日志")
        with self._lock:
            return list(self.audit)

    def sync(self, *, device_id, batch_id, ops):
        """离线同步：批次幂等 + 单操作幂等，重复上传不产生重复记录。"""
        if not device_id or not batch_id:
            raise ValidationError("离线同步须携带 device_id 与 batch_id")
        if not isinstance(ops, list):
            raise ValidationError("ops 须为操作列表")
        key = f"sync:{device_id}:{batch_id}"
        return self._execute("sync", key, lambda: self._sync_ops(ops))

    def _sync_ops(self, ops):
        results = []
        for op in ops:
            name = op.get("op")
            op_key = op.get("idempotency_key")
            payload = op.get("payload") or {}
            method_name = self.SYNC_OPS.get(name)
            if not method_name:
                results.append({"key": op_key, "ok": False, "error": {
                    "code": "unknown_op", "message": f"未知操作: {name}"}})
                continue
            try:
                method = getattr(self, method_name)
                result, replayed = method(idempotency_key=op_key, **payload)
                results.append({"key": op_key, "ok": True,
                                "replayed": replayed, "result": result})
            except DomainError as e:
                results.append({"key": op_key, "ok": False, "error": {
                    "code": e.code, "message": e.message}})
        return {"results": results}

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def person_view(self, person_id):
        with self._lock:
            p = self._person(person_id)
            d = asdict(p)
            d["stage_name"] = STAGES[p.stage] if p.role == ROLE_APPRENTICE else None
            return d

    def shape_view(self, shape_id):
        with self._lock:
            s = self._shape(shape_id)
            d = asdict(s)
            d["versions"] = [{
                "version_id": v.id,
                "branch_id": v.branch_id,
                "status": v.status,
                "author_id": v.author_id,
                "base_version_id": v.base_version_id,
                "corrects_version_id": v.corrects_version_id,
                "submitted_at": v.submitted_at,
            } for v in sorted(self.versions.values(), key=lambda x: x.submitted_at)
                if v.shape_id == s.id]
            return d

    def version_view(self, version_id):
        with self._lock:
            v = self._version(version_id)
            shape = self._shape(v.shape_id)
            d = asdict(v)
            d["shape_name"] = shape.name
            d["workshop_id"] = shape.workshop_id
            d["forbidden_hits"] = self._forbidden_hits(v)
            d["corrections"] = [o.id for o in self.versions.values()
                                if o.corrects_version_id == v.id]
            return d

    def list_licenses(self, shape_id=None):
        with self._lock:
            return [asdict(l) for l in self.licenses.values()
                    if shape_id is None or l.shape_id == shape_id]

    def list_patterns(self):
        with self._lock:
            return [asdict(p) for p in self.patterns.values()]
