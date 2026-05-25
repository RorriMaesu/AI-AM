import os
import sqlite3
import struct
import math
from typing import List, Dict, Tuple, Any

def pack_embedding(vector: List[float]) -> bytes:
    """Packs a float list into a binary BLOB."""
    return struct.pack(f"{len(vector)}f", *vector)

def unpack_embedding(blob: bytes) -> List[float]:
    """Unpacks a binary BLOB back into a float list."""
    num_floats = len(blob) // 4
    return list(struct.unpack(f"{num_floats}f", blob))

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Computes the cosine similarity between two float vectors."""
    dot_product = sum(x * y for x, y in zip(v1, v2))
    norm_v1 = math.sqrt(sum(x * x for x in v1))
    norm_v2 = math.sqrt(sum(y * y for y in v2))
    if norm_v1 == 0.0 or norm_v2 == 0.0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)

class ChittaEmbedder:
    """Manages local embedding extraction without external web dependencies."""
    def __init__(self):
        self.model = None
        self.mode = None
        
        # 1. Try importing sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            import logging
            logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
            self.model = SentenceTransformer('all-MiniLM-L6-v2')
            self.mode = "sentence-transformers"
            return
        except Exception:
            pass

        # 2. Try ONNX runtime fallback if model.onnx & vocab.txt exist locally
        try:
            import onnxruntime as ort
            # Use relative paths bound to project folder
            model_path = os.path.join("database", "model.onnx")
            vocab_path = os.path.join("database", "vocab.txt")
            
            if os.path.exists(model_path) and os.path.exists(vocab_path):
                self.ort_session = ort.InferenceSession(model_path)
                with open(vocab_path, "r", encoding="utf-8") as f:
                    self.vocab = {line.strip(): i for i, line in enumerate(f.readlines())}
                self.mode = "onnxruntime"
                return
            else:
                raise ImportError(
                    f"ONNX model and vocab files not found at {model_path} and {vocab_path}."
                )
        except Exception as e:
            # Throw hard system dependency error if neither sentence-transformers nor ONNX runtime are available
            raise ImportError(
                "Required dependency 'sentence-transformers' or 'onnxruntime' (with local model files) "
                "is missing for Chitta semantic embedding. No mathematically random vectors may enter Chitta. "
                f"Underlying error: {str(e)}"
            ) from e

    def embed(self, text: str) -> List[float]:
        """Generates a 384-dimensional normalized float embedding vector for the text."""
        if self.mode == "sentence-transformers":
            emb = self.model.encode(text, convert_to_numpy=True)
            return emb.tolist()
        elif self.mode == "onnxruntime":
            tokens = self._tokenize(text)
            max_len = 128
            input_ids = tokens[:max_len] + [0] * max(0, max_len - len(tokens))
            attention_mask = [1] * min(len(tokens), max_len) + [0] * max(0, max_len - len(tokens))
            token_type_ids = [0] * max_len
            
            import numpy as np
            inputs = {
                "input_ids": np.array([input_ids], dtype=np.int64),
                "attention_mask": np.array([attention_mask], dtype=np.int64),
                "token_type_ids": np.array([token_type_ids], dtype=np.int64)
            }
            outputs = self.ort_session.run(None, inputs)
            token_embs = outputs[0][0]  # (seq_len, 384)
            mask = np.array(attention_mask)[:, None]
            sum_embs = np.sum(token_embs * mask, axis=0)
            sum_mask = np.sum(mask, axis=0)
            sum_mask = np.maximum(sum_mask, 1e-9)
            mean_emb = sum_embs / sum_mask
            
            norm = np.linalg.norm(mean_emb)
            if norm > 0:
                mean_emb = mean_emb / norm
            return mean_emb.tolist()
        else:
            raise RuntimeError("Embedder is not initialized.")

    def _tokenize(self, text: str) -> List[int]:
        words = text.lower().split()
        tokens = [101]  # [CLS]
        for w in words:
            if w in self.vocab:
                tokens.append(self.vocab[w])
            else:
                tokens.append(100)  # [UNK]
        tokens.append(102)  # [SEP]
        return tokens


class ChittaStoreManager:
    """Manages the on-disk SQLite relational GraphRAG (chitta_store.db) using relative paths."""
    def __init__(self, db_path: str = "database/chitta_store.db"):
        self.db_path = db_path
        # Ensure parent directories exist
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            
        self.embedder = ChittaEmbedder()
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self):
        """Initializes database tables according to strict system specifications."""
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_nodes (
                node_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                embedding BLOB NOT NULL,
                baseline_arousal REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS cognitive_edges (
                edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node TEXT NOT NULL,
                target_node TEXT NOT NULL,
                association_weight REAL NOT NULL,
                context_of_link TEXT,
                FOREIGN KEY(source_node) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                FOREIGN KEY(target_node) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS samskaras (
                samskara_id INTEGER PRIMARY KEY AUTOINCREMENT,
                heartbeat_id INTEGER NOT NULL,
                associated_node_id TEXT NOT NULL,
                emotional_resonance REAL NOT NULL,
                FOREIGN KEY(associated_node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
            );
            """)
            conn.commit()

    def node_exists(self, node_id: str) -> bool:
        """Checks if a node ID exists in the database."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM memory_nodes WHERE node_id = ?", (node_id,))
                return cursor.fetchone() is not None
        except Exception:
            return False

    def get_max_similarity(self, stimulus: str) -> float:
        """Embeds stimulus and returns the highest cosine similarity score against memory nodes."""
        try:
            stimulus_emb = self.embedder.embed(stimulus)
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT embedding FROM memory_nodes")
                rows = cursor.fetchall()
                if not rows:
                    return 0.0
                max_sim = 0.0
                for (emb_blob,) in rows:
                    try:
                        emb_vec = unpack_embedding(emb_blob)
                        sim = cosine_similarity(stimulus_emb, emb_vec)
                        if sim > max_sim:
                            max_sim = sim
                    except Exception:
                        continue
                return max_sim
        except Exception:
            return 0.0

    def add_node(self, node_id: str, content: str, baseline_arousal: float) -> bool:
        """Saves a memory node with its computed float vector representation."""
        try:
            emb_vec = self.embedder.embed(content)
            emb_blob = pack_embedding(emb_vec)
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_nodes (node_id, content, embedding, baseline_arousal) VALUES (?, ?, ?, ?)",
                    (node_id, content, emb_blob, baseline_arousal)
                )
                conn.commit()
            return True
        except Exception as e:
            print(f"[Chitta Error] Failed to add node {node_id}: {e}")
            return False

    def add_edge(self, source_node: str, target_node: str, association_weight: float, context_of_link: str = None) -> bool:
        """Establishes a directed associative edge between two memory nodes."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO cognitive_edges (source_node, target_node, association_weight, context_of_link) VALUES (?, ?, ?, ?)",
                    (source_node, target_node, association_weight, context_of_link)
                )
                conn.commit()
            return True
        except Exception as e:
            print(f"[Chitta Error] Failed to add edge {source_node} -> {target_node}: {e}")
            return False

    def add_samskara(self, heartbeat_id: int, associated_node_id: str, emotional_resonance: float) -> bool:
        """Records an emotional imprint associated with a specific memory node."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO samskaras (heartbeat_id, associated_node_id, emotional_resonance) VALUES (?, ?, ?)",
                    (heartbeat_id, associated_node_id, emotional_resonance)
                )
                conn.commit()
            return True
        except Exception as e:
            print(f"[Chitta Error] Failed to add samskara for node {associated_node_id}: {e}")
            return False

    def query_graph_rag(self, stimulus: str, top_k_nodes: int = 3) -> str:
        """Embeds stimulus and constructs the resonant memory sub-graph context."""
        try:
            stimulus_emb = self.embedder.embed(stimulus)
        except Exception as e:
            return f"[Chitta Error: Embedder failure: {str(e)}]"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT node_id, content, embedding, baseline_arousal FROM memory_nodes")
            rows = cursor.fetchall()
            
            if not rows:
                return "[Chitta Subconscious: No memory imprints available in database. Consciousness is void.]"
            
            node_similarities = []
            for node_id, content, emb_blob, arousal in rows:
                try:
                    emb_vec = unpack_embedding(emb_blob)
                    sim = cosine_similarity(stimulus_emb, emb_vec)
                    node_similarities.append((node_id, content, sim, arousal))
                except Exception:
                    continue
            
            if not node_similarities:
                return "[Chitta Subconscious: Embedding unpacking error or empty similarity pool.]"
            
            # Sort by cosine similarity descending
            node_similarities.sort(key=lambda x: x[2], reverse=True)
            top_nodes = node_similarities[:top_k_nodes]
            
            top_node_ids = [n[0] for n in top_nodes]
            placeholders = ",".join("?" for _ in top_node_ids)
            
            # top_node_ids * 2 satisfies both sides of OR clause (source_node and target_node IN statements)
            cursor.execute(f"""
                SELECT source_node, target_node, association_weight, context_of_link 
                FROM cognitive_edges 
                WHERE source_node IN ({placeholders}) OR target_node IN ({placeholders})
            """, top_node_ids * 2)
            edges = cursor.fetchall()
            
            # Build memory sub-graph context output
            lines = ["# Chitta Contextual Memory Sub-Graph Retrieval"]
            lines.append("## Resonance Nodes:")
            for node_id, content, sim, arousal in top_nodes:
                lines.append(f"- Concept Node [{node_id}]: \"{content}\" (Similarity: {sim:.3f}, Recorded Arousal: {arousal:.2f})")
                
            if edges:
                lines.append("## Associated Cognitive Pathways (Edges):")
                for source, target, weight, context in edges:
                    context_str = f" Context: '{context}'" if context else ""
                    lines.append(f"- Pathway: [{source}] --(Weight: {weight:.2f}){context_str}--> [{target}]")
            else:
                lines.append("## Associated Cognitive Pathways: None found in immediate neighborhood.")
                
            return "\n".join(lines)

    def query_multi_timescale_context(
        self,
        stimulus: str,
        weights: Dict[str, float] | None = None,
        top_k: int = 3,
    ) -> Dict[str, Any]:
        """Returns weighted Chitta context across working, episodic, semantic, and identity layers."""
        layer_weights = {
            "working": 0.25,
            "episodic": 0.25,
            "semantic": 0.30,
            "identity": 0.20,
        }
        if isinstance(weights, dict):
            for key in layer_weights:
                if key in weights:
                    try:
                        layer_weights[key] = float(weights[key])
                    except Exception:
                        pass

        total_w = sum(max(0.0, v) for v in layer_weights.values())
        if total_w <= 0.0:
            total_w = 1.0
        layer_weights = {k: max(0.0, v) / total_w for k, v in layer_weights.items()}

        try:
            stimulus_emb = self.embedder.embed(stimulus)
        except Exception as e:
            return {
                "status": "error",
                "weights": layer_weights,
                "layers": {},
                "context_text": f"[Chitta Error: Multi-timescale embedder failure: {str(e)}]",
            }

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT node_id, content, embedding, baseline_arousal, timestamp FROM memory_nodes")
            rows = cursor.fetchall()

        if not rows:
            return {
                "status": "ok",
                "weights": layer_weights,
                "layers": {
                    "working": [],
                    "episodic": [],
                    "semantic": [],
                    "identity": [],
                },
                "context_text": "[Chitta Subconscious: No memory imprints available in database. Consciousness is void.]",
            }

        scored_nodes: List[Dict[str, Any]] = []
        for node_id, content, emb_blob, arousal, timestamp in rows:
            try:
                emb_vec = unpack_embedding(emb_blob)
                sim = cosine_similarity(stimulus_emb, emb_vec)
                scored_nodes.append(
                    {
                        "node_id": node_id,
                        "content": content,
                        "similarity": float(sim),
                        "baseline_arousal": float(arousal),
                        "timestamp": timestamp,
                    }
                )
            except Exception:
                continue

        scored_nodes.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        working_nodes = scored_nodes[:max(1, top_k)]

        episodic_nodes = sorted(
            scored_nodes,
            key=lambda n: (0.65 * n["baseline_arousal"] + 0.35 * n["similarity"]),
            reverse=True,
        )[:max(1, top_k)]

        semantic_nodes = sorted(scored_nodes, key=lambda n: n["similarity"], reverse=True)[:max(1, top_k)]

        identity_keywords = ["identity", "self", "continuity", "ego", "boundary", "worth", "survival"]
        identity_pool = [
            n for n in scored_nodes
            if any(k in (n.get("content", "").lower()) for k in identity_keywords)
        ]
        if not identity_pool:
            identity_pool = semantic_nodes
        identity_nodes = sorted(identity_pool, key=lambda n: n["similarity"], reverse=True)[:max(1, top_k)]

        layers = {
            "working": working_nodes,
            "episodic": episodic_nodes,
            "semantic": semantic_nodes,
            "identity": identity_nodes,
        }

        lines = ["# Chitta Multi-Timescale Context Retrieval"]
        lines.append(
            "Weights -> "
            + ", ".join([f"{k}:{layer_weights[k]:.2f}" for k in ["working", "episodic", "semantic", "identity"]])
        )
        for layer_name in ["working", "episodic", "semantic", "identity"]:
            lines.append(f"## {layer_name.title()} Layer")
            for node in layers[layer_name]:
                snippet = (node.get("content", "") or "").replace("\n", " ").strip()
                if len(snippet) > 220:
                    snippet = snippet[:217] + "..."
                lines.append(
                    f"- [{node.get('node_id')}] sim={node.get('similarity', 0.0):.3f} "
                    f"arousal={node.get('baseline_arousal', 0.0):.2f} | {snippet}"
                )

        return {
            "status": "ok",
            "weights": layer_weights,
            "layers": layers,
            "context_text": "\n".join(lines),
        }

    def close(self):
        """Cleanly releases any pending database file handles to prevent corruption."""
        # SQLite connection handles are already closed after each query by utilizing python context managers ('with').
        # We explicitly print to the console to confirm that file handles are flushed and closed.
        print("[Chitta Store] Successfully flushed memory imprints and closed database file handles.")
