# PR #8 本地集成进度

日期：2026-10-24（Asia/Shanghai）。本阶段只做本地集成；本次用户指令优先于任务说明中 fetch、merge、提交及推送等旧步骤。

## 本阶段实际修改

- `backend/tests/test_evidence_review_router.py`：迁移到真实 Bearer 解析与服务端测试令牌映射，保留真实权限依赖。租户 resident 绑定负责文档读取，独立经理通过 `manager_property` 审核；响应和独立数据库连接均断言 `reviewed_by` 为经理 ID。保留分类结果不被覆盖、历史记录、幂等、409/422、无写入等原有断言。新增未知令牌 401、租户审核 403、伪造身份头无效、经理跨物业文档与不存在文档均返回 404 的回归。
- `backend/tests/test_evidence_review_service.py`：原有审核测试改用经理绑定及经理 ID；保留此前已有的跨租户经理审核用例、独立持久化检查和原机器结果断言。无绑定失败场景改用未绑定经理身份。
- `backend/main.py`：`create_app` 新增可选的 keyword-only `classifier` 注入，生产默认行为不变；修正旧认证注释。没有新增 Maintenance 事件接口，保留既有路由及 dotenv 行为。
- `backend/tests/test_evidence_upload.py`、`test_evidence_extract.py`、`test_evidence_review_flag_queries.py`：HTTP 测试显式注入测试分类器；配置失败隐私回归使用返回 `ClassifyFailure(configuration)` 的测试分类器，保留落库、人工审核和内部详情不泄露断言。
- `backend/scripts/seed_evidence_demo.py`：修复先插绑定后建父身份导致的外键失败，改为同一事务内先建身份再建绑定；使用共享 SQLite 迁移入口，避免旧库直接执行 schema 时缺少 `due_date` 导致索引创建失败。保留已有身份字段，不覆盖冲突绑定，不提升权限，不写凭据。
- `backend/tests/test_seed_evidence_demo.py`：同步独立租户、经理及两种绑定，快照覆盖 manager/manager_property；验证幂等、冲突回滚和已有经理自定义字段保留。
- `backend/tests/test_legacy_upgrade.py`：保留现有旧库升级回归；关闭遗留快照连接，显式捕获跨线程仓库访问异常；补充 v0/v1/v2 旧库 seed 升级及重复运行、外键完整性、已有 due_date 保留测试。
- `backend/tests/conftest.py`：在测试收集、导入 `main` 之前设置临时数据库和存储路径，阻止 dotenv 加载，替换 LLM 配置及令牌映射；阻止外部 socket 连接，允许 Windows asyncio 必需的本地回环连接。
- `docs/evidence_demo.md`：同步 resident 上传、独立经理审核、seed 创建 manager_property、服务端令牌映射；说明 accepted/queued 不证明外部送达，保留 association/timeline 未完成及 UC6 范围限制。

## SQLite 核查结论

保留并验证了本阶段开始前已存在的 `backend/shared/sqlite.py` 和 EvidenceRepository 迁移修改：先执行版本迁移，再执行 schema，v3 只补充缺失的 `rent_record.due_date`。旧记录未知日期保持 NULL，不猜测日期、不重新计算历史租金状态；已有日期保持原值。EvidenceRepository 继续使用自己的跨线程连接与锁。此修复不声称覆盖其他尚未识别的历史 schema 差异。

所有升级验证仅使用临时数据库；未操作真实 `rentmate.db`，未读取真实 `.env`、密钥或 token，未调用真实 Qwen。

## 测试环境与结果

指定的 `backend\.venv\Scripts\python.exe` 已尝试，但无法启动：其 pyvenv.cfg 指向的 Python 3.11 安装不存在。未改写现有虚拟环境配置。改用本机 Codex Python 3.12.14，并在被忽略的 `backend/.venv/integration-test-deps` 内安装项目 requirements 的独立测试依赖。

在 backend 目录运行，设置 `PYTHONDONTWRITEBYTECODE=1` 和指向上述依赖目录的 `PYTHONPATH`：

```text
<Codex Python 3.12.14> -m pytest -q -p no:cacheprovider --tb=short
```

- 相关回归：**87 passed，1 warning**。包含 upload 19、extract 12、review_flag_queries 10、review_router 16、review_service 12、seed 8、legacy_upgrade 10。
- 首轮完整套件：253 passed，1 failed。失败是通用成功 stub 改变配置失败隐私回归的前提；随后注入明确的配置失败测试分类器，并补齐其 `source` 接口字段。原隐私和持久化断言保留。
- 最终完整套件：**254 passed，1 warning，30.86s**，无失败或跳过；包括 Maintenance、Communication、Review、Reporting、Lease/Rent 和 Evidence 的全部 backend 测试。
- 最终工作区 `git diff --check` 与暂存区 `git diff --cached --check` 均通过；仅有原文件的 LF/CRLF 提示，不是空白错误。新增未跟踪源码及本进度文件也单独检查行尾空白。
- 依赖 warning：Starlette 提示 httpx TestClient 接口已弃用；不影响当前测试结果。没有为此更改项目依赖约束。

## 合并状态与剩余事项

- 分支保持 `jenny/scrum-8-document-upload`。
- HEAD 保持 `cfe3a18e06bf0ac485b58d55b3a3feb8a82581e7`。
- MERGE_HEAD 保持 `9b4b32e6f61883678c228fe239071479c4d3fd8e`，**合并仍未完成**；索引无未解决冲突条目。
- 保留全部已有暂存、未暂存及未跟踪内容；本阶段未暂存文件，未再次 merge、reset、切换分支、提交、推送或修改 PR。
- 仍需修复或重建本机 Python 3.11 虚拟环境后，才能用用户指定解释器复验。本次完整结果基于上述替代环境。
- Evidence 的 association、timeline 及 Maintenance→Evidence 回调仍未完成；本阶段未扩展这些功能，也不声称 UC6 全部完成。
- 后续提交、推送及结束当前 merge 不在本阶段授权范围内；本阶段完成后停止。

## Git 集成续阶段（2026-10-24）

本节更新此前阶段的停止点：用户现已授权本地提交、fetch 及按需合并最新 main；仍禁止推送、强推、reset、删除现有工作或修改/合并 PR。

- 已核对当前实际工作区差异、暂存区和原 MERGE_MSG。原冲突文件为 `backend/main.py` 与 `backend/tests/test_contracts_smoke.py`：前者保留 dotenv、Maintenance/Review、health、rent-alerts 路由并挂载 Evidence，后者与待合入主干完全一致。schema 保留主干表并增加 Evidence 表，迁移与 manager_property 权限检查保留。无未解决冲突条目或残留标记。
- 未发现真实数据库、上传目录、凭据文件或二进制产物被跟踪；`.env.example` 中密钥为空、令牌映射为空对象，仅纳入配置模板。新增回归测试 `conftest.py`、`test_legacy_upgrade.py` 与本进度文档应纳入提交。
- 本地 `backend/evidence-test-result.txt`、`backend/evidence-upload-result.txt` 保持未跟踪，不纳入提交，也不删除。`docs/pr8_integration_task.md` 是本地工作指令，保持未跟踪。
- `docs/maintenance/verification/` 中三份版本化验收文档与原 MERGE_HEAD 完全一致，作为已有主干内容保留；没有新增本地运行日志。
- 已完成原 MERGE_HEAD `9b4b32e6f61883678c228fe239071479c4d3fd8e` 对应的本地合并提交：`1fa1eace71e91fae0cb6eb856dc9f5e9ffc307e8`，父提交为原分支 HEAD 与该主干提交。
- 随后执行 `git fetch origin`，最新 `origin/main` 为 **`6c8b0fd4b47c920cf99b5cf099115e8ba1aef9c6`**（PR #12 合并提交）。它尚未被第一次合并包含，已通过 `merge --no-ff --no-commit origin/main` 合入，无冲突。13 个合入文件均与该主干逐字一致，包含主干已有的 Maintenance 报告提供者、HTTP 幂等、演示 seed 及回归；没有自行新增事件接口。Evidence、app wiring、权限及 SQLite 迁移集成未被覆盖。
- 合入最新主干后完整 backend 测试：**260 passed，1 warning，33.73s**，无失败或跳过。命令仍为 `python -m pytest -q -p no:cacheprovider --tb=short`，使用 Codex **Python 3.12.14** 和 `backend/.venv/integration-test-deps` 独立依赖目录，`PYTHONDONTWRITEBYTECODE=1`。原 `.venv/Scripts/python.exe` 因缺失其引用的 Python 3.11 安装仍不可用；没有改写原环境配置。
- 测试收集前即隔离 module-level app 的数据库与 storage，禁用 dotenv 加载、替换 LLM 凭据配置，测试分类器/传输桩及 socket 外部连接阻断保持生效。未读取真实敏感配置、调用真实 Qwen 或操作真实数据库。唯一 warning 仍为 Starlette/httpx TestClient 弃用提示。
- 工作区及暂存区 `git diff --check` 均通过。最新主干合并和本节进度记录一起形成本地合并提交；其父提交为 `1fa1eace71e91fae0cb6eb856dc9f5e9ffc307e8` 与 `6c8b0fd4b47c920cf99b5cf099115e8ba1aef9c6`，最终哈希由完成后的 `git rev-parse HEAD` 报告，避免在提交内自引用。
- 完成后应无 MERGE_HEAD、无已暂存或未暂存的跟踪文件修改；上述两个测试日志和任务说明三个文件继续保留为未跟踪文件。本轮不推送、不修改或合并 PR。

## 后续阶段：Maintenance / Evidence 本地事件联调

本轮开始已通过远端只读核对：`origin/jenny/scrum-8-document-upload` 为 `2a5d894939ea90260f4b93abb8a58be712480070`，确认上述集成提交已推送后才修改代码。

现已新增本地事件接收、真实证据校验、原子接收记录/时间线/引用链接、持久化去重和接收查询；Maintenance 关闭/待确认前校验已接入其原有状态事务，不再接受不存在的占位引用。`MaintenanceEvent` 定义不变，身份不从 actor 推导。

接口、错误与超时恢复约定及本阶段验证结果统一记录在 [Evidence / Maintenance 联调记录](evidence_maintenance_integration.md)。共享契约/发送端/迁移改动的评审清单单独提供给用户转发，不放入仓库文档。这更新了此前“事件回调/维修时间线尚未实现”的本地代码状态；实际组员联合评审和部署接线仍待完成。

本阶段不暂存、不提交、不推送。既有两个测试结果日志和任务说明文件保留，不读取真实凭据、不调用真实 Qwen、不操作真实数据库。

## 后续交付复核

用户已另行授权将通过复核的事件联调代码提交并正常推送到现有分支。本次不新增功能，完整 backend 重新验证为 **293 passed，1 warning，48.37s**，独立 Python 3.12.14；工作区及暂存区 diff 检查通过。交付文件不包含数据库、上传文件、凭据或本地测试日志。详细复核记录见上述联调文档的“交付前复核”小节，最终 Git 哈希与推送结果由交付后命令报告。未开展实际组员联合部署验证，不修改或合并 PR。
