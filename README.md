# 天地课堂联播台

面向港澳多校参与“天地连线”直播的联播总控服务，替代散落在多个表格中的提问征集、
场次编排、信号切换与课后材料管理。仅依赖 Python 3.11+ 标准库，无需外部数据库。

## 运行与验收

```bash
python3 -m unittest discover -s tests    # 31 项自动化验收
python3 -m service.main                  # 启动 HTTP 服务（默认 127.0.0.1:8000）
```

设置 `TIAN_DI_EVENT_LOG=/path/to/events.jsonl` 可将事件落盘，重启后完整重放还原
全部排期版本、占位、通知与审计；不设置则使用内存实例。

## 核心能力与实现位置

| 业务要求 | 实现 |
| --- | --- |
| 学校提交带审核链的参与计划（提问、材料随计划归档） | `service/service.py` `submit_plan` / `approve_or_reject` |
| 总控按空间站窗口与校区容量锁定场次 | `hold_plan` / `lock_window`；容量取 `min(信号容量, 校区容量)` |
| 重复上报保持幂等 | 计划按 `school + idempotency_key` 去重；审核、占位、退出均幂等 |
| 断线重连从最后确认点继续 | `GET /sync?after_seq=N`（`service.py` `sync`），游标即事件序号 |
| 未成年人资料角色隔离 + 访问审计 | 学生完整资料仅所属学校可见；所有放行/拒绝均写 `AUDIT_ENTRY` |
| 窗口缩短 / 学校退出 / 两地同时改动 | `revise_window` / `withdraw_plan` 产生可解释 `ConflictRecord` |
| 可解释的冲突处置与通知范围 | 冲突记录含原因、影响计划/学校范围、选项与建议；总控 `resolve_conflict` |
| 通知失效不误发 | `dispatch_notices` 派发前复核：未决冲突挂起、被取代/失效草稿抑制 |
| 重启还原每次排期版本 | 每个版本一条 `SCHEDULE_VERSION_BUMPED` 不可变快照，`GET /schedule/versions/N` |
| 并发占位不超额 | 全部写操作在同一把锁内“校验→追加事件→投影更新” |

## HTTP 接口（JSON，鉴权信息由网关注入请求头）

请求头：`X-Actor-Id`、`X-Actor-Role: control|school|auditor`、
`X-Actor-School: <学校标识>`（学校角色必填）。

- `POST /admin/schools|campuses|windows` — 总控登记基础数据
- `POST /students` / `GET /students?school_id=` — 未成年人资料（仅本校）
- `POST /plans`（必带 `idempotency_key`）、`POST /plans/{id}/approvals`
- `POST /plans/{id}/hold`、`POST /windows/{id}/lock`
- `POST /windows/{id}/revise`（必带 `expected_version`）
- `POST /plans/{id}/withdraw`（锁定后退出必带 `expected_version`）
- `POST /conflicts/{id}/resolve`、`POST /notices/dispatch`
- `GET /schedule`、`GET /schedule/versions/{n}`、`GET /conflicts`、`GET /notices`
- `GET /sync?after_seq=N`、`GET /audit`（control/auditor）、`GET /health`

冲突与并发约定：锁定后的变更必须携带所基于的排期版本号；版本落后时返回
`409 SIMULTANEOUS_EDIT`，响应体携带冲突 ID 与当前版本，调用方拉取最新排期后重试。

## 领域事件

所有状态变更均为仅追加事件（`service/eventstore.py`），含 `event_time`（调用方
业务时间）与 `received_at`（服务端接收时间），审计记录只追加不覆盖。
