import sys
sys.path.append('/app')
from app.rag.chroma import create_chroma
from app.config import load_settings
settings=load_settings()
rag=create_chroma(settings)
print(f"Count: {rag._collection().count()}")
