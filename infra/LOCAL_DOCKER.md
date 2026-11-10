# 本机 Docker 操作

所有命令从仓库根目录执行。

```bash
python3 scripts/manage_stack.py up     # 构建、初始化并启动完整环境
python3 scripts/manage_stack.py status # 查看服务（含一次性初始化任务）
python3 scripts/manage_stack.py logs   # 最近的日志
python3 scripts/manage_stack.py down   # 删除项目容器和网络，保留数据卷
python3 scripts/manage_stack.py reset  # 删除本项目全部数据卷，再构建并启动
```

`reset` 会永久删除本项目的数据库、对象文件、索引和模型缓存；只作用于 `pipechina` Compose 项目。
不会清理电脑上其他项目的数据。首次下载模型可能较慢，可从日志检查实际进度。

需要直接操作单个组件时：

```bash
docker compose --env-file .env -f infra/compose/project.yml ps -a
docker compose --env-file .env -f infra/compose/project.yml logs --tail=100 migrate
docker compose --env-file .env -f infra/compose/project.yml restart api worker
```

容器名由 Compose 生成，例如 `pipechina-postgres-1`；数据卷例如 `pipechina_pg_data`。
旧共享环境已迁移为项目专用配置，不再使用 `shared-infra` 外部网络和无项目前缀的数据卷。

更新后维护任务由独立`maintenance-worker`消费，排障时可同时查看：

```bash
docker compose --env-file .env -f infra/compose/project.yml logs --tail=100 worker maintenance-worker beat
```

完整启动定义包含20项服务，迁移和对象存储初始化任务完成后正常退出，其余服务持续运行。
