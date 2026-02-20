import sys
sys.path.insert(0, "/app")
from app.config import load_settings
from app.rag.chroma import create_chroma
from app.llm.client import LLMClient
s = load_settings()
rag = create_chroma(s)
llm = LLMClient(s)
q = "в records завантажити давало zip зі стрімом а зараз stabx"
emb = llm.embed(text=q)
results = rag.retrieve_cases(group_id="1fWBz1RwCF0B/wGHfNMER4NkWBJPYvjGCv2kXsBJTok=", embedding=emb, k=5)
for r in results:
    print(r.get("case_id","")[:16], " dist=", round(r.get("distance",9),3), " doc=", r.get("document","")[:60].replace("\n"," "))
