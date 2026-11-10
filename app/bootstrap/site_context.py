"""Compose the optional external context provider without another container."""

from app.bootstrap.config import Settings
from app.infrastructure.site_context.http import HTTPSiteContext
from app.ports.site_context import SiteContextProvider


def create_site_context(settings: Settings) -> SiteContextProvider:
    return HTTPSiteContext(settings)
