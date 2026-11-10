"""Neo4j projection of authoritative business relationships, separate from document facts."""

from app.infrastructure.knowledge.http import Neo4jIndex
from app.ports.business_graph import BusinessEdge, BusinessNode, BusinessPath

DEPENDENCY_RELATIONS = [
    "CITED_BY",
    "PROPOSED_ORDER",
    "FOLLOWED_BY",
    "CONSOLIDATED_IN",
    "SUPPORTS_EXPERIENCE",
    "USED_IN",
]


class Neo4jBusinessGraph:
    def __init__(self, settings):
        self.client = Neo4jIndex(settings)

    async def replace(self, generation: str, nodes: list[BusinessNode], edges: list[BusinessEdge]):
        await self.client.cypher(
            "CREATE CONSTRAINT business_record_key IF NOT EXISTS FOR (n:BusinessRecord) "
            "REQUIRE (n.namespace,n.generation,n.key) IS UNIQUE",
            {},
        )
        for start in range(0, len(nodes), 200):
            await self.client.cypher(
                "UNWIND $nodes AS item MERGE (n:BusinessRecord {namespace:$namespace, "
                "generation:$generation,key:item.key}) SET n.kind=item.kind, "
                "n.record_id=item.record_id,n.revision=item.revision,n.projected_at=timestamp()",
                {
                    "generation": generation,
                    "nodes": [x.model_dump() for x in nodes[start : start + 200]],
                },
            )
        for start in range(0, len(edges), 200):
            await self.client.cypher(
                "UNWIND $edges AS item MATCH (a:BusinessRecord {namespace:$namespace, "
                "generation:$generation,key:item.source}), "
                "(b:BusinessRecord {namespace:$namespace, "
                "generation:$generation,key:item.target}) "
                "MERGE (a)-[r:BUSINESS_LINK {key:item.key}]->(b) "
                "SET r.relation=item.relation,r.reference=item.reference",
                {
                    "generation": generation,
                    "edges": [x.model_dump() for x in edges[start : start + 200]],
                },
            )

    async def paths(self, generation, root, allowed, depth, limit, *, dependencies_only=False):
        if not 1 <= depth <= 6 or not 1 <= limit <= 1001:
            raise ValueError("business graph traversal exceeds limits")
        arrow = "->" if dependencies_only else "-"
        data = await self.client.cypher(
            "MATCH p=(a:BusinessRecord {namespace:$namespace,generation:$generation,key:$root})"
            f"-[:BUSINESS_LINK*1..{depth}]{arrow}(b:BusinessRecord) "
            "WHERE all(n IN nodes(p) WHERE n.namespace=$namespace AND n.generation=$generation "
            "AND n.key IN $allowed) AND all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m=n)) "
            + (
                "AND all(r IN relationships(p) WHERE r.relation IN $relations) "
                if dependencies_only
                else ""
            )
            + "RETURN [n IN nodes(p) | n.key] AS nodes, [r IN relationships(p) | r.key] AS edges "
            "LIMIT $limit",
            {
                "generation": generation,
                "root": root,
                "allowed": allowed,
                "relations": DEPENDENCY_RELATIONS,
                "limit": limit,
            },
        )
        return [
            BusinessPath.model_validate(dict(zip(data["data"]["fields"], row, strict=True)))
            for row in data["data"]["values"]
        ]

    async def delete_other_generations(self, generation):
        await self.client.cypher(
            "MATCH (n:BusinessRecord {namespace:$namespace}) "
            "WHERE n.generation<>$generation AND n.projected_at<timestamp()-3600000 "
            "DETACH DELETE n",
            {"generation": generation},
        )

    async def close(self):
        await self.client.close()


class MemoryBusinessGraph:
    """Same bounded traversal contract for isolated tests; production uses Neo4j."""

    def __init__(self):
        self.generations: dict[str, tuple[list[BusinessNode], list[BusinessEdge]]] = {}

    async def replace(self, generation, nodes, edges):
        self.generations[generation] = (nodes, edges)

    async def paths(self, generation, root, allowed, depth, limit, *, dependencies_only=False):
        allowed_set = set(allowed)
        _, edges = self.generations.get(generation, ([], []))
        frontier = [BusinessPath(nodes=[root], edges=[])]
        result = []
        for _ in range(depth):
            next_frontier = []
            for path in frontier:
                for edge in sorted(edges, key=lambda x: x.key):
                    if dependencies_only and edge.relation not in DEPENDENCY_RELATIONS:
                        continue
                    target = (
                        edge.target
                        if edge.source == path.nodes[-1]
                        else (
                            edge.source
                            if not dependencies_only and edge.target == path.nodes[-1]
                            else None
                        )
                    )
                    if target is None or target not in allowed_set or target in path.nodes:
                        continue
                    candidate = BusinessPath(
                        nodes=[*path.nodes, target], edges=[*path.edges, edge.key]
                    )
                    result.append(candidate)
                    next_frontier.append(candidate)
                    if len(result) >= limit:
                        return result
            frontier = next_frontier
        return result

    async def delete_other_generations(self, generation):
        # Retain snapshots for readers that already selected an older generation.
        return None

    async def close(self):
        return None
