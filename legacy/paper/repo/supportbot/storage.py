import os
import json
import numpy as np
import pickle
from typing import List, Dict, Any

class LocalBlobStorage:
    def __init__(self, base_path: str):
        self.base_path = base_path
        os.makedirs(base_path, exist_ok=True)

    def save(self, filename: str, content: str):
        path = os.path.join(self.base_path, filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    def load(self, filename: str) -> str:
        path = os.path.join(self.base_path, filename)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return None

class SimpleVectorStore:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.documents = []
        self.embeddings = None
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
        self.load()

    def add(self, documents: List[Dict[str, Any]], embeddings: List[List[float]]):
        if not documents:
            return
            
        new_embeddings = np.array(embeddings)
        
        if self.embeddings is None:
            self.embeddings = new_embeddings
        else:
            self.embeddings = np.vstack([self.embeddings, new_embeddings])
            
        self.documents.extend(documents)
        self.save()

    def search(self, query_embedding: List[float], k: int = 5) -> List[Dict[str, Any]]:
        if self.embeddings is None or len(self.documents) == 0:
            return []

        query_emb = np.array(query_embedding)
        
        # Cosine similarity
        # Normalize embeddings
        norm_docs = np.linalg.norm(self.embeddings, axis=1)
        norm_query = np.linalg.norm(query_emb)
        
        if norm_query == 0:
            return []
            
        # Avoid division by zero
        norm_docs[norm_docs == 0] = 1e-10
        
        scores = np.dot(self.embeddings, query_emb) / (norm_docs * norm_query)
        
        # Top K
        top_indices = np.argsort(scores)[-k:][::-1]
        
        results = []
        for idx in top_indices:
            doc = self.documents[idx].copy()
            doc['score'] = float(scores[idx])
            results.append(doc)
            
        return results

    def save(self):
        with open(self.storage_path + '.pkl', 'wb') as f:
            pickle.dump(self.documents, f)
        if self.embeddings is not None:
            np.save(self.storage_path + '.npy', self.embeddings)

    def load(self):
        pkl_path = self.storage_path + '.pkl'
        npy_path = self.storage_path + '.npy'
        
        if os.path.exists(pkl_path):
            try:
                with open(pkl_path, 'rb') as f:
                    self.documents = pickle.load(f)
            except (EOFError, pickle.UnpicklingError):
                print(f"Warning: Could not load {pkl_path}, starting empty.")
                self.documents = []
                
        if os.path.exists(npy_path):
            self.embeddings = np.load(npy_path)
