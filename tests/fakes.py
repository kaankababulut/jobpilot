"""Test doubles that stand in for heavy or networked parts (the embedding model)."""
import hashlib
import math
import re


class FakeEmbedder:
    """Hashed bag of words: each word adds 1 to one of 384 slots, then the vector is scaled to length 1.
    Deterministic and instant; texts sharing words get a higher cosine, which is all the tests need."""
    model = "fake@test"
    dims = 384

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for word in re.findall(r"\w+", text.lower()):
            # hashlib, not hash(): Python salts str hashes per process, so vectors would differ between runs
            v[int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % self.dims] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)
