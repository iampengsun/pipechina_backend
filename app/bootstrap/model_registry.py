from app.modules.handover.domain.models import *  # noqa: F403
from app.modules.hazard.domain.models import *  # noqa: F403
from app.modules.inspection.domain.models import *  # noqa: F403
from app.modules.knowledge.domain.models import *  # noqa: F403
from app.modules.maintenance_order.domain.models import *  # noqa: F403
from app.modules.meeting.domain.models import *  # noqa: F403
from app.modules.operation_event.domain.agent_models import *  # noqa: F403
from app.modules.operation_event.domain.memory_models import *  # noqa: F403
from app.modules.operation_event.domain.models import *  # noqa: F403
from app.modules.operational_graph.application.change_capture import register_change_capture
from app.modules.operational_graph.domain.models import *  # noqa: F403
from app.modules.quality.domain.models import *  # noqa: F403
from app.modules.report.domain.models import *  # noqa: F403
from app.shared.platform.models import *  # noqa: F403
from app.shared.security.authorization.models import *  # noqa: F403
from app.shared.security.identity.models import *  # noqa: F403

register_change_capture()
