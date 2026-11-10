# 知识库语料目录

60 个主题，75 个版本文件，345 条有依据的验收问题。

## 内容与格式

- PDF 50 份、DOCX 13 份、XLSX 6 份、HTML 3 份、Markdown 2 份、TXT 1 份。
- PDF 共 430 页：图文单栏 7 份、纯扫描 7 份、双栏 18 份、文本与扫描混合 8 份、横向排版 10 份；其中 88 页没有文本层，需要 OCR。
- 每个版本正文约 3,653–4,013 字符，不含附件表格；包含编号规则、办理步骤、例外处理、复核条件、版本差异和关联文件。
- 图像包含流程图、统计图；扫描页包含灰度与轻微倾斜。Excel 包含汇总、72 行记录明细、规则正文三个工作表，以及计算公式和图表。

## 校验与使用

已完成文件哈希与大小核对、PDF 图像层/文本层检查、PDF 和 Word 共 534 页渲染及页面总览检查。配套质量测试 6 项通过。尚未进行服务端解析、OCR、切块和检索的端到端验收。

此目录保存可重复导入的原始文件，不作为运行时文件服务器目录。导入时使用 `scripts/import_knowledge_corpus.py` 经知识库 API 创建文档与版本、申请上传地址、上传文件并确认完成；添加 `--process` 才请求后续处理。存储位置由后端存储配置和上传授权决定，不在脚本中写死文件服务器地址。

语料文件及`manifest.json`已经随仓库固定保存，测试和联调直接使用这些文件。本批Word使用Noto Sans CJK SC，PDF使用嵌入的宋体。

|文号|主题|格式与结构|版本|
|---|---|---|---|
|QH-STD-2025-001|[临川输气站设备缺陷分级与销项管理](standards/QH-STD-2025-001-v2.pdf)|.pdf / illustrated|2|
|QH-STD-2025-002|[北岑分输站运行记录与电子证据管理](standards/QH-STD-2025-002-v2.pdf)|.pdf / scan|2|
|QH-STD-2025-003|[河源压气站计量数据复核与差异管理](standards/QH-STD-2025-003-v2.pdf)|.pdf / two_column|2|
|QH-STD-2025-004|[青岳末站备品备件验收与追溯管理](standards/QH-STD-2025-004-v2.pdf)|.pdf / mixed|2|
|QH-STD-2025-005|[栖霞清管站承包商进站与作业资料管理](standards/QH-STD-2025-005-v1.pdf)|.pdf / landscape|1|
|QH-STD-2025-006|[东澜计量站仪表校验记录与证书管理](standards/QH-STD-2025-006-v1.pdf)|.pdf / two_column|1|
|QH-STD-2025-007|[临川输气站生产文件版本与受控分发](standards/QH-STD-2025-007-v1.docx)|.docx / word_tables|1|
|QH-STD-2025-008|[北岑分输站巡检路线与任务完成认定](standards/QH-STD-2025-008-v1.docx)|.docx / word_tables|1|
|QH-STD-2025-009|[河源压气站交接班事项闭环管理](standards/QH-STD-2025-009-v1.xlsx)|.xlsx / workbook|1|
|QH-STD-2025-010|[青岳末站运行异常报告与升级管理](standards/QH-STD-2025-010-v1.html)|.html / web_article|1|
|QH-STD-2025-011|[栖霞清管站防汛检查与季节性风险管理](standards/QH-STD-2025-011-v1.pdf)|.pdf / illustrated|1|
|QH-STD-2025-012|[东澜计量站培训记录与岗位能力确认](standards/QH-STD-2025-012-v1.pdf)|.pdf / scan|1|
|QH-MAN-2025-001|[临川输气站过滤分离器状态监测手册](manuals/QH-MAN-2025-001-v2.pdf)|.pdf / two_column|2|
|QH-MAN-2025-002|[北岑分输站调压撬指示一致性检查手册](manuals/QH-MAN-2025-002-v2.pdf)|.pdf / mixed|2|
|QH-MAN-2025-003|[河源压气站计量撬数据质量维护手册](manuals/QH-MAN-2025-003-v2.pdf)|.pdf / landscape|2|
|QH-MAN-2025-004|[青岳末站压缩机辅助系统记录手册](manuals/QH-MAN-2025-004-v1.pdf)|.pdf / two_column|1|
|QH-MAN-2025-005|[栖霞清管站收发球设施档案维护手册](manuals/QH-MAN-2025-005-v1.docx)|.docx / word_tables|1|
|QH-MAN-2025-006|[东澜计量站阀室远传单元维护手册](manuals/QH-MAN-2025-006-v1.docx)|.docx / word_tables|1|
|QH-MAN-2025-007|[临川输气站在线分析仪样品记录手册](manuals/QH-MAN-2025-007-v1.xlsx)|.xlsx / workbook|1|
|QH-MAN-2025-008|[北岑分输站通信机柜巡检手册](manuals/QH-MAN-2025-008-v1.md)|.md / structured_text|1|
|QH-MAN-2025-009|[河源压气站供电监测记录维护手册](manuals/QH-MAN-2025-009-v1.pdf)|.pdf / illustrated|1|
|QH-MAN-2025-010|[青岳末站可燃气体检测档案手册](manuals/QH-MAN-2025-010-v1.pdf)|.pdf / scan|1|
|QH-MAN-2025-011|[栖霞清管站阴极保护巡测数据手册](manuals/QH-MAN-2025-011-v1.pdf)|.pdf / two_column|1|
|QH-MAN-2025-012|[东澜计量站视频巡检证据导出手册](manuals/QH-MAN-2025-012-v1.pdf)|.pdf / mixed|1|
|QH-OPS-2025-001|[临川输气站重复告警合并与复核流程](procedures/QH-OPS-2025-001-v2.pdf)|.pdf / landscape|2|
|QH-OPS-2025-002|[北岑分输站缺陷工单转派与复验流程](procedures/QH-OPS-2025-002-v2.pdf)|.pdf / two_column|2|
|QH-OPS-2025-003|[河源压气站计量差异联合复核流程](procedures/QH-OPS-2025-003-v2.docx)|.docx / word_tables|2|
|QH-OPS-2025-004|[青岳末站远传中断记录补录流程](procedures/QH-OPS-2025-004-v1.docx)|.docx / word_tables|1|
|QH-OPS-2025-005|[栖霞清管站备件替代技术确认流程](procedures/QH-OPS-2025-005-v1.xlsx)|.xlsx / workbook|1|
|QH-OPS-2025-006|[东澜计量站巡检图片证据复核流程](procedures/QH-OPS-2025-006-v1.html)|.html / web_article|1|
|QH-OPS-2025-007|[临川输气站班组待办清单移交流程](procedures/QH-OPS-2025-007-v1.pdf)|.pdf / illustrated|1|
|QH-OPS-2025-008|[北岑分输站生产日报更正与重发流程](procedures/QH-OPS-2025-008-v1.pdf)|.pdf / scan|1|
|QH-OPS-2025-009|[河源压气站会议决议转工单流程](procedures/QH-OPS-2025-009-v1.pdf)|.pdf / two_column|1|
|QH-OPS-2025-010|[青岳末站季节性专项检查闭环流程](procedures/QH-OPS-2025-010-v1.pdf)|.pdf / mixed|1|
|QH-INC-2025-001|[栖霞清管站计量曲线突跳调查报告](cases/QH-INC-2025-001-v2.pdf)|.pdf / landscape|2|
|QH-INC-2025-002|[东澜计量站远传数值冻结调查报告](cases/QH-INC-2025-002-v2.pdf)|.pdf / two_column|2|
|QH-INC-2025-003|[临川输气站备件批次错配调查报告](cases/QH-INC-2025-003-v1.docx)|.docx / word_tables|1|
|QH-INC-2025-004|[北岑分输站交接事项遗漏调查报告](cases/QH-INC-2025-004-v1.docx)|.docx / word_tables|1|
|QH-INC-2025-005|[河源压气站排水沟堵塞复盘报告](cases/QH-INC-2025-005-v1.xlsx)|.xlsx / workbook|1|
|QH-INC-2025-006|[青岳末站巡检照片误判复核报告](cases/QH-INC-2025-006-v1.txt)|.txt / plain_text|1|
|QH-INC-2025-007|[栖霞清管站仪表证书关联错误报告](cases/QH-INC-2025-007-v1.pdf)|.pdf / illustrated|1|
|QH-INC-2025-008|[东澜计量站日报指标重复统计报告](cases/QH-INC-2025-008-v1.pdf)|.pdf / scan|1|
|QH-INC-2025-009|[临川输气站承包商资料交付缺项报告](cases/QH-INC-2025-009-v1.pdf)|.pdf / two_column|1|
|QH-INC-2025-010|[北岑分输站通信主备切换复盘报告](cases/QH-INC-2025-010-v1.pdf)|.pdf / mixed|1|
|QH-HOV-2025-001|[河源压气站周一白班运行交接记录](handovers/QH-HOV-2025-001-v2.pdf)|.pdf / landscape|2|
|QH-HOV-2025-002|[青岳末站夜班通信恢复交接记录](handovers/QH-HOV-2025-002-v2.pdf)|.pdf / two_column|2|
|QH-HOV-2025-003|[栖霞清管站雨后巡检交接记录](handovers/QH-HOV-2025-003-v1.docx)|.docx / word_tables|1|
|QH-HOV-2025-004|[东澜计量站检修窗口结束交接记录](handovers/QH-HOV-2025-004-v1.docx)|.docx / word_tables|1|
|QH-HOV-2025-005|[临川输气站计量复核值班记录](handovers/QH-HOV-2025-005-v1.xlsx)|.xlsx / workbook|1|
|QH-HOV-2025-006|[北岑分输站专项检查事项交接记录](handovers/QH-HOV-2025-006-v1.md)|.md / structured_text|1|
|QH-HOV-2025-007|[河源压气站备件到货值班记录](handovers/QH-HOV-2025-007-v1.pdf)|.pdf / illustrated|1|
|QH-HOV-2025-008|[青岳末站月末资料归档交接记录](handovers/QH-HOV-2025-008-v1.pdf)|.pdf / scan|1|
|QH-NOT-2025-001|[栖霞清管站关于调整缺陷复核时限的通知](notices/QH-NOT-2025-001-v2.pdf)|.pdf / two_column|2|
|QH-NOT-2025-002|[东澜计量站关于开展计量数据专项复核的通知](notices/QH-NOT-2025-002-v1.pdf)|.pdf / mixed|1|
|QH-NOT-2025-003|[临川输气站关于加强汛后设施复查的通知](notices/QH-NOT-2025-003-v1.pdf)|.pdf / landscape|1|
|QH-NOT-2025-004|[北岑分输站关于启用新版交接清单的通知](notices/QH-NOT-2025-004-v1.pdf)|.pdf / two_column|1|
|QH-NOT-2025-005|[河源压气站关于清理备件证书缺项的通知](notices/QH-NOT-2025-005-v1.docx)|.docx / word_tables|1|
|QH-NOT-2025-006|[青岳末站关于统一日报时间口径的通知](notices/QH-NOT-2025-006-v1.docx)|.docx / word_tables|1|
|QH-NOT-2025-007|[栖霞清管站关于开展岗位培训复核的通知](notices/QH-NOT-2025-007-v1.xlsx)|.xlsx / workbook|1|
|QH-NOT-2025-008|[东澜计量站关于开展季度档案抽查的通知](notices/QH-NOT-2025-008-v1.html)|.html / web_article|1|

扫描PDF的扫描页只有图像，没有隐藏文字层。混合PDF交替包含文字页与扫描页。

批量导入使用 scripts/import_knowledge_corpus.py。manifest.json 只登记业务文件，目录及校验报告不上传。
