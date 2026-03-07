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

    def retrieve_cases(
        self,
        *,
        group_id: str,
        group_ids: list[str] | None = None,
        embedding: list[float],
        k: int,
        status: str | None = "solved",
    ) -> List[Dict[str, Any]]:
        """Semantic search in the collection.

        Args:
            group_id: Primary group to search.
            group_ids: If provided, search across multiple groups (union).
            status: Filter by case status. Pass None to return all statuses.
                    Defaults to "solved" so that only SCRAG-indexed cases are returned.
        """
        col = self._collection()
        ids_to_search = group_ids if group_ids else [group_id]
        if len(ids_to_search) == 1:
            group_filter: Dict[str, Any] = {"group_id": ids_to_search[0]}
        else:
            group_filter = {"group_id": {"$in": ids_to_search}}
        if status is not None:
            where_filter: Dict[str, Any] = {"$and": [group_filter, {"status": status}]}
        else:
            where_filter = group_filter
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

    def delete_cases_by_group(self, group_id: str) -> int:
        """Delete ALL cases for a group using metadata filter.

        More thorough than delete_cases(ids) because it catches any stale
        ChromaDB entries whose case_ids are no longer tracked in MySQL
        (e.g. due to a race between the live-message worker and re-ingest).
        Returns number of documents deleted.
        """
        try:
            col = self._collection()
            result = col.get(where={"group_id": group_id}, include=[])
            ids = result.get("ids") or []
            if ids:
                col.delete(ids=ids)
                log.info("delete_cases_by_group: removed %d docs for group %s", len(ids), group_id[:20])
            return len(ids)
        except Exception as e:
            log.warning("delete_cases_by_group failed for group %s: %s", group_id[:20], e)
            return 0

    def list_all_case_ids(self) -> List[str]:
        """Return every case_id currently stored in ChromaDB (all groups).

        Used by the SYNC_RAG worker to identify stale entries.
        """
        try:
            col = self._collection()
            result = col.get(include=[])
            return result.get("ids") or []
        except Exception as e:
            log.warning("list_all_case_ids failed: %s", e)
            return []

    def wipe_all_cases(self) -> int:
        """Delete the entire RAG collection (all cases). Returns count deleted."""
        try:
            col = self._collection()
            all_ids = col.get(include=[])["ids"]
            if all_ids:
                col.delete(ids=all_ids)
            log.info("RAG wipe: deleted %d documents", len(all_ids))
            return len(all_ids)
        except Exception as e:
            log.warning("RAG wipe error: %s", e)
            return 0


@dataclass(frozen=True)
class DualRag:
    """Two-collection RAG: SCRAG (solved) + RCRAG (recommendation).

    Each is an independent ChromaDB collection with separate indices.
    """
    scrag: ChromaRag
    rcrag: ChromaRag

    def upsert_case(self, *, case_id: str, document: str, embedding: list[float], metadata: Dict[str, Any], status: str = "solved") -> None:
        """Upsert into the correct collection based on status."""
        target = self.scrag if status == "solved" else self.rcrag
        target.upsert_case(case_id=case_id, document=document, embedding=embedding, metadata=metadata)
        # If promoting recommendation→solved, remove from RCRAG
        if status == "solved":
            try:
                self.rcrag.delete_cases([case_id])
            except Exception:
                pass  # May not exist in RCRAG

    def delete_cases(self, case_ids: List[str]) -> int:
        """Delete from both collections."""
        n = self.scrag.delete_cases(case_ids)
        n += self.rcrag.delete_cases(case_ids)
        return n

    def delete_cases_by_group(self, group_id: str) -> int:
        return self.scrag.delete_cases_by_group(group_id) + self.rcrag.delete_cases_by_group(group_id)

    def list_all_case_ids(self) -> List[str]:
        return self.scrag.list_all_case_ids() + self.rcrag.list_all_case_ids()

    def wipe_all_cases(self) -> int:
        return self.scrag.wipe_all_cases() + self.rcrag.wipe_all_cases()


def create_chroma(settings: Settings) -> DualRag:
    u = urlparse(settings.chroma_url)
    host = u.hostname or "rag"
    port = u.port or (443 if u.scheme == "https" else 80)

    client = chromadb.HttpClient(host=host, port=port)
    base = settings.chroma_collection
    scrag = ChromaRag(collection_name=f"{base}_scrag", client=client)
    rcrag = ChromaRag(collection_name=f"{base}_rcrag", client=client)
    log.info("Chroma dual-RAG configured: %s://%s:%s scrag=%s rcrag=%s",
             u.scheme or "http", host, port, f"{base}_scrag", f"{base}_rcrag")
    return DualRag(scrag=scrag, rcrag=rcrag)

