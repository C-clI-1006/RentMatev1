# 本地交接：Stage 2 UC7（2026-10-24，sharon/reporting）

- 基线提交 `5b6cb2a`；已获取最新 main `6c8b0fd`、PR #11 `d33c93a`，并把新增代码整合进工作区。整合与 reporting 修复已提交至 sharon/reporting（未推送）；不能只看 HEAD 判断代码是否包含最新整合。
- 保留本地已验证修复：并发提交后复查 property/period、经理 get/list、子衡 lease/rent provider（独立降级、读取最新状态）、LLM 关闭思考与空正文保护，以及 Jenny 模块和全部路由。
- 本轮更新：维修提交 Idempotency-Key/冲突检查、demo 防重复、经理公开服务版 MaintenanceReportSource、调度版 SQLiteMaintenanceReportSource；两者共用事件历史月份筛选。priority=None 保留，选中后展示当前字段，无事件时 last_activity_at 回退 updated_at。
- 保留本地历史不足策略：无法判断月份归属的记录保留并注明原因；未改成 PR 的静默排除。
- 已修复：历史不足的完整性传播——`MaintenanceSectionData.period_range_complete`（默认 True，兼容旧报告）由共用 `_section_from_requests` 计算（经理版与调度版同一规则）；存在 `unknown_membership`（非当月创建、月内无事件、created 早于期末且无期末前事件）时为 False。`ReportService` 在 maintenance flag=False 时强制 outcome=PARTIAL_SUCCESS（有可用数据仅归属不确定，不触发 Q5 三源全不可用；EMPTY 不受影响）；`ReportRecord._check_outcome` 拒绝 COMPLETE+flag=False 的组合。当月创建或当月有事件即可确认归属，不因缺少更早历史降级。标记随 data_json 持久化，SQLite 重读保留。
- 验证：全量 **285 passed**（临时库，无真实模型调用；含本轮 3 条失败审计测试）。跑法：在独立测试进程启动前将 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 显式设为空字符串（dotenv 默认不覆盖已存在变量，从而保证 live adapter 未配置、fail-closed），`backend/.env` 保持原样。
- 已修复 Q5：三节全部 NOT_AVAILABLE 时停止生成——不调 LLM、不写 report 行（不再占用 event_id），仅记 audit-only 失败（action=`report.failed`，含 event_id）；同 event_id 数据恢复后可重新生成。部分缺失保留可用数据并标注；EMPTY 不算失败。
- 已修复：源级权限拒绝（collector 抛出的 PermissionError）——`generate` 捕获后停止处理：不调 LLM、不写 report、不降级 NOT_AVAILABLE；经 `record_failure`（audit-only，details 含 `code:"authorization_denied"`，不含异常原文）记录 `report.failed`，返回 `authorization_denied`（通用文字）。其他源异常仍走原数据缺失降级。维修源拒绝时租约租金源不再被调用；event_id 不被占用，恢复后同 event_id 可重试。
- 本轮新增测试：维修月份归属集成（partial + 标记 SQLite 往返、当月确认不降级、旧行无字段兼容、经理/调度双 provider 规则一致），全量回归通过。
- 本轮补齐三类 `report.failed` 审计（复用 `record_failure`，actor=可信 caller，details 无异常文本/旧报告信息，event_id 不占用可重试）：① 生成前授权拒绝（`authorization_denied`，先于查重/取数/LLM）；② 首次 `by_event` 查重发现 event_id 已被其他 property/period 占用（`event_id_conflict`）；③ insert 后并发复核发现获胜方 scope 不一致（`event_id_conflict`，失败方无 report 行，纯审计）。Q5 与源级拒绝沿用已有审计，不重复。
- 本轮新增测试：上述三类失败各 1 条（临时库、LLM 环境变量显式置空 fail-closed），断言审计条数、actor、event_id、code 与无 report 行/无取数/无 LLM 调用。
- 已覆盖：历史不足完整性（`period_range_complete`）、三源全不可用、源权限拒绝与本轮三类失败审计的 audit-only 记录；仍待修：维修提交 `idempotency_conflict` 的失败审计（属维修域，不在本轮）。
- 后续对接：经理 HTTP 生成/查阅入口、Chaney 实际月度调度、可重复月报种子及干净环境演示。核心正常生成、持久化、复用与真实叙述已验证；不要重新实现已完成模块。
- 本轮备份（含提交 bundle、未提交文件和演示库快照）：`/Users/yanxiaoran/Documents/Codex/2026-10-17/zhe/work/stage2/backups/before-latest-pr11-update-20261024-8e0af105`。
- PROGRESS.md 供内部交接，可上传 GitHub；最终作业文件包排除。不覆盖 .env 或演示数据。
