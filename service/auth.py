"""调用方身份与角色。

演示级认证：通过请求头声明身份（X-Actor-Id / X-Actor-Role / X-School-Id），
生产环境应替换为真实鉴权网关，角色矩阵与隔离规则不变：

- control  总控：全部权限
- reviewer 审核员：计划初审、查看计划元数据（不可见未成年人资料）
- school   学校：提交/撤回本校计划、登记与查看本校学生名单
- auditor  审计员：只读审计日志与事件流（学生名单已脱敏）
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import DomainError

ROLES = ("control", "reviewer", "school", "auditor")


class AuthError(Exception):
    """身份缺失或非法（HTTP 401）。"""


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    school_id: str | None = None


def actor_from_headers(headers) -> Actor:
    actor_id = (headers.get("X-Actor-Id") or "").strip()
    role = (headers.get("X-Actor-Role") or "").strip()
    school_id = (headers.get("X-School-Id") or "").strip() or None
    if not actor_id or role not in ROLES:
        raise AuthError("缺少合法的调用方身份头（X-Actor-Id / X-Actor-Role）")
    if role == "school" and not school_id:
        raise AuthError("学校角色必须携带 X-School-Id")
    if role != "school":
        school_id = None
    return Actor(actor_id, role, school_id)


def require(actor: Actor, *roles: str) -> None:
    if actor.role not in roles:
        raise DomainError(
            "forbidden",
            f"角色 {actor.role} 无权执行该操作，需要 {'/'.join(roles)}",
            403,
        )
