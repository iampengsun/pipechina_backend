# 项目专用容器环境

从仓库根目录执行：

```bash
python3 scripts/manage_stack.py up
```

`compose/project.yml` 是唯一部署入口，统一管理基础设施、模型服务、API、Worker、Beat、Mock IdP 和初始化任务。
Compose 自动创建项目网络和带 `pipechina_` 前缀的数据卷，无需提前创建外部网络或共享卷。
API、Worker、Beat 使用服务名连接数据库和模型服务；宿主机调试仍可使用绑定到 127.0.0.1 的端口。

启动脚本保留已有 `.env`，首次从 `.env.example` 生成，并补齐空的检索服务密钥。
默认文字模型为 Qwen，需要配置 `DASHSCOPE_API_KEY`；该 API 设置与 Docker 基础设施独立。
启动和重置前会校验文字模型密钥，缺少时提前退出；启动命令会等待容器健康，最长 30 分钟。

一次性 `migrate` 任务按顺序执行 Alembic、开发用户初始化和知识索引初始化。
API 和任务进程只有在初始化成功后才启动。所有异步队列均由 Worker 监听，包括 `knowledge_parse`。
ASR、Vision、知识模型首次运行需要下载权重；`/health/knowledge` 可检查知识组件是否真正就绪。

```bash
python3 scripts/manage_stack.py status
python3 scripts/manage_stack.py logs
python3 scripts/manage_stack.py down
```

删除并重建本项目数据见[本机Docker操作](LOCAL_DOCKER.md)。`services/` 保存项目模型服务源码，`infra/docker/` 保存后端镜像定义。

完整Compose定义20项服务，包含迁移、对象存储初始化两个一次性任务。`maintenance-worker`处理维护队列，`worker`处理模型和业务队列。新库执行`python3 scripts/manage_stack.py up`，会应用`0001 → 0002_agent_tasks → 0003_station_locations`并保留项目卷；已有`0001`库顺序升级，不清空卷。更新后核对`api`、`worker`、`maintenance-worker`、`beat`状态；一次性任务成功退出是正常状态。
