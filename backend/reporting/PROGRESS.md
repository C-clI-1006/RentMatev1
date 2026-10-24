# 本地交接：Stage 2 UC7（2026-10-24，sharon/reporting）

## 当前状态

- 自动测试基线：**隔离环境 295 passed**（临时库，无真实模型调用）。跑法：独立测试进程启动前将 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 显式设为空字符串（dotenv 默认不覆盖已存在变量，保证 live adapter 未配置、fail-closed）：
  `LLM_BASE_URL= LLM_MODEL= LLM_API_KEY= .venv/bin/python -m pytest tests -q`（backend/ 下）。`backend/.env` 保持原样。
- 所有本轮改动（`manager_api.py`、`tests/test_manager_reporting_api.py`、`seed_reporting_demo.py`、README、本日志）保持**未提交**；`.env` 与演示库未动；无 push。
- 备份（提交 bundle、未提交文件、演示库快照）：`/Users/yanxiaoran/Documents/Codex/2026-10-17/zhe/work/stage2/backups/before-latest-pr11-update-20261024-8e0af105`。
- PROGRESS.md 供内部交接，可上传 GitHub；最终作业文件包排除。

## 当前功能

- 已整合最新 main（`6c8b0fd`）与 PR #11（`d33c93a`）进工作区，整合与 reporting 修复已提交至 sharon/reporting（未推送）；不能只看 HEAD 判断代码是否包含最新整合。保留本地已验证修复：并发提交后复查 property/period、经理 get/list、子衡 lease/rent provider（独立降级、读取最新状态）、LLM 关闭思考与空正文保护，以及 Jenny 模块和全部路由。
- 维修源：提交 Idempotency-Key/冲突检查、demo 防重复、经理公开服务版与调度版 MaintenanceReportSource，共用事件历史月份筛选。priority=None 保留，选中后展示当前字段，无事件时 last_activity_at 回退 updated_at。
- 历史不足策略：无法判断月份归属的记录保留并注明原因（未改成静默排除）。完整性传播——`MaintenanceSectionData.period_range_complete`（默认 True，兼容旧报告）由共用 `_section_from_requests` 计算（经理/调度同一规则）；存在 `unknown_membership`（非当月创建、月内无事件、created 早于期末且无期末前事件）时为 False。`ReportService` 在 maintenance flag=False 时强制 outcome=PARTIAL_SUCCESS；`ReportRecord._check_outcome` 拒绝 COMPLETE+flag=False。当月创建或当月有事件即可确认归属，不因缺少更早历史降级。标记随 data_json 持久化，SQLite 重读保留。
- Q5：三节全部 NOT_AVAILABLE 时停止生成——不调 LLM、不写 report 行（不占用 event_id），仅记 audit-only 失败（`report.failed`，含 event_id）；同 event_id 数据恢复后可重新生成。部分缺失保留可用数据并标注；EMPTY 不算失败。
- 源级权限拒绝（collector 抛 PermissionError）：`generate` 捕获后停止——不调 LLM、不写 report、不降级 NOT_AVAILABLE；经 `record_failure`（details 含 `code:"authorization_denied"`，不含异常原文）记 `report.failed`，返回 `authorization_denied`（通用文字）。其他源异常仍走数据缺失降级。维修源拒绝时租约租金源不再被调用。
- `report.failed` 审计共三类（复用 `record_failure`，actor=可信 caller，details 无异常文本/旧报告信息，event_id 不占用可重试）：① 生成前授权拒绝（`authorization_denied`，先于查重/取数/LLM）；② 首次 `by_event` 查重发现 event_id 已被其他 property/period 占用（`event_id_conflict`）；③ insert 后并发复核发现获胜方 scope 不一致（`event_id_conflict`，失败方无 report 行，纯审计）。Q5 与源级拒绝沿用已有审计，不重复。
- 经理 HTTP 入口 `reporting/manager_api.py`（main.py 两行注册，前缀 `/api/manager/monthly-reports`）：
  - `POST /generate`：身份仅取服务端 `RENTMATE_AUTH_TOKENS`/AuthContext（strict Bearer），拒绝 body `manager_id`（422）；同 idempotency_key 复用（`reused=true`），新 key 生成下一代；错误映射 401/403/409/503；叙述层留 seam（`build_narrator`）便于测试注入。
  - `GET ""`：`?property_id&year&month` 按 generation 升序返回版本列表，无记录返回 `[]`。
  - `GET /{report_id}`：单份报告，property 取自令牌绑定。
  - 复用既有 router 与 `ReportService.get`/`list_for_period`，未写新 SQL。只读已验证：不取数、不调 LLM、不新建报告。
  - 两层拒绝均不泄露报告存在性：`get_auth` 依赖先校验令牌自身 property 绑定（失效 403，先于端点逻辑）；端点 `_require_access` 校验请求的 property（未绑定者一律 404 "Report not found"，存在与不存在同响应）。
- 演示种子 `backend/scripts/seed_reporting_demo.py`：`--db` 必须与服务 `DATABASE_PATH` 同库；演示房源 `reporting-demo-property`、月份 `2026-09`；初始化不调模型不生成报告，`INSERT OR IGNORE` 重复执行安全。

## 测试

- `tests/test_manager_reporting_api.py` 共 **10 条**（临时库、标记 stub 叙述器，无真实模型调用）：生成 4 条（正常含 401/422、越权 403 仅审计不落行、同 key 复用、新 key 下一代）+ GET 6 条（单查 401 无/错令牌与 200 字段断言、多版本升序、空期间 `[]`、404 不存在、越权 404 与不存在不可区分、权限撤销 403 三种场景不可区分；每条均断言 stub 计数与 report 行数不变）。
- 回归点：维修月份归属集成（partial + 标记 SQLite 往返、当月确认不降级、旧行无字段兼容、双 provider 规则一致）、三类失败审计各 1 条（断言审计条数、actor、event_id、code 与无 report 行/无取数/无 LLM 调用）。

## 经理接口真实验收（2026-10-24，一次性）

- 结论：真实经理接口验收通过，LLM 调用一次。
- 临时库 + `seed_reporting_demo.py`；进程内注入临时随机经理令牌（`RENTMATE_AUTH_TOKENS`/`DATABASE_PATH` 仅进程 env，未动 `.env`）。
- POST generate（`reporting-demo-property` / 2026-09）：`complete`、`narrative=live`、`created_by=manager:reporting-demo-manager`。
- GET 单查 + GET 列表均 200，仅 1 份报告；重放同一 idempotency_key：`reused=true`、`report_id` 不变、`report` 表仍 1 行。
- 未触碰现有演示库 `backend/rentmate.db`；全程无自动重试/额外探测。

## 剩余待办

- 维修提交 `idempotency_conflict` 的失败审计（属维修域，不在本轮）。
- Chaney 实际月度调度对接、可重复月报种子及干净环境演示。
- 核心生成、持久化、复用、真实叙述与经理 HTTP 生成/查阅已验证；不要重新实现已完成模块。
