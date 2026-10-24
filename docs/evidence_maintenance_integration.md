# Evidence / Maintenance 本地联调契约

本地实现及临时数据库回归已接入；仍需 Kerwin 审查发送端改动并共同复验。没有新增事件模型，也没有新增 HTTP 事件接收入口。

## 身份与证据 ID

`evidence_refs` 使用上传成功返回的 `document_id`，新 ID 是 UUID4 字符串；兼容库中已有的字符串 ID，不传文件路径。校验只认可同物业中 `upload_status='saved'` 的真实记录。

- manager：验证独立 AuthContext 中的 manager_id 与 manager_property，允许使用该物业下的证据。
- tenant：验证 tenant_property，只允许使用本人上传的证据。
- 回调仅面向可信的进程内接线；AuthContext 必须由服务端会话解析/可信配置提供。不能从 payload.actor、recipient_id 或客户端字段构造身份。
- `actor` 是 Maintenance 提供的审计字段；接收记录另存经过验证的 `received_by`，二者不混用。

## 调用接口

```python
from functools import partial
from maintenance.outbox import dispatch_pending, reconcile

# evidence_service 与 maintenance_store 指向同一个应用数据库。
# manager_auth 来自已验证的服务端身份；按物业分别接线，不能由事件自选身份。
callback = partial(evidence_service.receive_maintenance_event, auth=manager_auth)
results = dispatch_pending(maintenance_store, {"evidence": callback}, property_id=manager_auth.property_id)

validation = evidence_service.validate_references(document_ids, property_id, manager_auth)
receipt = evidence_service.maintenance_receipt(event_id, property_id, manager_auth)
timeline = evidence_service.maintenance_timeline(request_id, property_id, manager_auth)
```

接收既有 `contracts.maintenance.MaintenanceEvent`，或 dispatch_pending 的实际字典：事件 snake_case 字段加现有 recipient_id、message_type、review_required。未知字段、无效类型、无时区时间及空事件 ID 被拒绝。字段按模型规范化，时间转 UTC，再比较整个事件及传输字段；JSON 键顺序不同不算冲突。

成功回调必须是 Python `True`，且仅在接收记录、时间线条目和全部证据链接一起提交后返回。SQLite 主键以 event_id 持久化去重；相同内容重放返回 True，不新增记录。同 ID 不同内容为永久拒绝 `event_id_conflict`。每次重放仍重新验证调用方权限。

HTTP 查询/校验复用现有 Bearer 依赖：

| 接口 | 身份与结果 |
|---|---|
| `POST /api/evidence/references/validate`，body 为 property_id、evidence_refs | 租户或经理；成功返回 status=valid、property_id、去重后的 document_ids；额外身份字段 422 |
| `GET /api/evidence/maintenance-events/{event_id}?property_id=...` | 经理；返回 accepted（含 received_at、received_by、payload）或 not_received；其他物业的接收记录表现为 not_received |
| `GET /api/evidence/maintenance-timeline?property_id=...&request_id=...` | 经理；返回按发生时间及事件 ID 稳定排序的事件、接收时间和接收经理 |

未认证 401；无绑定/越权 403；引用不存在和跨物业引用统一 404 `evidence_not_found`，不泄露另一物业证据是否存在；存储查询失败 503 `evidence_unavailable`。通用校验允许空列表（没有引用需要验证）；关闭/待确认流程另强制至少一个真实引用。

## 关闭前校验

MaintenanceService.change_status 在其原有 BEGIN IMMEDIATE 事务中调用 Evidence 的 `validate_references`，复用该事务连接，重新检查经理绑定和真实证据。无效引用返回 evidence_invalid，存储不可用返回 evidence_unavailable；均不更新状态、版本、历史或 outbox。原有空引用 evidence_required 及版本/状态/经理检查保留。所有携带引用的状态变更都做真实校验，awaiting_confirmation 与 closed 必须有引用。

## 失败与超时

| 回调结果 | dispatch_pending 状态 | 后续动作 |
|---|---|---|
| 提交成功或相同事件已接收：True | handed_off | 表示消费者持久化确认，不代表通知送达 |
| PermanentHandoffError：invalid_event、forbidden、evidence_required、evidence_not_found、event_id_conflict | rejected | 明确不接受此请求；不自动重试，last_error 保留稳定错误码 |
| 本地 SQLite busy/locked，已确认整个事务回滚：RetryableHandoffError | retry | 可用原 event_id/原内容重试 |
| TimeoutError、其他未知存储/提交异常或非 True 返回 | uncertain | 禁止盲目重试，先核对接收记录 |

超时恢复：查询相同物业的 receipt。accepted 时还要核对 payload 与原事件相同，再 `reconcile(store, event_id, 'evidence', accepted=True)`。同 ID 内容不同不能标记成功。not_received 不是永久拒绝记录，也不能证明一个尚在执行的回调以后不会提交；必须先确认原尝试已结束，再在权威数据库核实无记录后 accepted=False，使队列回到 retry。查询失败时继续保持 uncertain。

成功事件的 receipt、timeline、document links 只有一份；证据查询的 association 对已链接文档返回 associated。这里实现的是维修事件链接与维修时间线，不包含租约关联、通用前端时间线或正式登录认证。

没有后台自动 dispatch，也没有修改 mc_outbox 的其他消费者。部署前由 Kerwin 确认 rejected 的运维处理和按物业的可信 adapter 接线；实际组员联合评审与部署未声称完成。

## 实现与验证进度（2026-10-24）

实现阶段开始前已确认远端分支为 `2a5d894939ea90260f4b93abb8a58be712480070`；该阶段结束时修改留在本地工作区，未暂存、提交或推送。交付状态以后续记录为准。

新增 `tests/test_evidence_maintenance.py` 使用真实临时 SQLite、真实上传记录、既有 MaintenanceService 和 dispatch_pending；仅分类器/优先级推理使用明确的测试桩。覆盖正常持久化、重启及并发重放、同 ID 不同内容拒绝、无效/跨物业引用、伪造 actor 不授权、租户所有权、权限撤销、部分写入回滚、SQLite busy 确认回滚、未知超时在提交前/后分别核对、队列永久拒绝/重试/不确定、关闭及待确认失败保持原状态/版本/历史/outbox、HTTP Bearer 查询隔离、v3→v4 升级，以及按物业派发互不干扰。

早期存储故障测试准备试图删除有外键引用的临时表，被 SQLite 正确阻止；已改为表重命名模拟存储不可用，未放宽业务断言。第一次完整回归 292 passed；随后补充按物业过滤及多物业回归，将以最终复验结果为准。

测试环境延用已验证的独立 Python 3.12.14 和 `backend/.venv/integration-test-deps`；原虚拟环境仍缺少其指向的 Python 3.11 安装。测试收集前的 dotenv 禁用、数据库/上传目录隔离、LLM 配置替换及外部 socket 阻断均保持。`maintenance.verify_live` 是在线验收工具，本轮未运行。

最终验证：

- 相关回归：`tests/test_evidence_maintenance.py tests/test_maintenance.py tests/test_legacy_upgrade.py`，**67 passed，1 warning，20.04s**。
- 完整 backend：**293 passed，1 warning，47.88s**，无失败或跳过；相比集成基线的 260 项新增 33 项事件联调回归。
- 命令：在 backend 设置 `PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH` 指向独立依赖目录，运行独立 Python 的 `-m pytest -q -p no:cacheprovider --tb=short`。
- 唯一 warning 为 Starlette/httpx TestClient 弃用提示；没有为此改动依赖约束。
- 工作区 `git diff --check`、暂存区 `git diff --cached --check` 通过；新增未跟踪源码/文档另做语法及行尾空白检查，也通过。
- 实现阶段结束时 HEAD 为 `2a5d894939ea90260f4b93abb8a58be712480070`，改动未提交；后续交付见下节。原有日志和任务说明保持未跟踪且未删除。

## 交付前复核（2026-10-24）

用户已授权复核通过后提交并正常推送到现有分支，不强推、不合并 PR、不新增功能。

- 分支为 `jenny/scrum-8-document-upload`，基线 HEAD 及远端同名分支均为 `2a5d894939ea90260f4b93abb8a58be712480070`，无未完成合并。
- 18 个交付文件经路径、差异及源码检查，未包含真实数据库、上传文件、凭据、依赖目录或测试结果日志；没有残留冲突标记。两个已有测试结果 txt 和本地任务说明继续保留为未跟踪文件。
- 未扩展功能；按用户要求移除仓库文档内的组员评审清单，保留技术契约和测试记录。
- 使用同一独立 Python 3.12.14 环境重新运行全部 backend 测试：**293 passed，1 warning，48.37s**。警告仍为 Starlette/httpx 弃用提示。未运行在线验收工具、调用真实 Qwen、读取敏感配置或操作真实数据库。
- 工作区及暂存区 diff 空白检查通过；本记录随交付代码一并提交，最终提交哈希与推送结果由交付后的 Git 命令确认。
- Kerwin 的联合接线、共享改动评审及部署验证仍待双方完成；本次不修改或合并 PR。
