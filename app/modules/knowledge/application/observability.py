"""Attach metadata-only capability logs to the owning knowledge execution."""

from copy import copy
from uuid import uuid4

from app.shared.platform.capabilities import observe_capability


class ObservedCapability:
    def __init__(self, target, session, job, capability):
        self.target, self.session, self.job, self.capability = target, session, job, capability

    def __getattr__(self, name):
        return getattr(self.target, name)

    async def _call(self, method, *args, **kwargs):
        config = self.job.config_snapshot.get("capabilities", {}).get(self.capability, {})
        async with observe_capability(
            self.session,
            job=self.job,
            run_id=self.job.resource_id or uuid4(),
            capability=self.capability,
            provider=config.get("provider", getattr(self.target, "name", "http")),
            model=config.get("model_name", getattr(self.target, "model", "docling")),
            alias=config.get("alias", f"knowledge-{self.capability}"),
        ):
            # Unknown token/page counts remain null; do not fabricate measurements.
            return await getattr(self.target, method)(*args, **kwargs)

    async def embed(self, texts):
        return await self._call("embed", texts)

    async def rerank(self, query, texts):
        return await self._call("rerank", query, texts)

    async def submit(self, *args, **kwargs):
        return await self._call("submit", *args, **kwargs)


def observed_knowledge(resources, session, job):
    scoped = copy(resources)
    for attribute, capability in (
        ("parser", "parser"),
        ("embedding", "embedding"),
        ("reranker", "rerank"),
    ):
        setattr(
            scoped,
            attribute,
            ObservedCapability(getattr(resources, attribute), session, job, capability),
        )
    return scoped
