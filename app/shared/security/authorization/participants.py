"""Validate real, active participants against the target resource and effective grants."""

from app.shared.errors import ConflictError
from app.shared.security.authentication.schemas import AuthenticatedIdentity
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.repository import load_current_user
from app.shared.security.identity.models import OrganizationUnit, UserAccount


async def validate_assignee(session, account_id, organization_id, *, permissions, owner_id):
    account = await session.get(UserAccount, account_id, populate_existing=True)
    if account is None or not account.active or account.organization_unit_id != organization_id:
        raise ConflictError("assignee must be an active account in the resource organization")
    organization = await session.get(OrganizationUnit, organization_id, populate_existing=True)
    if organization is None or not organization.active:
        raise ConflictError("assignee organization is inactive")
    current = await load_current_user(
        session,
        AuthenticatedIdentity(
            user_id=account.id,
            issuer=account.external_issuer,
            subject=account.external_subject,
            username=account.username,
            display_name=account.display_name,
            organization_unit_id=organization_id,
            authz_version=account.authz_version,
        ),
    )
    for permission in permissions:
        require_data_scope(
            current, organization_id, permission, owner_id=owner_id, assignee_id=account.id
        )
    return current
