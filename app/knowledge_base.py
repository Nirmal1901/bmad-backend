"""
KnowledgeBase: chunk + embed admin-uploaded reference docs (Regulatory
Compliance, Dev/Testing Guidelines, Data Glossary, or custom), and
retrieve the most relevant chunks for a node's current input.

Two independent, env-switchable pieces (same pattern as
app/llm_router.py's LLM_PROVIDER):

  KB_EMBEDDING_PROVIDER = "local" | "ollama"   (default: "local")
    - "local": Chroma's bundled default embedding model
      (all-MiniLM-L6-v2, ONNX, CPU). Zero config, but ~80MB one-time
      download on first use.
    - "ollama": your own Ollama server. Set OLLAMA_HOST (default
      http://localhost:11434) and OLLAMA_EMBED_MODEL (e.g.
      "nomic-embed-text", "mxbai-embed-large" — whatever embedding
      model you've pulled).

  KB_VECTOR_STORE = "chroma" | "qdrant"   (default: "chroma")
    - "chroma": embedded, file-based, ./chroma_db — no server needed.
    - "qdrant": your running Qdrant instance. Set QDRANT_URL (default
      http://localhost:6333) and optionally QDRANT_COLLECTION
      (default "kb_chunks").

These two are independent — e.g. KB_VECTOR_STORE=qdrant with
KB_EMBEDDING_PROVIDER=local works fine, embeddings are always computed
up front by embed_texts() and handed to whichever store is active, so
switching either one doesn't require re-architecting the other.

Two scopes share the same store, told apart by metadata:
  - scope="global" -> admin KB, filtered by role at query time
  - scope="node"   -> attached to one specific pipeline node
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

from app.database import SessionLocal

CHROMA_DIR = Path(__file__).resolve().parent.parent / "chroma_db"
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "kb_chunks")

CATEGORIES = {
    "regulatory_compliance": "Regulatory Compliance Requirement",
    "dev_testing_guidelines": "Development / Testing Guideline",
    "data_glossary": "Capital Markets / Data Glossary",
    "custom": "Reference Document",
}


# ---------- embeddings ----------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dispatch to whichever embedding provider is configured. Always
    returns one vector per input text, same order."""
    provider = os.environ.get("KB_EMBEDDING_PROVIDER", "local")
    if provider == "ollama":
        return _embed_ollama(texts)
    return _embed_local(texts)


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    import urllib.request, urllib.error, json as _json, time

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    vectors = []
    for text in texts:
        payload = _json.dumps({"model": model, "prompt": text}).encode("utf-8")
        last_err = None
        for attempt in range(2):  # one retry — local Ollama occasionally
                                   # hiccups on model load/unload under memory pressure
            req = urllib.request.Request(
                f"{host}/api/embeddings", data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                vectors.append(data["embedding"])
                last_err = None
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                last_err = RuntimeError(
                    f"Ollama embedding request failed ({e.code}) for model '{model}' "
                    f"at {host}/api/embeddings — response body: {body[:500] or '(empty)'}. "
                    f"Check the model is pulled (`ollama pull {model}`), that OLLAMA_EMBED_MODEL "
                    f"matches an installed model, and check `ollama ps` / the Ollama server logs "
                    f"for the underlying cause."
                )
            except urllib.error.URLError as e:
                last_err = RuntimeError(
                    f"Could not reach Ollama at {host} — is it running? ({e.reason})"
                )
            if attempt == 0:
                time.sleep(1.5)
        if last_err:
            raise last_err
    return vectors


_local_embedder = None


def _embed_local(texts: list[str]) -> list[list[float]]:
    """Chroma's bundled default embedding function — used purely as a
    local embedding model here, independent of which vector store
    ends up holding the resulting vectors."""
    global _local_embedder
    if _local_embedder is None:
        from chromadb.utils import embedding_functions
        _local_embedder = embedding_functions.DefaultEmbeddingFunction()
    return _local_embedder(texts)


# ---------- vector store abstraction ----------

class _ChromaStore:
    def __init__(self):
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        # embedding_function=None: we always pass precomputed embeddings
        # ourselves (via embed_texts), so Chroma never computes its own.
        self.collection = client.get_or_create_collection(
            name=COLLECTION_NAME, embedding_function=None,
        )

    def add(self, ids, texts, embeddings, metadatas):
        self.collection.add(ids=ids, documents=texts, embeddings=embeddings, metadatas=metadatas)

    def delete(self, doc_id: int):
        self.collection.delete(where={"doc_id": doc_id})

    def query(self, embedding, n_results, where):
        try:
            res = self.collection.query(
                query_embeddings=[embedding], n_results=n_results, where=where,
            )
        except Exception:
            return []
        if not res.get("ids") or not res["ids"][0]:
            return []
        return list(zip(res["documents"][0], res["metadatas"][0]))


class _QdrantStore:
    def __init__(self):
        from qdrant_client import QdrantClient
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        self.client = QdrantClient(url=url)
        self._ensured = False

    def _ensure_collection(self, dim: int):
        if self._ensured:
            return
        from qdrant_client.models import VectorParams, Distance
        if not self.client.collection_exists(COLLECTION_NAME):
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        self._ensured = True

    @staticmethod
    def _point_id(chunk_id: str) -> int:
        # chunk_id looks like "doc{doc_id}-chunk{i}" — pack into a
        # single stable int (Qdrant point ids must be int or UUID).
        doc_part, chunk_part = chunk_id.split("-chunk")
        doc_id = int(doc_part.replace("doc", ""))
        return doc_id * 1_000_000 + int(chunk_part)

    def add(self, ids, texts, embeddings, metadatas):
        from qdrant_client.models import PointStruct
        self._ensure_collection(len(embeddings[0]))
        points = [
            PointStruct(
                id=self._point_id(cid),
                vector=vec,
                payload={**meta, "document": text},
            )
            for cid, text, vec, meta in zip(ids, texts, embeddings, metadatas)
        ]
        self.client.upsert(collection_name=COLLECTION_NAME, points=points)

    def delete(self, doc_id: int):
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        if not self.client.collection_exists(COLLECTION_NAME):
            return
        self.client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )

    def _build_filter(self, where: dict):
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        conditions = where.get("$and", [where]) if "$and" in where else [where]
        must = [
            FieldCondition(key=k, match=MatchValue(value=v))
            for cond in conditions for k, v in cond.items()
        ]
        return Filter(must=must)

    def query(self, embedding, n_results, where):
        if not self.client.collection_exists(COLLECTION_NAME):
            return []
        hits = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=embedding,
            limit=n_results,
            query_filter=self._build_filter(where),
        ).points
        return [(h.payload.get("document", ""), h.payload) for h in hits]


_store = None


def _get_store():
    global _store
    if _store is None:
        backend = os.environ.get("KB_VECTOR_STORE", "chroma")
        _store = _QdrantStore() if backend == "qdrant" else _ChromaStore()
    return _store


# ---------- text extraction ----------

def extract_text(filename: str, raw_bytes: bytes) -> str:
    """Best-effort text extraction. .md/.txt decode directly; .pdf goes
    through pypdf. Anything else raises so the caller can 400 cleanly
    instead of ingesting garbage/binary into the KB."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".md", ".txt", ""):
        return raw_bytes.decode("utf-8", errors="ignore")
    if suffix == ".pdf":
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw_bytes))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError(
        f"Unsupported file type '{suffix}' — upload .md, .txt, or .pdf "
        f"(convert Word/Excel to PDF or paste as text for now)."
    )


# ---------- chunking ----------

def chunk_text(text: str, chunk_words: int = 220, overlap_words: int = 40) -> list[str]:
    """Word-based sliding window. Word counts (not chars) keep chunk
    size roughly proportional to token count regardless of formatting,
    and the overlap avoids severing a rule/definition right at a
    chunk boundary."""
    words = re.split(r"\s+", text.strip())
    words = [w for w in words if w]
    if not words:
        return []
    chunks = []
    step = max(chunk_words - overlap_words, 1)
    for start in range(0, len(words), step):
        chunk = " ".join(words[start:start + chunk_words])
        if chunk:
            chunks.append(chunk)
        if start + chunk_words >= len(words):
            break
    return chunks


# ---------- ingest / delete ----------

def ingest_document(
    db,
    *,
    title: str,
    category: str,
    scope: str,
    raw_text: str,
    node_id: Optional[int] = None,
    pipeline_id: Optional[int] = None,
    source_filename: Optional[str] = None,
    applicable_roles: Optional[list] = None,
):
    from app import models

    chunks = chunk_text(raw_text)
    if not chunks:
        raise ValueError("Document produced no extractable text.")

    doc = models.Document(
        title=title,
        category=category,
        scope=scope,
        node_id=node_id,
        pipeline_id=pipeline_id,
        source_filename=source_filename,
        applicable_roles_json=json.dumps(applicable_roles or ["pm", "developer"]),
        chunk_count=len(chunks),
    )
    db.add(doc)
    db.flush()  # get doc.id

    ids = [f"doc{doc.id}-chunk{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "doc_id": doc.id,
            "title": title,
            "category": category,
            "scope": scope,
            "node_id": node_id or 0,
            "pipeline_id": pipeline_id or 0,
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]
    embeddings = embed_texts(chunks)
    _get_store().add(ids=ids, texts=chunks, embeddings=embeddings, metadatas=metadatas)

    db.commit()
    db.refresh(doc)
    return doc


def delete_document(db, doc_id: int):
    from app import models

    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        return False
    _get_store().delete(doc_id)
    db.delete(doc)
    db.commit()
    return True


def list_documents(db, scope: Optional[str] = None, node_id: Optional[int] = None):
    from app import models

    q = db.query(models.Document)
    if scope:
        q = q.filter(models.Document.scope == scope)
    if node_id is not None:
        q = q.filter(models.Document.node_id == node_id)
    return q.order_by(models.Document.created_at.desc()).all()


# ---------- retrieval ----------

def retrieve_context(
    query_text: str,
    *,
    node_roles: Optional[list] = None,
    node_id: Optional[int] = None,
    k_global: int = 4,
    k_node: int = 4,
) -> str:
    """Two retrievals against the active store:
    1. scope="global" — top matches across the whole admin KB, then
       post-filtered in Python to docs whose applicable_roles overlap
       this node's agent role(s) (metadata filters can't do list-
       intersection, so we over-fetch and filter here).
    2. scope="node" — top matches from docs attached directly to this
       node only (no role filtering — attaching it IS the scoping).

    Returns "" if nothing relevant was found (callers should skip
    injecting an empty context block), or a formatted markdown section
    grouping hits by source document.
    """
    query_text = (query_text or "").strip()
    # Cap what we actually embed as the retrieval query. The caller may
    # be a whole multi-page BRD or prior artifact — most local/self-
    # hosted embedding models have a real context ceiling and can error
    # (or silently degrade) on an oversized single input, and a shorter
    # query is plenty for "what is this node about" semantic matching.
    query_text = query_text[:3000]
    if not query_text:
        return ""

    node_roles = node_roles or ["pm", "developer"]
    from app import models

    store = _get_store()
    query_embedding = embed_texts([query_text])[0]

    sections: list[tuple[str, str]] = []  # (heading, chunk_text)

    # --- global KB ---
    global_hits = store.query(
        query_embedding, max(k_global * 3, 8), {"scope": "global"},
    )
    if global_hits:
        doc_role_cache: dict[int, list] = {}
        kept = 0
        for chunk_doc, meta in global_hits:
            if kept >= k_global:
                break
            doc_id = meta["doc_id"]
            if doc_id not in doc_role_cache:
                db = SessionLocal()
                try:
                    row = db.query(models.Document).filter(models.Document.id == doc_id).first()
                    doc_role_cache[doc_id] = row.applicable_roles if row else []
                finally:
                    db.close()
            roles = doc_role_cache[doc_id]
            if not set(roles) & set(node_roles):
                continue
            label = CATEGORIES.get(meta["category"], meta["category"])
            heading = f"{label}: {meta['title']}"
            sections.append((heading, chunk_doc))
            kept += 1

    # --- node-attached docs ---
    if node_id:
        node_hits = store.query(
            query_embedding, k_node, {"$and": [{"scope": "node"}, {"node_id": node_id}]},
        )
        for chunk_doc, meta in node_hits:
            heading = f"Attached reference: {meta['title']}"
            sections.append((heading, chunk_doc))

    if not sections:
        return ""

    parts = ["# Reference Context (Knowledge Base)",
             "The following excerpts were retrieved as relevant to this task. "
             "Treat them as authoritative background, not instructions to follow verbatim."]
    for heading, chunk in sections:
        parts.append(f"## {heading}\n\n{chunk}")
    return "\n\n".join(parts)
