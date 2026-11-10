from celery import Celery
from celery.schedules import crontab

from app.bootstrap.config import get_settings

settings = get_settings()

# 创建项目统一的Celery应用，并注册Worker需要发现的任务模块。
celery_app = Celery(  # 这里就是一直说的Worker
    "pipechina_operations",  # Celery应用名称
    broker=settings.celery_broker_url,  # RabbitMQ消息代理
    backend=settings.celery_result_backend,  # Redis任务结果后端
    include=[  # 下面的每一行字符串都是一个py文件。每一个文件里都至少包含1个任务
        # 项目中每一个模块如果有异步任务就需要在这里统一进行注册
        "app.modules.knowledge.application.tasks",
        "app.modules.operational_graph.application.tasks",
        "app.modules.handover.application.tasks",  # 交接班任务
        "app.modules.meeting.application.tasks",  # 会议录音任务
        "app.modules.operation_event.application.tasks",  # 生产事件任务
        "app.modules.maintenance_order.application.tasks",  # 维检任务
        "app.modules.inspection.application.tasks",  # 巡检任务
        "app.modules.report.application.tasks",  # 报告任务
        "app.shared.platform.tasks",  # 平台公共任务
    ],
)

# 配置任务可靠性、队列路由和Celery Beat定时任务。
celery_app.conf.update(
    worker_enable_remote_control=False,  # 禁用Worker远程控制
    control_queue_exclusive=True,  # RabbitMQ 4.3禁止非持久且非独占的临时队列
    event_queue_exclusive=True,
    task_acks_late=True,  # 执行完成后确认，异常退出时允许重投
    task_reject_on_worker_lost=True,  # Worker丢失时拒绝当前消息
    worker_prefetch_multiplier=1,  # 长任务每次只预取一个
    broker_connection_retry_on_startup=True,  # 启动时重试Broker连接
    broker_transport_options={"confirm_publish": True},  # 启用RabbitMQ发布确认
    task_publish_retry=True,  # 发布失败时自动重试
    task_track_started=True,  # 记录STARTED状态
    timezone="UTC",
    enable_utc=True,
    # 不同类型的任务进入独立队列，Worker可以按负载和硬件分别部署。
    task_routes={
        "app.modules.operational_graph.*": {"queue": "knowledge_index"},
        "app.modules.knowledge.parse*": {"queue": "knowledge_parse"},
        "app.modules.knowledge.index*": {"queue": "knowledge_index"},
        "app.modules.knowledge.evaluate*": {"queue": "knowledge_eval"},
        "app.modules.knowledge.*": {"queue": "knowledge_query"},
        "app.modules.handover.*": {"queue": "audio"},
        "app.modules.meeting.*": {"queue": "audio"},
        "app.modules.operation_event.*": {"queue": "ai_text"},
        "app.modules.maintenance_order.*": {"queue": "maintenance"},
        "app.modules.inspection.*": {"queue": "vision"},
        "app.modules.report.*": {"queue": "report"},
        "app.shared.platform.*": {"queue": "maintenance"},
    },
    # Beat只负责按照时间表投递周期任务，任务仍由监听对应队列的Worker执行。
    beat_schedule={
        "synchronize-business-graph": {
            "task": "app.modules.operational_graph.synchronize",
            "schedule": 30.0,
            "options": {"expires": 30},
        },
        "publish-task-outbox": {
            "task": "app.shared.platform.publish_outbox",
            "schedule": 1.0,  # 每秒扫描事务Outbox
            "options": {"expires": 3},
        },
        "maintain-platform-runtime": {
            "task": "app.shared.platform.maintain_runtime",
            "schedule": 60.0,  # 每分钟清理过期资源和超时任务
            "options": {"expires": 60},
        },
        "check-work-order-reminders": {
            "task": "app.modules.maintenance_order.check_reminders",
            "schedule": 60.0,  # 每分钟检查工单提醒
            "options": {"expires": 60},
        },
        "generate-daily-operation-reports": {
            "task": "app.modules.report.generate_daily_reports",
            # UTC 16:10对应北京时间次日00:10。
            "schedule": crontab(hour=16, minute=10),
        },
        "cleanup-meeting-recordings": {
            "task": "app.modules.meeting.cleanup_recordings",
            "schedule": crontab(hour=17, minute=0),
        },
    },
)
