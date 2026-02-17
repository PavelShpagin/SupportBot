from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlparse

import chromadb

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChromaRag:
    collection_name: str
    client: Any

    def _collection(self):
        return self.client.get_or_create_collection(name=self.collection_name)

    def upsert_case(self, *, case_id: str, document: str, embedding: list[float], metadata: Dict[str, Any]) -> None:
        col = self._collection()
        col.upsert(ids=[case_id], documents=[document], embeddings=[embedding], metadatas=[metadata])

    def retrieve_cases(self, *, group_id: str, embedding: list[float], k: int, status: str = "solved") -> List[Dict[str, Any]]:
        col = self._collection()
        # Filter by group_id AND status (only return solved cases by default)
        where_filter = {"$and": [{"group_id": group_id}, {"status": status}]}
        out = col.query(
            query_embeddings=[embedding],
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )
        return self._format_results(out)

    def search_all_cases(self, *, embedding: list[float], k: int) -> List[Dict[str, Any]]:
        col = self._collection()
        out = col.query(
            query_embeddings=[embedding],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        return self._format_results(out)

    def _format_results(self, out: Dict[str, Any]) -> List[Dict[str, Any]]:
        # out fields are lists per query (we always do 1 query)
        ids = (out.get("ids") or [[]])[0]
        docs = (out.get("documents") or [[]])[0]
        metas = (out.get("metadatas") or [[]])[0]
        dists = (out.get("distances") or [[]])[0]
        results: List[Dict[str, Any]] = []
        for i, cid in enumerate(ids):
            results.append(
                {
                    "case_id": cid,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                }
            )
        return results

    def delete_cases(self, case_ids: List[str]) -> int:
        """Delete cases from RAG by their IDs. Returns number deleted."""
        if not case_ids:
            return 0
        col = self._collection()
        col.delete(ids=case_ids)
        return len(case_ids)


def create_chroma(settings: Settings) -> ChromaRag:
    u = urlparse(settings.chroma_url)
    host = u.hostname or "rag"
    port = u.port or (443 if u.scheme == "https" else 80)

    client = chromadb.HttpClient(host=host, port=port)
    log.info("Chroma client configured: %s://%s:%s collection=%s", u.scheme or "http", host, port, settings.chroma_collection)
    return ChromaRag(collection_name=settings.chroma_collection, client=client)

