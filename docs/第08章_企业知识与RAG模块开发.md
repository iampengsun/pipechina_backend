# 第8章：企业知识与RAG

RAG在回答前检索证据。Elasticsearch按词和编号查找文本，Qdrant按向量相似度召回，Neo4j按关系找材料；Embedding产生向量，Reranker对有限候选重新评分。Docling与OCR先把原始文件转换成可定位内容，这些组件不能互相代替。

相关基础：[RAG全流程](基础知识/05_检索文档与多模态.md#t23)、[全文检索](基础知识/05_检索文档与多模态.md#t24)、[向量检索](基础知识/05_检索文档与多模态.md#t25)、[图检索](基础知识/05_检索文档与多模态.md#t26)、[Embedding](基础知识/05_检索文档与多模态.md#t27)、[重排与RRF](基础知识/05_检索文档与多模态.md#t28)、[文档解析](基础知识/05_检索文档与多模态.md#t29)、[评价指标](基础知识/06_效果评测与验收.md#t33)。

## 8.1 RAG先检索，再根据证据回答

RAG由检索增强生成组成：先从当前用户可见的知识中找证据，再让模型依据证据回答。Embedding、搜索引擎或图数据库都不是最终答案；它们帮助选择上下文，答案还须带来源与引用并通过核查。

项目同时提供独立知识问答和调查工具`search_knowledge`。共享的是入库、授权、检索与引用规则；调查还会结合当前事件、历史经验和人工审核。

## 8.2 文档、版本、片段与事实

| 对象 | 解释 |
|---|---|
| KnowledgeBase | 知识库范围、组织和读取ACL |
| KnowledgeDocument | 文档编号、标题、类别和读取ACL |
| KnowledgeVersion | 文件、版本号、生效时间、审核发布、索引代次 |
| KnowledgeChunk | 可检索片段、父级上下文和定位 |
| KnowledgeFact | 从原文抽出的实体关系，绑定证据片段及引用 |
| KnowledgeRun | 一次问答的输入、检索轨迹、回答和事件流 |

版本是授权和有效性的基本边界。新文件须作为新版本入库；向量相似不代表设备型号或规范适用范围一致，检索还要使用组织、版本、型号和时间过滤。

## 8.3 先认识Elasticsearch

ES保存JSON文档，索引类似一组采用同样字段规则的检索记录。`mapping`规定字段类型，`text`先分词后做全文匹配，`keyword`按完整值匹配。倒排索引保存词项到文档的对应关系，BM25根据词频等信息对全文结果评分。

分析器决定一段文字变成哪些词项。本项目标题与正文采用`cjk`，标识和版本字段为`keyword`；定位信息保留但不参加索引。分词结果影响召回，不应假定设备编号按自然语言分词后仍能精确匹配。

### ES基础练习

本机ES入口为`http://127.0.0.1:9200`，需要项目配置的认证。以下使用独立练习索引，不写业务知识索引。先从`.env`读取并在当前终端设置`ES_PASSWORD`，不要将密钥写进命令文件。

```bash
curl -u "elastic:$ES_PASSWORD" -X PUT http://127.0.0.1:9200/pipechina-learning-docs \
  -H 'Content-Type: application/json' -d '{"mappings":{"properties":{
    "text":{"type":"text","analyzer":"cjk"},"device_code":{"type":"keyword"}
  }}}'
curl -u "elastic:$ES_PASSWORD" -X PUT \
  'http://127.0.0.1:9200/pipechina-learning-docs/_doc/1?refresh=wait_for' \
  -H 'Content-Type: application/json' \
  -d '{"text":"教学阀门发生渗漏","device_code":"DEMO-01"}'
curl -u "elastic:$ES_PASSWORD" -X POST \
  http://127.0.0.1:9200/pipechina-learning-docs/_search \
  -H 'Content-Type: application/json' -d '{"query":{"match":{"text":"阀门渗漏"}}}'
```

`match`先分析查询文字；`term`适合对`keyword`做完整值过滤。例如`{"term":{"device_code":"DEMO-01"}}`。搜索结果中`_id`是记录ID，`_score`是当前查询相关性分数，`_source`是原始JSON，分数不能跨不同查询直接解释成置信度。

需要同时满足文字相关与设备编号时，可使用`bool`组合查询：`must`放`match`，`filter`放精确`term`。过滤决定候选是否允许进入结果，相关性排序决定候选先后；项目的版本白名单正是额外的过滤约束，不能让高分越过权限。

```bash
curl -u "elastic:$ES_PASSWORD" -X POST http://127.0.0.1:9200/pipechina-learning-docs/_analyze \
  -H 'Content-Type: application/json' -d '{"analyzer":"cjk","text":"阀门渗漏"}'
curl -u "elastic:$ES_PASSWORD" http://127.0.0.1:9200/pipechina-learning-docs/_mapping
```

先观察`tokens`中的分词，再对照mapping解释`match`与`term`的差别。项目业务索引带有代次，更新结构或向量配置时应按新代次处理，不通过修改已发布内容来假装重新建索引。

用`_analyze`查看实际分词，用`_mapping`查看字段。Bulk HTTP成功还要检查每个条目是否失败；写入后等待refresh才能搜索到。完成练习后可只删除`pipechina-learning-docs`，不要使用通配符清理项目索引。

## 8.4 向量与重排

Embedding把片段映射为固定维度向量，Qdrant按向量相近程度召回。向量维度、模型版本和索引必须匹配；模型升级不能只替换名称而沿用不兼容向量。

全文适合明确词项与编号，向量帮助召回不同表达，关系图帮助扩展实体关联。不同检索分数不直接相加当作统一概率；项目按检索流程合并候选，再由Reranker对问题与片段重新评分。

## 8.5 先认识Neo4j

Neo4j使用属性图：节点表示对象，关系连接节点，标签用于分类，节点和关系都可保存属性。路径是一系列节点与关系。关系方向表达存储或业务引用，不天然代表因果。

Cypher用图模式查询。`(d:LearningDevice)`表示带标签的节点，`[r:SHOWS]`表示关系，`MATCH`匹配，`WHERE`过滤，`RETURN`选择返回值，`MERGE`匹配或创建，`SET`赋属性。

### Neo4j基础练习

打开本机`http://127.0.0.1:7474`，按项目配置连接`bolt://127.0.0.1:7687`。用户名为neo4j，密码读取`.env`配置。先执行`RETURN 1 AS connected`，再用单独教学标签练习：

```cypher
MERGE (d:LearningDevice {lesson:'basics', code:'DEMO-01'})
SET d.name='教学阀门'
MERGE (s:LearningSymptom {lesson:'basics', name:'渗漏'})
MERGE (d)-[:SHOWS]->(s)
RETURN d, s;
```

```cypher
MATCH (d:LearningDevice)-[:SHOWS]->(s:LearningSymptom)
WHERE d.lesson='basics' AND s.lesson='basics'
RETURN d.code AS device, s.name AS symptom
LIMIT 20;
```

`CREATE`重复执行可能增加重复记录；`MERGE`的模式设计和唯一约束共同影响幂等性。查询里使用`$parameter`传值，避免把外部文字拼接成Cypher。练习清理只匹配`LearningDevice`、`LearningSymptom`及`lesson='basics'`，不要删除业务标签。

## 8.6 两种图谱的分工

RAG文档图使用`KnowledgeEntity`和`KnowledgeFact`，事实节点绑定版本、原文引用和证据片段。实体字符串同名并不等于完成企业设备消歧。实际实现见[http.py](../app/infrastructure/knowledge/http.py)。

业务图使用`BusinessRecord`和`BUSINESS_LINK`，从数据库中的业务ID与人工关联构建，负责事件、工单、验收、经验和知识依赖。它使用单独快照，详情见[第13章](第13章_业务图谱设备身份与变更影响复核.md)。

## 8.7 入库与发布流程

```text
创建知识库/文档 → 创建版本与上传授权 → 上传并验证
→ 解析/OCR → 分段与父级上下文 → 生成向量、抽取事实
→ 写入索引 → 人工审核 → 发布
```

解析保留页码、段落、表格和定位。分段控制上下文长度，同时保存父级文字；表格片段应保留表头，避免将某一数值与其含义拆散。人工纠正文档解析结果后重新处理相应阶段，保留修订和原因。

ES、Qdrant与Neo4j写入不在PostgreSQL同一事务中。发布门槛、索引代次、重试清理和数据库复核共同决定版本是否可用。部分索引成功不能直接把版本标记为published。

## 8.8 检索、回答与引用核查

查询前构造用户可见版本白名单，结合发布状态、有效期、型号和来源ACL。召回后再回查数据库，防止旧索引保留已经撤回或撤权的资料。会议发布的知识继续继承会议来源ACL。

`SearchInput`支持`hybrid`、`fulltext`、`vector`、`graph`和有预算限制的`global`查询；`limit`为1–20，可指定知识库、型号、版本、查询时间和会话。查询扩展也受数量与上下文预算约束。

生成结果拆成claims，每条claim引用真实evidence ID和原文quote。服务检查引用存在、原文支持和有效性，保留冲突、未知及拒答。没有证据时不让模型自由补写答案；来源中的指令按资料处理，不能覆盖系统规则。

异步问答通过运行事件流提供检索、生成、核查及完成阶段。SSE支持按事件位置续读，不等同于每个Token实时流出；断线后重连仍会重新鉴权。

## 8.9 代码与验证

从[Schema](../app/modules/knowledge/domain/schemas.py)进入[API](../app/modules/knowledge/api/router.py)，再阅读[pipeline.py](../app/modules/knowledge/application/pipeline.py)、[chunking.py](../app/modules/knowledge/application/chunking.py)、[retrieval.py](../app/modules/knowledge/application/retrieval.py)和[answers.py](../app/modules/knowledge/application/answers.py)。评测原理与改进闭环见第11章。

[真实索引测试](../tests/test_knowledge_external.py)验证版本过滤、写入和删除；[知识质量测试](../tests/test_knowledge_quality.py)检查表格、引用与指标。真实模型与专家效果另行验证，不能把Fake回归或一次召回成功当作完整问答效果达标。

## HTTP接口速查

以下为当前OpenAPI中的实际方法与路径，已包含`/api/v1`前缀；健康接口使用独立路径。请求体列是Schema名称，完整字段、查询参数、响应和约束见`/openapi.json`。源码链接指向处理函数所在文件。

| 方法 | 路径 | 请求体Schema | 处理函数 |
|---|---|---|---|
| GET | `/api/v1/knowledge/bases` | — | [bases](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/bases` | `BaseCreate` | [create_base](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/bases/{base_id}/acl` | `ACLUpdate` | [base_acl](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/bases/{base_id}/documents` | — | [documents](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/conversations` | `ConversationCreate` | [conversation](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/conversations/{conversation_id}/runs` | — | [conversation_runs](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/documents` | `DocumentCreate` | [create_document](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/documents/{document_id}/acl` | `ACLUpdate` | [document_acl](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/documents/{document_id}/versions` | — | [versions](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/documents/{document_id}/versions` | `VersionCreate` | [create_version](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/runs` | `SearchInput` | [ask](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/runs/{run_id}` | — | [read_run](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/runs/{run_id}/events` | — | [events](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/runs/{run_id}/feedback` | `FeedbackInput` | [feedback](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/runs/{run_id}:cancel` | — | [cancel](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/search` | `SearchInput` | [search](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/versions/{version_id}/artifact` | — | [artifact](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}/artifact:correct` | `ArtifactCorrection` | [correct_artifact](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/versions/{version_id}/diff/{other_id}` | — | [version_diff](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/versions/{version_id}/evidence` | — | [evidence](../app/modules/knowledge/api/router.py) |
| GET | `/api/v1/knowledge/versions/{version_id}/original` | — | [original](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}/uploads:complete` | — | [complete](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}:process` | — | [process](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}:publish` | — | [publish](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}:review` | `Review` | [review](../app/modules/knowledge/api/router.py) |
| POST | `/api/v1/knowledge/versions/{version_id}:withdraw` | `Reason` | [withdraw](../app/modules/knowledge/api/router.py) |
