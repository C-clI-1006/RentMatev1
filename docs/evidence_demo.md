# 证据模块演示操作说明（Windows PowerShell）

面向组员的简明操作说明：在 Windows PowerShell 中跑通证据模块的两条演示流程。

## 本演示覆盖 / 不覆盖

覆盖：上传 →（文字 PDF 自动分类）→ 经理审阅确认 / 人工选类，全部通过 Swagger 完成。

**不覆盖（演示时不要声称）：**

- **association 尚未完成**：演示文档始终保持 `association: "unassociated"`，不要演示或声称关联功能。
- **原型认证**：身份来自请求头 `X-Prototype-*`，是 TEST-ONLY 的桩，**不是真实认证**；任何人可设置这些头，绑定检查只是范围检查，不是授权。
- **不声称整个 UC6 已完成**：本演示只展示上传 → 分类 → 审阅这一条样例子路径，不代表分类准确性，也不代表 UC6 全部范围。
- **文档中不含任何真实密钥**。

## 0. 约定

- 演示身份（seed 脚本写入的虚构数据，非真实用户）：`T-DEMO-01` / `P-DEMO-01`。
- 所有请求的 `property_id` 使用 `P-DEMO-01`。
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

- TEST-ONLY 脚本：创建虚构租户 `T-DEMO-01`、虚构物业 `P-DEMO-01` 及 `role='manager'` 绑定。
- 幂等：行已存在则不覆盖、不重复写；绑定已存在但 role 不是 manager 时报错回滚。
- 不会创建任何 evidence / 分类 / 审阅记录，不写任何凭据。
- 成功后输出 `seed complete: db=...\backend\rentmate.db`（默认库路径与 `main.py` 默认一致）。

## 3. 配置四个 LLM 环境变量

**重要：`backend/main.py` 不会自动加载 `.env` 文件（代码中没有 dotenv）。**
根目录的 `.env.example` 只是变量名参考；真实值必须在**启动 uvicorn 的同一个 PowerShell 会话**里用 `$env:` 设置。

密钥用 `Read-Host -AsSecureString` 隐藏输入，**不要把密钥写进文档、命令或命令行参数**（`$env:LLM_API_KEY="..."` 会留在命令历史里）：

```powershell
$env:LLM_BASE_URL = Read-Host "LLM_BASE_URL（代理地址，如 https://.../v1）"
$secKey = Read-Host "LLM_API_KEY（隐藏输入）" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secKey).Password
$env:LLM_MODEL = Read-Host "LLM_MODEL（模型名）"
$env:LLM_TIMEOUT_S = Read-Host "LLM_TIMEOUT_S（超时秒数，默认 30）"
```

说明：

- 代码实际读取的四个变量是 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_TIMEOUT_S`（`backend/llm/classify.py`，`QwenDocumentClassifier`，即 evidence 模块用的文档分类适配器）。`.env.example` 中的 LLM 段现在与代码一致（`LLM_TIMEOUT_S=30`，超时秒数，默认 30）。
- 这四个变量是 `llm` 包模型调用的单一配置点，按设计供未来其他模块（如 maintenance 优先级评估）共用，但目前仓库里只有文档分类适配器读取它们；`LLM_TIMEOUT_S` 只影响分类调用，与上传/审阅流程无关。
- 前两项缺失时适配器按“fail-closed”运行：每次调用返回 `configuration` 失败，文档路由到人工审阅（见流程一的后备说明），上传本身仍为 accepted。

## 4. 启动后端

在**已设置上面环境变量的同一个会话**、`backend/` 目录下：

```powershell
cd <仓库路径>\backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

看到 `Uvicorn running on http://127.0.0.1:8000` 即启动成功（另开终端操作 Swagger，不要关掉这个会话）。

## 5. 打开 Swagger

浏览器访问 <http://127.0.0.1:8000/docs>。

所有 evidence 端点在 `evidence` 标签下，路径前缀 `/api/evidence`。每个请求的 Parameters 面板里需要填两个请求头：

| 参数 | 值 |
|---|---|
| `X-Prototype-Tenant-Id`（必填） | `T-DEMO-01` |
| `X-Prototype-Property-Id`（必填） | `P-DEMO-01` |
| `X-Prototype-Manager-Id`（可选） | 可留空 |

（再提醒：这是原型桩身份，不是真实认证。）

## 演示流程一：文字 PDF → 自动分类 → 确认

1. 准备一份**可提取文字**的 PDF 样本（如发票、租约样例），≤ 10 MiB、非空。
2. Swagger → `POST /api/evidence/documents`：
   - 请求头按上表填写；
   - `file` 选择该 PDF；`property_id` 填 `P-DEMO-01`。
3. 200 响应形如 `{"status":"accepted","document":{...}}`。**记下响应中的实际 `document_id`**，后续步骤全部使用它。此时预期 `classification` 为 `"classified"`，`classification_type` 为五个业务类别之一（`lease_document` / `invoice` / `inspection_record` / `maintenance_photo` / `communication_record`），具体结果取决于 LLM。
4. 验证持久化：`GET /api/evidence/documents/{document_id}?property_id=P-DEMO-01`（带请求头），确认 `classification` 已落库。
5. （可选）`GET /api/evidence/documents/{document_id}/text?property_id=P-DEMO-01`：`status:"extracted"` 并返回提取文本。
6. 经理确认机器建议：`POST /api/evidence/reviews`（带请求头），JSON 请求体：

   ```json
   {
     "status": "confirm_suggested",
     "document_id": "<上传响应中的实际 document_id>",
     "property_id": "P-DEMO-01"
   }
   ```

7. 200 返回 `EvidenceReviewResult`：`final_category`（机器建议类别）、`reviewed_by` 为 `T-DEMO-01`、`reviewed_at`（UTC）。演示至此结束。

**后备**：若 LLM 未配置或不可达，第 3 步的 `classification` 会是 `"failed"`（`review_code` 如 `configuration`）。此时 `confirm_suggested` 会返回 409 `review_rejected`，改走流程二的 `select_category` 人工选类即可——这正是 fail-closed 的期望行为，不是 bug。

## 演示流程二：图片 → 转人工分类

1. 准备一张 jpg / png 图片（≤ 10 MiB、非空）。
2. Swagger → `POST /api/evidence/documents`：请求头同上；`file` 选图片；`property_id` 填 `P-DEMO-01`。
3. 200 `accepted`，记下响应中的实际 `document_id`。预期 `classification` 为 `"failed"`、`review_code` 为 `file_type_not_supported`、`review_required: true`——**文字分类仅支持 PDF（UC6）**，图片被保存但不做自动分类。
4. （可选）`GET .../text` 端点：返回 `status:"manual_review_required"`、`code:"file_type_not_supported"`。原文始终保留、可下载。
5. 经理人工选类：`POST /api/evidence/reviews`（带请求头），JSON 请求体（类别按图片实际内容选，示例用维修照片）：

   ```json
   {
     "status": "select_category",
     "document_id": "<上传响应中的实际 document_id>",
     "property_id": "P-DEMO-01",
     "category": "maintenance_photo"
   }
   ```

   可选手写类别：`lease_document` / `invoice` / `inspection_record` / `maintenance_photo` / `communication_record`（无 `unknown`）。
6. 200 返回 `EvidenceReviewResult`：`final_category` 为所选类别，`reviewed_by` 为 `T-DEMO-01`。演示至此结束。

## 限制与注意

- **文字 PDF 限制**：文本提取与自动分类只支持文字型 PDF。非 PDF（图片、doc、xls、txt 等）保存成功但分类记 `failed` / `file_type_not_supported`；加密、扫描无文字、损坏的 PDF 返回 `manual_review_required`（`pdf_encrypted` / `pdf_no_text` / `pdf_corrupt`），原文永不被修改或删除。
- **上传限制**：允许扩展名 `pdf, jpg, jpeg, png, doc, docx, xls, xlsx, txt`；≤ 10 MiB；空文件拒收（明确 `rejected`，不落盘）。
- **环境变量生命周期**：`$env:` 只在当前会话有效；换会话需重新设置。`main.py` 不读 `.env`。
- **密钥纪律**：本文件及任何提交中不得出现真实密钥；密钥只通过 `Read-Host -AsSecureString` 输入。
- **原型边界**：头身份是 TEST-ONLY 桩；`T-DEMO-01` / `P-DEMO-01` 是虚构 seed 行，不是真实用户。
