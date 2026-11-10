"""Business operations available to external investigation assistants."""

from uuid import NAMESPACE_URL, uuid5

from app.modules.operation_event.application import memory
from app.modules.operation_event.domain.memory_schemas import ObservationCreate
from app.modules.operational_graph.application.impacts import read_review


async def submit_observation(session, user, event_id, expected_version_id, content, request_id):
    payload = ObservationCreate(expected_version_id=expected_version_id, content=content)
    observation_id = uuid5(
        NAMESPACE_URL, f"pipechina:mcp:observation:{user.user_id}:{event_id}:{request_id}"
    )
    row = await memory.add_observation(
        session, user, event_id, payload, observation_id=observation_id
    )
    return {
        "id": str(row.id),
        "event_id": str(row.event_id),
        "event_version_id": str(row.event_version_id),
        "status": row.status,
        "version": row.version,
        "content": row.content,
        "requires_human_confirmation": row.status == "pending",
    }


async def impact_review(session, user, review_id):
    return await read_review(session, user, review_id)
