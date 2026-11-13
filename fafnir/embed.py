"""Local sentence embeddings (bge-small-en-v1.5, ONNX, on the CPU so it never competes for
graphics memory). Used to pick the few tools relevant to a request, and later for memory search.
Nothing leaves the PC. Model files live in models/embed/ (133 MB, not committed).
"""
from __future__ import annotations

import threading

import numpy as np

from .config import ROOT

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_lock = threading.Lock()
_state: dict = {}


def _load():
    with _lock:
        if not _state:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            folder = ROOT / "models" / "embed"
            tok = Tokenizer.from_file(str(folder / "tokenizer.json"))
            tok.enable_truncation(max_length=256)
            tok.enable_padding()
            so = ort.SessionOptions()
            so.intra_op_num_threads = 4
            sess = ort.InferenceSession(str(folder / "model.onnx"), so, providers=["CPUExecutionProvider"])
            _state.update(tok=tok, sess=sess, inputs={i.name for i in sess.get_inputs()})
    return _state


def embed(texts: list[str], query: bool = False) -> np.ndarray:
    """Unit-length vectors, one row per text. `query=True` for the thing being searched for."""
    s = _load()
    if query:
        texts = [QUERY_PREFIX + t for t in texts]
    out = []
    for i in range(0, len(texts), 32):
        enc = s["tok"].encode_batch(texts[i:i + 32])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in s["inputs"]:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        cls = s["sess"].run(None, feed)[0][:, 0]            # bge uses the [CLS] vector
        out.append(cls / np.linalg.norm(cls, axis=1, keepdims=True))
    return np.vstack(out) if out else np.zeros((0, 384), dtype=np.float32)
