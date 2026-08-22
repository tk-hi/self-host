"""Ingest /workspace/corpus/docs into Qdrant via LlamaIndex + BGE-M3 (CPU).

Run: HF_HOME=/workspace/hf-cache /workspace/venvs/pipeline/bin/python ingest.py
"""

from pathlib import Path

from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
import qdrant_client

DOCS = Path("/workspace/corpus/docs")
COLLECTION = "meridian"

Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-m3", device="cpu")
Settings.llm = None
Settings.node_parser = SentenceSplitter(chunk_size=512, chunk_overlap=64)

client = qdrant_client.QdrantClient(host="127.0.0.1", port=6333)
if client.collection_exists(COLLECTION):
    client.delete_collection(COLLECTION)

docs = []
for f in sorted(DOCS.glob("*.txt")):
    docs.append(Document(text=f.read_text(), metadata={"doc_id": f.stem},
                         id_=f.stem))

store = QdrantVectorStore(client=client, collection_name=COLLECTION)
ctx = StorageContext.from_defaults(vector_store=store)
index = VectorStoreIndex.from_documents(docs, storage_context=ctx,
                                        show_progress=True)
print(f"ingested {len(docs)} docs into '{COLLECTION}'")
print("points:", client.count(COLLECTION).count)
