"""Resolve admin aliases once; persist portable, credential-free execution settings."""

from app.shared.errors import ConflictError
from app.shared.platform.capabilities import capability_snapshot


async def knowledge_snapshot(session, settings):
    backend = "fake" if settings.knowledge_backend == "fake" else "custom_http"
    text_model = {
        "fake": "fake-text-v1",
        "qwen": settings.qwen_text_model,
        "openai": settings.openai_text_model,
    }.get(settings.text_provider, settings.custom_text_model)
    defaults = {
        "embedding": dict(
            provider=backend,
            model_name=settings.embedding_model,
            model_snapshot=settings.embedding_revision,
            config={
                "dimensions": settings.embedding_dimensions,
                "batch_size": settings.knowledge_batch_size,
            },
        ),
        "rerank": dict(
            provider=backend,
            model_name=settings.reranker_model,
            model_snapshot=settings.reranker_revision,
            config={"max_candidates": 100},
        ),
        "parser": dict(
            provider=backend,
            model_name="docling",
            config={
                "max_pages": settings.knowledge_max_document_pages,
                "max_bytes": settings.knowledge_max_document_bytes,
            },
        ),
        "text": dict(provider=settings.text_provider, model_name=text_model, config={}),
    }
    for capability in ("embedding", "rerank", "parser"):
        defaults[capability]["config"]["timeout_seconds"] = settings.knowledge_timeout_seconds
    resolved = await capability_snapshot(
        session,
        {key: f"knowledge-{key}" for key in defaults},
        strategy_versions={
            "chunker": "structure-intent-v1",
            "graph_extractor": "evidence-facts-v1",
        },
        generation="",
        defaults=defaults,
    )
    capabilities = resolved["capabilities"]
    if capabilities["parser"]["model_name"] != "docling":
        raise ConflictError("this parser runtime supports only docling", alias="knowledge-parser")
    embedding, rerank, text = (capabilities[k] for k in ("embedding", "rerank", "text"))
    return {
        "capabilities": capabilities,
        "embedding_model": embedding["model_name"],
        "embedding_revision": embedding["model_snapshot"] or "",
        "dimensions": embedding["config"].get("dimensions", settings.embedding_dimensions),
        "reranker_model": rerank["model_name"],
        "reranker_revision": rerank["model_snapshot"] or "",
        "backend": settings.knowledge_backend,
        "chunker": "structure-intent-v1",
        "graph_extractor": "evidence-facts-v1",
        "text_model": text["model_name"],
        "text_provider": text["provider"],
        "provider": text["provider"],
        "model": text["model_name"],
        "model_config": text["config"],
        "model_alias": text["alias"],
    }
