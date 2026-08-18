"""Read-only chat tool for the current knowledge-base partition."""

from __future__ import annotations

import json
from collections.abc import Mapping

from backend.tools.base import Tool, ToolInvocationContext

from .models import EmbeddingProfile
from .service import KnowledgeBaseService


def knowledge_base_search_tool(
    service: KnowledgeBaseService,
    *,
    user_id: str,
    profile: EmbeddingProfile,
    config: Mapping[str, object],
    project_id: str | None = None,
) -> Tool:
    """Build a tool whose only model-controlled argument is ``query``."""

    def search(context: ToolInvocationContext, *, query: str) -> str:
        if project_id:
            section = service.ensure_section(user_id, project_id=project_id)
        elif context.session_id:
            section = service.ensure_section(user_id, session_id=context.session_id)
        else:
            return json.dumps({"results": [], "warning": "当前会话没有可用知识库分区。"}, ensure_ascii=False)
        response = service.search(
            query,
            user_id=user_id,
            section_id=section.section_id,
            profile=profile,
            algorithm=str(config.get("algorithm", "hybrid")),  # type: ignore[arg-type]
            bm25_candidate_k=int(config.get("bm25_candidate_k", 20)),
            vector_candidate_k=int(config.get("vector_candidate_k", 20)),
            top_k=int(config.get("top_k", 8)),
        )
        return json.dumps(
            {
                "algorithm": response.algorithm,
                "warning": response.warning,
                "results": [
                    {
                        "chunk_id": item.chunk_id,
                        "document_id": item.document_id,
                        "filename": item.filename,
                        "text": item.text,
                        "page_start": item.page_start,
                        "page_end": item.page_end,
                        "score": item.score,
                        "source": item.source,
                        "rank": item.rank,
                    }
                    for item in response.results
                ],
            },
            ensure_ascii=False,
        )

    return Tool(
        name="search_knowledge_base",
        description="Search the current user's knowledge base for relevant PDF passages. The runtime applies the current session and permissions.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 4000}},
            "required": ["query"],
            "additionalProperties": False,
        },
        read_only=True,
        context_handler=search,
    )


__all__ = ["knowledge_base_search_tool"]
