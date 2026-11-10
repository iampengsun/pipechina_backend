"""External mock identity provider used for local development and integration tests."""

from dev.mock_idp.main import MockIdPSettings, create_mock_idp_app

__all__ = ["MockIdPSettings", "create_mock_idp_app"]
