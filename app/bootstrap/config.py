from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "国家油气管网智能生产运营辅助系统"
    api_prefix: str = "/api/v1"
    cors_allowed_origins: tuple[str, ...] = ("http://localhost:5173",)
    mcp_allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "testserver")
    mcp_allowed_origins: tuple[str, ...] = ("http://127.0.0.1:*", "http://localhost:*")
    database_url: str = "postgresql+asyncpg://peter:123456@127.0.0.1:5432/pipechina"

    jwt_issuer: str = "http://mock-idp:9001"
    jwt_audience: str = "pipechina-backend"
    jwt_algorithm: Literal["RS256", "ES256"] = "RS256"
    jwt_jwks_url: str = "http://127.0.0.1:9001/.well-known/jwks.json"
    jwt_jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    jwt_jwks_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    auto_create_schema: bool = False
    auto_seed: bool = False
    run_tasks_inline: bool = False
    knowledge_backend: Literal["fake", "http"] = "http"
    es_url: str = "http://127.0.0.1:9200"
    es_username: str = "elastic"
    es_password: SecretStr | None = None
    es_api_key: SecretStr | None = None
    es_index_prefix: str = Field(default="pipechina-knowledge", pattern=r"^[a-z][a-z0-9_-]+$")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection_prefix: str = Field(
        default="pipechina_knowledge", pattern=r"^[a-z][a-z0-9_]+$"
    )
    neo4j_url: str = "http://127.0.0.1:7474"
    neo4j_username: str = "neo4j"
    neo4j_password: SecretStr | None = None
    neo4j_database: str = Field(default="neo4j", pattern=r"^[a-zA-Z0-9_-]+$")
    knowledge_runtime_url: str = "http://127.0.0.1:8103"
    knowledge_service_api_key: SecretStr | None = None
    embedding_model: str = "configured-embedding-model"
    embedding_revision: str = ""
    embedding_dimensions: int = Field(default=1024, ge=1, le=65536)
    reranker_model: str = "configured-reranker-model"
    reranker_revision: str = ""
    knowledge_ca_file: Path | None = None
    knowledge_timeout_seconds: float = Field(default=30, gt=0, le=900)
    knowledge_batch_size: int = Field(default=64, ge=1, le=512)
    knowledge_max_document_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    knowledge_max_document_pages: int = Field(default=2000, ge=1, le=10000)
    job_lease_seconds: int = Field(default=120, ge=30, le=3600)
    job_heartbeat_interval_seconds: int = Field(default=20, ge=1, le=300)

    @model_validator(mode="after")
    def validate_knowledge(self) -> Settings:
        """校验知识服务地址、租约和生产认证配置。"""

        if self.job_heartbeat_interval_seconds * 2 >= self.job_lease_seconds:
            raise ValueError("heartbeat interval must be less than half the lease duration")
        from urllib.parse import urlsplit

        for url in (
            self.es_url,
            self.qdrant_url,
            self.neo4j_url,
            self.knowledge_runtime_url,
        ):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("knowledge endpoints require HTTP(S) URLs without credentials")
        if self.app_env == "production":
            if self.knowledge_backend == "fake":
                raise ValueError("production knowledge backend must not be fake")
            if not (self.es_api_key or self.es_password) or not self.qdrant_api_key:
                raise ValueError("production search services require authentication")
            if not self.neo4j_password or not self.knowledge_service_api_key:
                raise ValueError("production graph and model services require authentication")
        return self

    qweather_api_host: str | None = None
    qweather_api_key: SecretStr | None = None
    qweather_jwt: SecretStr | None = None
    amap_api_key: SecretStr | None = None
    enterprise_gis_layers: dict[str, str] = Field(default_factory=dict)
    enterprise_gis_token: SecretStr | None = None
    enterprise_gis_fields: str = Field(default="OBJECTID,name,type", pattern=r"^[A-Za-z0-9_,]+$")
    site_context_radius_meters: int = Field(default=3000, ge=100, le=10000)
    site_context_timeout_seconds: float = Field(default=10, gt=0, le=30)
    site_context_ttl_seconds: int = Field(default=300, ge=30, le=900)

    @model_validator(mode="after")
    def validate_site_context(self) -> Settings:
        from urllib.parse import urlsplit
        from uuid import UUID

        if self.qweather_api_key and self.qweather_jwt:
            raise ValueError("configure one QWeather authentication method")
        if (self.qweather_api_key or self.qweather_jwt) and not self.qweather_api_host:
            raise ValueError("QWeather credentials require QWEATHER_API_HOST")
        if self.qweather_api_host:
            url = urlsplit(self.qweather_api_host)
            if (
                url.scheme != "https"
                or not url.hostname
                or not url.hostname.endswith(".qweatherapi.com")
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
                or url.username
                or url.port
            ):
                raise ValueError("QWeather host must be your HTTPS *.qweatherapi.com API host")
        for org, endpoint in self.enterprise_gis_layers.items():
            UUID(org)
            url = urlsplit(endpoint)
            import re

            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.query
                or url.fragment
                or not re.search(r"/(FeatureServer|MapServer)/[0-9]+$", url.path)
            ):
                raise ValueError("enterprise GIS requires an organization-scoped ArcGIS layer URL")
        return self

    text_provider: Literal["fake", "qwen", "openai", "custom_http"] = "qwen"
    asr_provider: Literal["fake", "local_http", "openai", "custom_http"] = "local_http"
    vision_provider: Literal["fake", "local_http", "custom_http"] = "local_http"
    storage_provider: Literal["memory", "local_filesystem", "s3"] = "local_filesystem"

    celery_broker_url: str = "amqp://guest:guest@localhost:5672//"
    celery_result_backend: str = "redis://localhost:6379/1"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None
    s3_bucket: str = "pipechina-private"
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = True
    s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"

    local_storage_root: Path = Path("./data/objects")
    local_storage_public_base_url: str = "http://localhost:8000"
    local_storage_signing_secret: SecretStr = SecretStr(
        "development-local-storage-secret-change-me"
    )

    dashscope_api_key: SecretStr | None = None
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_text_model: str = "qwen3.7-plus"

    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_text_model: str = "gpt-5.5"
    openai_asr_model: str = "gpt-4o-transcribe"

    custom_text_base_url: str = "http://127.0.0.1:8201/v1"
    custom_text_model: str = "custom-text-model"
    custom_text_api_key: SecretStr | None = None

    asr_base_url: str = "http://127.0.0.1:8101"
    asr_model: str = "large-v3"
    asr_api_key: SecretStr | None = None
    vision_base_url: str = "http://127.0.0.1:8102"
    vision_model: str = "openbmb/MiniCPM-V-4.6"
    vision_api_key: SecretStr | None = None
    provider_timeout_seconds: float = 60.0
    media_provider_timeout_seconds: float = 900.0
    provider_max_retries: int = Field(default=2, ge=0, le=5)
    request_rate_limit_per_minute: int = Field(default=600, ge=1, le=100000)
    ops_metrics_token: SecretStr | None = None
    provider_trust_env: bool = False
    job_heartbeat_timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    outbox_max_attempts: int = Field(default=8, ge=1, le=100)
    outbox_base_retry_seconds: int = Field(default=5, ge=1, le=3600)

    max_audio_bytes: int = 500 * 1024 * 1024
    meeting_recording_retention_days: int | None = Field(default=None, ge=1, le=36500)
    allowed_audio_types: tuple[str, ...] = (
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
        "audio/x-m4a",
    )
    max_image_bytes: int = 20 * 1024 * 1024
    allowed_image_types: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")

    @model_validator(mode="after")
    def validate_production_safety(self) -> Settings:
        """拒绝生产环境中的不安全配置组合。"""

        if self.app_env != "production":
            return self
        if not self.jwt_issuer.startswith("https://"):
            raise ValueError("production JWT_ISSUER must use HTTPS")
        if not self.jwt_jwks_url.startswith("https://"):
            raise ValueError("production JWT_JWKS_URL must use HTTPS")
        if "*" in self.cors_allowed_origins:
            raise ValueError("production CORS_ALLOWED_ORIGINS must not contain a wildcard")
        if self.auto_create_schema:
            raise ValueError("AUTO_CREATE_SCHEMA must be false in production; use Alembic")
        if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql+psycopg://")):
            raise ValueError("production DATABASE_URL must use PostgreSQL")
        if self.text_provider == "fake":
            raise ValueError("production TEXT_PROVIDER must not be fake")
        if self.text_provider == "qwen":
            if self.dashscope_api_key is None or not self.dashscope_api_key.get_secret_value():
                raise ValueError("DASHSCOPE_API_KEY is required for the Qwen text provider")
            if not self.qwen_base_url.startswith("https://"):
                raise ValueError("production QWEN_BASE_URL must use HTTPS")
        if self.text_provider == "openai":
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
                raise ValueError("OPENAI_API_KEY is required for the OpenAI text provider")
            if not self.openai_base_url.startswith("https://"):
                raise ValueError("production OPENAI_BASE_URL must use HTTPS")
        if self.asr_provider == "fake" or self.vision_provider == "fake":
            raise ValueError("production media providers must not be fake")
        if self.storage_provider == "memory":
            raise ValueError("production STORAGE_PROVIDER must not be memory")
        if self.storage_provider == "local_filesystem":
            if len(self.local_storage_signing_secret.get_secret_value()) < 32:
                raise ValueError("production LOCAL_STORAGE_SIGNING_SECRET is too short")
            if not self.local_storage_public_base_url.startswith("https://"):
                raise ValueError("production LOCAL_STORAGE_PUBLIC_BASE_URL must use HTTPS")
        if self.s3_endpoint_url and not self.s3_use_ssl:
            raise ValueError("production custom S3 endpoints must enable TLS")
        if self.s3_public_endpoint_url and not self.s3_public_endpoint_url.startswith("https://"):
            raise ValueError("production public S3 endpoints must use HTTPS")
        return self


@lru_cache(maxsize=1)  # 如果之前有创建过settings对象就缓存并使用它，没有就建立
def get_settings() -> Settings:
    """读取并缓存当前进程的应用配置。"""

    return Settings()
