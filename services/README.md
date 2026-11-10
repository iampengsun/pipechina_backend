# 项目自有独立服务

主后端固定使用 Python 3.14。ASR 与视觉模型通过内部 HTTP 契约隔离，因此模型运行时可以使用各自稳定兼容的 Python、CUDA 和原生依赖，不污染主后端环境。

- `asr/`：faster-whisper转写、时间段和ECAPA匿名说话人标签。
- `vision/`：MiniCPM-V 视觉分析。
- `knowledge_runtime/`：Docling文档解析、OCR、Embedding和重排，作为一个运行时容器。

这里保留服务源码、独立依赖和 Dockerfile；所有 Compose 编排集中在 `infra/compose/`。
PostgreSQL、ES 等通用软件没有项目源码，不放在这里。

项目Compose默认包含ASR、视觉和知识运行时，通过统一命令启动：

```bash
python3 scripts/manage_stack.py up
```

首次启动需要从模型仓库下载权重。生产环境应预下载并固定模型快照，同时通过内网访问控制保护 8101/8102 端口。

ASR和视觉的`/health/live`只证明进程存活；`/health/ready`在权重加载后返回就绪，首次加载返回503。说话人标签不是身份认证。CPU视觉先用缩略图判断场景，再使用原图提取设备隐患，默认FP32；完整CPU模型栈需要Docker至少16GB内存。具体交付和验收边界见[功能完成度清单](../docs/功能完成度与验收边界.md)。
