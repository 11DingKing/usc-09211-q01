# 接口说明

所有接口（除 `/health`）要求请求头：`X-Actor-Id`、`X-Actor-Role`（control / reviewer / school / auditor），学校角色另需 `X-School-Id`。

变更类接口（POST / PUT）接受 `Idempotency-Key` 请求头；幂等重放时响应头带 `Idempotent-Replay: true`。所有请求体可附 `occurred_at`（ISO 8601）表示业务发生时间。

错误统一为 `{"error": 代码, "message": 中文说明, ...}`，状态码：400 参数 / 401 未认证 / 403 越权 / 404 不存在 / 409 冲突。

## 健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 无需身份，返回 `{"status": "ok"}` |

## 登记

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/schools` | control | 登记学校：`{school_id, name, region, seat_capacity}` |
| GET | `/schools` | 任意 | 学校列表 |
| POST | `/windows` | control | 登记空间站窗口：`{window_id, start, end, seat_capacity}`；时间不得与既有窗口重叠（409 `window_overlap`） |
| GET | `/windows` | 任意 | 窗口列表 |

## 参与计划与审核链

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/plans` | school / control | 提交计划：`{plan_id, school_id, window_id, student_count}`；学校只能提交本校；同校同窗口仅允许一个进行中计划 |
| GET | `/plans` | 任意 | 计划列表，过滤：`school_id` `status` `window_id` |
| GET | `/plans/{plan_id}` | 任意 | 计划详情（含审核链，不含学生名单） |
| POST | `/plans/{plan_id}/reviews` | reviewer / control | 审核：`{stage: initial\|final, action: approve\|reject, comment?}`；初审 reviewer/control，终审仅 control，须按序进行 |
| POST | `/plans/{plan_id}/withdraw` | school / control | 撤回：`{reason?}`；已锁定的占位随之释放，相关待通知失效，总控收到通知 |

## 未成年人资料

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| PUT | `/plans/{plan_id}/students` | school / control | 登记学生名单：`{students: [{student_id, name, grade}]}`；人数不得超过计划人数 |
| GET | `/plans/{plan_id}/students?purpose=用途` | school（本校）/ control | 读取名单；必须带 `purpose`，每次访问写入审计 |
| GET | `/audit` | control / auditor | 未成年人资料访问审计（含 seq、操作者、用途） |

## 场次与排期

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/sessions` | control | 锁定场次：`{session_id, window_id, assignments: [{plan_id, seats}], expected_version?}`；容量不足 409 `capacity_exceeded` 并附剩余座位 |
| GET | `/sessions` | 任意 | 场次列表 |
| POST | `/sessions/{session_id}/cancel` | control | 取消场次：`{reason?, expected_version}`；释放座位、失效相关待通知、通知各校与总控 |
| POST | `/windows/{window_id}/shrink` | control | 窗口收缩：`{new_end?, new_seat_capacity?, expected_version, resolution?}`；超容时 409 `capacity_overflow` 返回影响面与选项，`resolution=auto_trim` 确认后按终审时间从新到旧移除 |
| GET | `/schedule` | 任意 | 当前排期：版本号、各窗口用量、各场次占位 |
| GET | `/schedule/versions` | 任意 | 全部排期版本及中文摘要 |
| GET | `/schedule/versions/{n}` | 任意 | 还原第 n 版排期快照 |

## 通知

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/notifications` | control / school | 通知列表，过滤：`status`（pending/sent/superseded）`school_id`；学校仅见本校 |
| POST | `/notifications/dispatch` | control | 下发全部待通知；返回 `{dispatched, dispatched_count, skipped_superseded}`；已失效的永不下发 |

## 事件流（断线重连）

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/events?since={seq}` | control / auditor | 返回 `seq` 之后的事件与 `latest_seq`；客户端以 `latest_seq` 为最后确认点，断线后据此续传。审计员视角学生名单已脱敏 |

## 冲突响应示例

版本冲突（409 `version_conflict`）：

```json
{
  "error": "version_conflict",
  "message": "排期版本已变化：期望 v1，当前 v2，期间有 1 次变更",
  "expected_version": 1,
  "current_version": 2,
  "intervening": [{"version": 2, "type": "session_locked", "summary": "场次 sess-2 锁定：窗口 win-1，1 所学校共 20 人", "actor": "ops-1", "affected_schools": ["sch-2"]}],
  "recovery": "请先 GET /schedule/versions/2 查看最新排期，再基于新版本重试"
}
```

窗口收缩超容（409 `capacity_overflow`）：`impact` 给出已占位、超出座位与移除候选顺序，`options` 给出可选处置（`auto_trim` 及其效果），确认后以原请求加 `resolution` 重试。
