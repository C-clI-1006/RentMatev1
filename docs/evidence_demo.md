# 证据模块演示操作说明（Windows PowerShell）

面向组员的简明操作说明：在 Windows PowerShell 中跑通证据模块的两条演示流程。

## 本演示覆盖 / 不覆盖

覆盖：上传 →（文字 PDF 自动分类）→ 经理审阅确认 / 人工选类，全部通过 Swagger 完成。

**不覆盖（演示时不要声称）：**

- **本演示不包含事件派发**：上传后默认 `association: "unassociated"`。新增的 Maintenance 回调会在成功接收真实引用后记录维修时间线并使引用文档显示 associated，接线及返回格式见 [联调契约](evidence_maintenance_integration.md)。该部分已做本地回归，仍待 Kerwin 联合评审；租约关联和通用前端时间线尚未实现。
- **原型会话**：身份来自 `Authorization: Bearer <token>`，令牌在服务端 `RENTMATE_AUTH_TOKENS` JSON 映射中解析，尚未接入正式登录认证。租户上传与经理审核使用**不同的令牌 / 身份**；租户和经理的数据库物业绑定授权检查实际执行，不能用客户端身份字段绕过。
- **不声称整个 UC6 已完成**：本演示只展示上传 → 分类 → 审阅这一条样例子路径，不代表分类准确性，也不代表 UC6 全部范围。
- `accepted` 只代表上传被接受并保存；其他模块的 `queued` / `accepted` 状态也不能证明通知已经对外送达。
- **文档中不含任何真实密钥或令牌**，也不包含任何私人机器路径。

## 0. 约定

- 演示身份（seed 脚本写入的虚构数据，非真实用户）：`T-DEMO-01` / `P-DEMO-01`；经理审核示例身份 `M-DEMO-01`（虚构）。
- 所有请求的 `property_id` 使用 `P-DEMO-01`。
- 两个演示令牌都在服务端 `RENTMATE_AUTH_TOKENS`（服务端环境变量，JSON 映射）中预先配置，**不要**把任何真实令牌写进文档、命令或提交（见第 3 节）。
- **document_id 一律使用上传响应里返回的实际值**（`document.document_id`），不要硬编码任何本机的 ID。
- 后端地址：`http://127.0.0.1:8000`，Swagger：`http://127.0.0.1:8000/docs`。

## 1. 安装 backend 依赖

```powershell
cd <仓库路径>\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

若 `Activate.ps1` 被执行策略拦截，先在当前会话执行 `Set-ExecutionPolicy -Scope Process Bypass` 再激活。

## 2. 写入演示数据（seed）

在仓库根目录（或任意目录，脚本按自身位置解析 db 与 schema，与 CWD 无关）：

```powershell
python backend\scripts\seed_evidence_demo.py
```

- TEST-ONLY 脚本：创建虚构租户 `T-DEMO-01`、独立经理 `M-DEMO-01` 和共享物业 `P-DEMO-01`。租户通过 `tenant_property(role='resident')` 上传；经理通过 `manager_property` 审核。**不写入任何认证令牌**。
- 幂等：行已存在则不覆盖、不重复写；租户绑定已存在但 role 不是 resident 时明确报错并回滚，不覆盖或提升权限。
- 不会创建任何 evidence / 分类 / 审阅记录，不写任何凭据。
- 成功后输出 `seed complete: db=...`（默认库路径与 `main.py` 默认一致）。

注意：seed 只负责数据库里的绑定行；**认证令牌映射（`RENTMATE_AUTH_TOKENS`）与这些行是分开的**，由服务端自行配置，见第 3 节。

## 3. 配置 LLM 与认证环境变量

**`backend/main.py` 启动时会自动加载 `backend/.env`**（`load_dotenv`，在业务模块导入之前执行），所以默认做法是：

```powershell
cd <仓库路径>
Copy-Item .env.example backend\.env
# 用编辑器把真实值填入 backend\.env 的 LLM 段与 RENTMATE_AUTH_TOKENS，然后重启 uvicorn
```

`.env.example` 只是变量名参考（不含任何真实值）；`backend/.env` 已被 git 忽略，**不要把真实值写进任何会被提交的文件**。

- **进程环境变量优先**：已经存在于进程环境中的变量会**覆盖** `backend/.env` 里的同名值（dotenv 加载不覆盖既有变量）。所以也可以不改文件，直接在启动 uvicorn 的会话里导出：

  ```powershell
  $env:LLM_BASE_URL = Read-Host "LLM_BASE_URL（代理地址，如 https://.../v1）"
  $secKey = Read-Host "LLM_API_KEY（隐藏输入）" -AsSecureString
  $env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secKey).Password
  $env:LLM_MODEL = Read-Host "LLM_MODEL（模型名）"
  $env:LLM_TIMEOUT_SECONDS = Read-Host "LLM_TIMEOUT_SECONDS（超时秒数，默认 30）"
  $env:RENTMATE_AUTH_TOKENS = '{"REPLACE_WITH_RANDOM_TENANT_TOKEN":{"tenant_id":"T-DEMO-01","property_id":"P-DEMO-01"},"REPLACE_WITH_RANDOM_MANAGER_TOKEN":{"tenant_id":"M-DEMO-SESSION","property_id":"P-DEMO-01","manager_id":"M-DEMO-01"}}'
  ```

  `$env:` 只在当前会话有效，换会话需重新设置；此方式适合临时联调，**不要把令牌留在命令历史里**。
- 密钥 / 令牌纪律：真实值只写入本机 `backend/.env` 或按会话导出；**任何文档、命令、提交中不得出现真实密钥或令牌**；密钥用 `Read-Host -AsSecureString` 隐藏输入。

### LLM 段（Maintenance 与 Evidence 共用）

- 代码实际读取的四个变量是 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_TIMEOUT_SECONDS`：Maintenance 的 `LlmPriorityAssessor`（`llm/live.py`）与 Evidence 的 `QwenDocumentClassifier`（`llm/classify.py`）读取**同一组**变量——这是 `llm` 包所有模型调用的单一配置点。两个模块**保留各自现有的 fail-closed 行为**：Maintenance 未配置 / 不可用推理时按现有逻辑转人工审阅；Evidence 未配置时每次调用返回 `configuration` 失败、文档路由到人工审阅。
- 前两项（`LLM_BASE_URL` / `LLM_API_KEY`）缺失时适配器按“fail-closed”运行（见流程一的后备说明），上传本身仍为 accepted；`LLM_TIMEOUT_SECONDS` 只影响调用超时，与上传/审阅流程本身无关。

### RENTMATE_AUTH_TOKENS（服务端认证映射）

- 身份**只来自服务端 `RENTMATE_AUTH_TOKENS` 映射**：请求只携带 `Authorization: Bearer <token>`，`tenant_id` / `property_id` / `manager_id` 一律由服务端从映射解析，客户端无法通过请求头 / 请求体提供身份。
- **租户上传令牌**：映射条目含 `tenant_id` + `property_id`（演示用 `T-DEMO-01` / `P-DEMO-01`）。
- **经理审核令牌**：映射条目额外含 `manager_id`；`reviewed_by` 是该 `manager_id`（需通过 `manager_property` 绑定校验，默认拒绝，无租户回退），不是 `tenant_id`。
- 每个映射的租户/物业（或经理/物业）关系必须在共享绑定表中已验证存在。seed 同时建立租户的 resident 绑定及独立经理的 `manager_property` 绑定；经理会话不需要租户绑定。令牌映射缺失或令牌不匹配时一律 401。
- 令牌在服务端**读取时解析**（每次请求读取 `RENTMATE_AUTH_TOKENS`），演示期间换令牌不需要重启后端；LLM 与 `.env` 相关变量在启动时加载，改动后需重启 uvicorn。

## 4. 启动后端

在**已配置好上述变量的会话**（或已写好 `backend/.env` 的任意会话）、`backend/` 目录下：

```powershell
cd <仓库路径>\backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

看到 `Uvicorn running on http://127.0.0.1:8000` 即启动成功（另开终端操作 Swagger，不要关掉这个会话）。

## 5. 打开 Swagger

浏览器访问 <http://127.0.0.1:8000/docs>。

所有 evidence 端点在 `evidence` 标签下，路径前缀 `/api/evidence`。每个请求的 Authorization 面板选择 **Bearer**，填入第 3 节配置的令牌：

| 操作 | Bearer 令牌 |
|---|---|
| 租户上传 / 读取（documents、text） | 租户令牌（`RENTMATE_AUTH_TOKENS` 中 `tenant_id=T-DEMO-01` 的条目） |
| 经理审核（`POST /api/evidence/reviews`） | 经理令牌（额外带 `manager_id` 的条目） |

（再提醒：Bearer 令牌是 TEST-ONLY 原型桩身份，不是真实认证；身份字段全部来自服务端映射，请求中不携带任何身份头。）

## 演示流程一：文字 PDF → 自动分类 → 确认

1. 准备一份**可提取文字**的 PDF 样本（如发票、租约样例），≤ 10 MiB、非空。
2. Swagger → `POST /api/evidence/documents`：
    - Authorization 用**租户令牌**（Bearer）；
    - `file` 选择该 PDF；`property_id` 填 `P-DEMO-01`。
3. 200 响应形如 `{"status":"accepted","document":{...}}`。**记下响应中的实际 `document_id`**，后续步骤全部使用它。此时预期 `classification` 为 `"classified"`，`classification_type` 为五个业务类别之一（`lease_document` / `invoice` / `inspection_record` / `maintenance_photo` / `communication_record`），具体结果取决于 LLM。
4. 验证持久化：`GET /api/evidence/documents/{document_id}?property_id=P-DEMO-01`（Authorization 用租户令牌），确认 `classification` 已落库。
5. （可选）`GET /api/evidence/documents/{document_id}/text?property_id=P-DEMO-01`（Authorization 用租户令牌）：`status:"extracted"` 并返回提取文本。
6. 经理确认机器建议：`POST /api/evidence/reviews`（Authorization 用**经理令牌**），JSON 请求体：

    ```json
    {
      "status": "confirm_suggested",
      "document_id": "<上传响应中的实际 document_id>",
      "property_id": "P-DEMO-01"
    }
    ```

7. 200 返回 `EvidenceReviewResult`：`final_category`（机器建议类别）、`reviewed_by` 为令牌映射中校验通过的经理身份（`manager_id`，不是 `tenant_id`）、`reviewed_at`（UTC）。演示至此结束。

**后备**：若 LLM 未配置或不可达，第 3 步的 `classification` 会是 `"failed"`（`review_code` 如 `configuration`）。此时 `confirm_suggested` 会返回 409 `review_rejected`，改走流程二的 `select_category` 人工选类即可——这正是 fail-closed 的期望行为，不是 bug。

## 演示流程二：图片 → 转人工分类

1. 准备一张 jpg / png 图片（≤ 10 MiB、非空）。
2. Swagger → `POST /api/evidence/documents`：Authorization 用**租户令牌**；`file` 选图片；`property_id` 填 `P-DEMO-01`。
3. 200 `accepted`，记下响应中的实际 `document_id`。预期 `classification` 为 `"failed"`、`review_code` 为 `file_type_not_supported`、`review_required: true`——**文字分类仅支持 PDF（UC6）**，图片被保存但不做自动分类。
4. （可选）`GET .../text` 端点（Authorization 用租户令牌）：返回 `status:"manual_review_required"`、`code:"file_type_not_supported"`。原文始终保留、可下载。
5. 经理人工选类：`POST /api/evidence/reviews`（Authorization 用**经理令牌**），JSON 请求体（类别按图片实际内容选，示例用维修照片）：

    ```json
    {
      "status": "select_category",
      "document_id": "<上传响应中的实际 document_id>",
      "property_id": "P-DEMO-01",
      "category": "maintenance_photo"
    }
    ```

    可选手写类别：`lease_document` / `invoice` / `inspection_record` / `maintenance_photo` / `communication_record`（无 `unknown`）。
6. 200 返回 `EvidenceReviewResult`：`final_category` 为所选类别，`reviewed_by` 为令牌映射中校验通过的经理身份（`manager_id`）。演示至此结束。

## 限制与注意

- **文字 PDF 限制**：文本提取与自动分类只支持文字型 PDF。非 PDF（图片、doc、xls、txt 等）保存成功但分类记 `failed` / `file_type_not_supported`；加密、扫描无文字、损坏的 PDF 返回 `manual_review_required`（`pdf_encrypted` / `pdf_no_text` / `pdf_corrupt`），原文永不被修改或删除。
- **上传限制**：允许扩展名 `pdf, jpg, jpeg, png, doc, docx, xls, xlsx, txt`；≤ 10 MiB；空文件拒收（明确 `rejected`，不落盘）。
- **环境变量生命周期**：`backend/.env` 在 `main.py` 启动时自动加载（dotenv 不覆盖既有进程环境变量，**进程环境变量优先**）；`$env:` 只在当前会话有效，换会话需重新设置。
- **密钥 / 令牌纪律**：本文件及任何提交中不得出现真实密钥或令牌；密钥只通过 `Read-Host -AsSecureString` 输入或写入本机 `backend/.env`（git 已忽略 `backend/.env`）。
- **原型边界**：Bearer 令牌是 TEST-ONLY 桩，身份字段全部来自服务端 `RENTMATE_AUTH_TOKENS` 映射，不是真实认证；`T-DEMO-01` / `P-DEMO-01` / `M-DEMO-01` 是虚构的演示身份，不是真实用户。
