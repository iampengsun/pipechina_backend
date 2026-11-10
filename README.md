# 国家油气管网智能生产运营辅助系统后端 V1.3

V1.3 是 Python 3.14 模块化单体后端。代码按业务功能垂直组织，并为文字模型、语音识别、视觉模型、对象存储和知识检索定义独立端口。当前默认使用：

- 文字：阿里云百炼千问 `qwen3.7-plus`（远程 API）。
- 语音：本地 faster-whisper `large-v3`。
- 视觉：本地 MiniCPM-V 4.6。
- 文件：完整 Compose 使用项目内 SeaweedFS S3；宿主机独立运行默认使用本地私有文件系统，也可配置 AWS S3。

知识能力扩展采用 Elasticsearch 全文检索、Qdrant 向量检索和 Neo4j 关系检索。
知识业务模块已实现，包含文档治理、三索引、证据问答、权限、评测和独立解析/模型服务。
接口与验证边界见 [知识与RAG](docs/第08章_企业知识与RAG模块开发.md)，整体导航见 [项目文档](docs/README.md)。
不连接或控制 SCADA。

## 1. 代码结构

```text
app/
├── modules/
│   ├── handover/              # 交接班录音、转写、校订和交接重点
│   ├── meeting/               # 会议录音、说话人转写和知识发布
│   ├── operation_event/       # 生产运行事件抽取、版本和确认
│   ├── maintenance_order/     # 异常分级、人工审核和工单状态机
│   ├── inspection/            # 人工上传巡检图片、隐患候选和人工复核
│   ├── hazard/                # 隐患登记、风险评估、整改、验收和销项
│   ├── knowledge/             # 文档治理、混合检索、引用问答和评测
│   ├── operational_graph/     # 设备身份、证据链、知识影响复核与重复异常
│   ├── quality/               # RAG/智能体评测改进与独立验收
│   └── report/                # 日报/复盘、审核、发布和 DOCX 导出
│       ├── api/
│       ├── application/
│       ├── domain/
│       └── infrastructure/
├── ports/                     # 业务层唯一允许依赖的外部能力接口
│   ├── text.py
│   ├── speech.py
│   ├── vision.py
│   ├── storage.py
│   └── knowledge.py
├── infrastructure/
│   ├── providers/             # 千问、OpenAI、本地/自训 HTTP、Fake 适配器
│   └── storage/               # 本地文件、AWS/S3、内存测试适配器
├── shared/
│   ├── security/
│   │   ├── identity/         # 数据库用户和组织身份
│   │   ├── authentication/   # JWT 验签并解析数据库身份（AuthN）
│   │   ├── authorization/    # 角色、权限和数据范围决策（AuthZ）
│   │   └── audit/            # 安全审计上下文
│   └── ...                   # Outbox、任务和传输入口
└── bootstrap/                 # 配置及 Provider 装配

services/
├── asr/                       # 独立 faster-whisper 服务
├── vision/                    # 独立 MiniCPM-V 服务
└── knowledge_runtime/         # 文档解析、OCR、Embedding和重排

infra/
├── compose/                   # project.yml（完整项目环境）
├── docker/                    # 后端镜像构建文件
└── monitoring/                # 项目监控与告警配置

dev/mock_idp/                  # 仅供开发/测试使用的独立外部 JWT 签发服务
```

`tests/test_architecture.py` 会阻止业务模块绕过 `ports` 直接导入外部实现，也会阻止项目重新退化为一个大而全的 `AIProvider`。

部署目录和启动命令见 [infra/README.md](infra/README.md)；认证、异步任务、模型快照和模块边界见
[项目技术与功能模块介绍](docs/第01章_项目技术与功能模块介绍.md)。

## 2. 兼容基线

- 主后端：CPython `>=3.14,<3.15`；容器固定 `3.14.7-slim-trixie`。
- PostgreSQL `18.6`。
- FastAPI `0.141.1`、Pydantic `2.13.4`、SQLAlchemy `2.0.52`。
- Celery `5.6.3`、RabbitMQ `4.3.5`、Redis `8.10.1`。
- LangGraph `1.2.11`、GEPA `0.1.4`。
- 本地 ASR/视觉运行时单独使用 Python 3.13 容器，避免 PyTorch、CTranslate2、CUDA 等原生依赖影响 Python 3.14 主后端。

主后端所有直接和传递依赖固定在 `pyproject.toml` 与 `uv.lock`。本地推理服务各自拥有独立依赖文件。

## 3. 项目专用容器一键启动

### 3.1 准备配置

```bash
cp .env.example .env
```

在 `.env` 中只需先填写：

```env
DASHSCOPE_API_KEY=你的百炼API密钥
```

默认的 `QWEN_BASE_URL` 使用百炼公共 OpenAI 兼容地址，不要求额外填写 Workspace ID。

### 3.2 一次启动完整项目环境

```bash
python3 scripts/manage_stack.py up
```

脚本首次自动从 `.env.example` 创建 `.env`，为空的服务密钥会自动生成。已有配置会保留。
所有容器由 `infra/compose/project.yml` 统一管理：PostgreSQL、RabbitMQ、Redis、SeaweedFS、
Elasticsearch、Qdrant、Neo4j、知识运行时、ASR、Vision、Mock IdP、API、Worker、Beat 和初始化任务。
网络与数据卷使用 `pipechina` 项目前缀，不依赖外部网络、共享容器或外部数据卷。
迁移、开发用户和知识索引初始化由一次性 `migrate` 任务完成，成功后才启动 API 与 Worker。
首次启动会下载模型依赖和权重，模型服务就绪时间取决于下载速度。
文字模型仍按 `.env` 的 `TEXT_PROVIDER` 配置；默认 Qwen 需要 `DASHSCOPE_API_KEY`。

```bash
python3 scripts/manage_stack.py status
python3 scripts/manage_stack.py logs
python3 scripts/manage_stack.py down  # 停止并保留项目数据
python3 scripts/manage_stack.py reset # 删除本项目数据并重建
```

### 3.3 宿主机调试（可选）

```bash
uv python install 3.14
uv sync --frozen --group dev
```

PyCharm 选择项目 `.venv`。需要在宿主机调试 API 时，先停止容器 API，避免 8000 端口冲突；
`.env.example` 的地址供宿主机访问，容器内部地址由 Compose 统一覆盖。

接口文档：`http://localhost:8000/docs`。

主后端不签发 JWT，也不保存 JWT 私钥或共享密钥。开发环境的 JWT 由独立的
`mock-idp` 进程使用 RS256 私钥签发，主后端只通过 JWKS 公钥验证。获取开发令牌：

```bash
curl -X POST http://127.0.0.1:9001/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=password&username=admin&password=123456'
```

开发账号如下：

| 用户名 | 密码 | 应用状态 | 数据库权限 |
|---|---|---|---|
| `admin` | `123456` | 启用 | 全局系统管理员 |
| `peter` | `123456` | 启用 | 本组织生产调度员 |
| `tom` | `123456` | 停用 | 无；即使外部认证成功，后端仍返回 `ACCOUNT_DISABLED` |

这些固定密码和 Mock IdP 只能用于本地开发及自动化测试，不得部署到生产环境。

### 3.4 初始化生产权限管理员

生产环境必须接入真实外部 IdP 的 HTTPS issuer 和 JWKS 地址。主后端只把 JWT
当作已签名的外部身份凭据；用户、角色、权限和数据范围均从数据库读取，JWT
中自带的 `roles`、`permissions` 或 `org_scope` 不参与授权。首次部署在迁移完成后执行一次：

```bash
uv run python scripts/bootstrap_security.py \
  --issuer "你的 JWT issuer" \
  --subject "管理员 JWT 的 sub" \
  --username admin \
  --display-name "系统管理员" \
  --organization-code ROOT \
  --organization-name "总部"
```

脚本创建数据库用户、根组织和全局系统管理员角色。后续通过 `/api/v1/admin/access/*` 管理组织、用户、角色、授权期限、数据范围和撤销原因；所有授权变更都会进入审计日志。

## 4. 切换模型和存储

业务代码不需要修改，只改环境变量：

| 能力 | 当前默认 | 可替换值 |
|---|---|---|
| 文字 | `TEXT_PROVIDER=qwen` | `fake`、`openai`、`custom_http` |
| 语音 | `ASR_PROVIDER=local_http` | `fake`、`openai`、`custom_http` |
| 视觉 | `VISION_PROVIDER=local_http` | `fake`、`custom_http` |
| 存储 | `STORAGE_PROVIDER=local_filesystem` | `memory`、`s3` |

例如将文字和语音切到 OpenAI API：

```env
TEXT_PROVIDER=openai
ASR_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_TEXT_MODEL=...
OPENAI_ASR_MODEL=...
```

这里使用的是 OpenAI API，不是 ChatGPT 网页订阅。

## 5. 接入未来自训模型

推荐把自训模型部署成独立 HTTP 推理服务：

- 文字：OpenAI 兼容 `POST /v1/chat/completions`，支持 JSON Schema 结构化输出。
- ASR：multipart `POST /v1/transcriptions`，返回 `full_text`、`duration_ms` 和 `segments`。
- 视觉：multipart `POST /v1/vision/analyze`，返回 `{"findings": [...]}`。

然后设置：

```env
TEXT_PROVIDER=custom_http
CUSTOM_TEXT_BASE_URL=http://internal-text-server/v1
CUSTOM_TEXT_MODEL=company-text-v1

ASR_PROVIDER=custom_http
ASR_BASE_URL=http://internal-asr-server
ASR_MODEL=company-asr-v1

VISION_PROVIDER=custom_http
VISION_BASE_URL=http://internal-vision-server
VISION_MODEL=company-vision-v1
```

业务模块只接收统一的 `MediaRef` 和结构化结果，不知道文件来自本地还是 S3，也不知道推理来自本机、OpenAI 或自训模型。

## 6. 切换 AWS S3

参考 `.env.aws.example`：

```env
STORAGE_PROVIDER=s3
S3_ENDPOINT_URL=
S3_PUBLIC_ENDPOINT_URL=
S3_BUCKET=pipechina-production
S3_REGION=us-east-1
S3_ADDRESSING_STYLE=auto
```

在 ECS、EC2 或 EKS 上优先使用 IAM Role，保持 `S3_ACCESS_KEY` 和 `S3_SECRET_KEY` 为空。项目会使用 boto3 默认凭证链。桶本身应由 Terraform/CloudFormation 等基础设施代码创建；`ensure_bucket` 只用于本地 S3 兼容环境。

本地需要演练 S3 适配器时，可把 `.env` 改成 SeaweedFS 配置，并执行：

```bash
docker compose --env-file .env -f infra/compose/project.yml up -d seaweedfs
docker compose --env-file .env -f infra/compose/project.yml up -d --build object-storage-init
```

## 7. 数据流向与文件访问

- 本地存储文件不会因为调用千问文字模型而自动上传到百炼。
- 音频由本地 ASR 读取；图片由本地视觉服务读取。
- 语音转写后的文字、事件原文和报告来源会在对应文字任务中发送给千问。调查使用的规范、经验和气象/空间证据也按角色视图提供给文字模型。
- 气象与地图查询向配置的服务发送人工确认站场的坐标及受限参数；企业GIS按当前组织选择授权图层。认证信息保留在服务端。
- 如果切换远程 ASR，音频会发送给相应远程供应商。
- 如果未来加入远程视觉适配器，图片才会发送给相应供应商。

## 8. 验证

默认单元和 API 测试使用项目专用 PostgreSQL 中的独立数据库 `pipechina_test`，并在每个测试前重建其 `public` schema；不会读写长期运行的 `pipechina` 开发库。首次运行前在项目 PostgreSQL 中创建该独立测试库。CI 使用一次性 PostgreSQL `pipechina_ci`。

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy app scripts
uv run python -m compileall -q app alembic tests scripts services
uv run coverage run -m pytest -W error
uv run coverage report --fail-under=65
uv run pip-audit --local --skip-editable
```

默认测试使用 Fake Provider、PostgreSQL 测试库和内存存储，不调用收费 API，也不下载模型。真实百炼、本地模型和 AWS 联调均设计为显式的部署后冒烟测试。

调查记忆与经验整合接口见[第12章](docs/第12章_持续调查与经验记忆.md)，评测改进协作接口见[第11章](docs/第11章_效果评测与改进验收闭环.md)。项目只提供后端 API，不包含业务前端。


## 9. 业务流程与验收资料

调查、图谱和外部取证沿同一条业务链使用：事件先由人确认设备关联，工具读取当前资料并保存证据，风险与方案形成候选，人工审核后进入工单执行。知识、位置或设备关联变化时，使用前重新核对；气象等短时资料还检查有效期。整改与独立验收保留前后实测。

提示词版本和经验也使用同一套评测与审核规则。提示词搜索读取开发集，候选在独立事件上重新调查和标注，再通过改进验收发布；关闭工单形成的经验须经审核，后续调查检查适用条件。

| 内容 | 文档 |
|---|---|
| 调查、技能、工具和MCP任务 | [第5章](docs/第05章_异常分级与维检工单模块开发.md) |
| 评测、提示词调整与改进验收 | [第11章](docs/第11章_效果评测与改进验收闭环.md) |
| 人工补证、恢复与长期经验 | [第12章](docs/第12章_持续调查与经验记忆.md) |
| 设备身份、业务关系与影响复核 | [第13章](docs/第13章_业务图谱设备身份与变更影响复核.md) |
| 气象、GIS与地图取证 | [第14章](docs/第14章_气象预警与GIS地图工具.md) |

项目提供后端API。完整Compose有20项服务，含两个一次性初始化任务；85张应用表按`0001 → 0002_agent_tasks → 0003_station_locations`迁移。CPU模型栈需至少16GB Docker内存。气象和地图按请求调用配置的外部API，无需额外容器。

[文档目录](docs/README.md)提供业务与基础知识入口，[验收目录](dev/acceptance/README.md)保留各次检查的源码版本、配置条件和结果。流程验证、真实服务可用性和专家效果分别记录。
