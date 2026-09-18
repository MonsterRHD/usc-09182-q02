"""泥咕咕技艺谱系：领域常量、记录类型与业务错误。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 角色
ROLE_MASTER = "master"          # 师傅
ROLE_APPRENTICE = "apprentice"  # 学徒
ROLE_INHERITOR = "inheritor"    # 传承人
ROLES = (ROLE_MASTER, ROLE_APPRENTICE, ROLE_INHERITOR)

# 学徒阶段（有序，跨阶段晋级指一次跨越两级及以上）
STAGES = ["入门", "基础", "进阶", "出师"]

# 版本状态
STATUS_PENDING = "pending"      # 待确认
STATUS_CONFIRMED = "confirmed"  # 已确认

MAIN_BRANCH = "main"


class DomainError(Exception):
    """业务错误基类，status/code 供 HTTP 层映射。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


class Conflict(DomainError):
    status = 409
    code = "conflict"


@dataclass
class Workshop:
    id: str
    name: str
    created_at: str


@dataclass
class Person:
    id: str
    name: str
    role: str
    workshop_id: str
    stage: int = 0                      # 学徒阶段下标（仅学徒有意义）
    stage_history: list = field(default_factory=list)  # 晋级历史，只增不改
    created_at: str = ""


@dataclass
class Lineage:
    """师承关系：师傅 -> 学徒。"""

    id: str
    master_id: str
    apprentice_id: str
    created_at: str


@dataclass
class Shape:
    """造型。同名造型可分属不同作坊，身份 = (名称, 作坊)。"""

    id: str
    name: str
    workshop_id: str
    branches: dict = field(default_factory=dict)  # branch_id -> 分支头版本 id
    fork_count: int = 0
    created_at: str = ""


@dataclass
class Version:
    """造型版本。提交后不可改；纠正旧版以新版本引用 corrects_version_id。"""

    id: str
    shape_id: str
    author_id: str
    branch_id: str
    base_version_id: Optional[str]
    corrects_version_id: Optional[str]
    photo_summary: str   # 照片摘要（散列/文字说明），服务不存原图
    steps: list          # 工序，如 [{"name": "打孔", "detail": "音孔两孔..."}]
    materials: list      # 泥料等材料
    note: str
    status: str
    submitted_at: str
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None


@dataclass
class ForbiddenPattern:
    """禁用图样。禁用只影响公开展示判断，不删除任何历史版本。"""

    id: str
    motif: str
    reason: str
    banned_by: str
    banned_at: str


@dataclass
class License:
    """公开许可（可公开范围）。撤回只影响今后的展示，授权事实留档。"""

    id: str
    shape_id: str
    version_id: Optional[str]  # None 表示覆盖整个造型
    scope: str                 # 公开范围，如 展览 / 出版 / 网络展示
    granted_by: str
    granted_at: str
    revoked_by: Optional[str] = None
    revoked_at: Optional[str] = None
    revoke_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass
class Citation:
    """谱系引用：必须同时标明版本与适用作坊。"""

    id: str
    version_id: str
    workshop_id: str
    purpose: str
    cited_by: str
    cited_at: str
