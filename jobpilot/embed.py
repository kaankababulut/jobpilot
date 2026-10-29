"""Embeddings for semantic search. An embedding is a list of numbers (a vector) that a small language
model computes from a text, so that texts with similar meaning get vectors pointing in similar directions.
Comparing two vectors (cosine similarity) then measures how alike two jobs, or a job and the CV, are.
This module builds the text that gets embedded, defines the Embedder interface and runs the real model
(multilingual-e5-small, int8 ONNX) on the CPU. Download it once with `python -m jobpilot.embed --download`."""
import argparse
import hashlib
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

PASSAGE_PREFIX = "passage: "  # e5 models are trained with "passage: " for documents and "query: " for searches
QUERY_PREFIX = "query: "


class Embedder(Protocol):
    """Anything that turns texts into vectors: the real ONNX model, or FakeEmbedder in tests."""
    model: str  # stored next to each vector, so vectors from different models are never compared
    dims: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _skill_names(skills) -> list[str]:
    # job_detail gives [{name, ...}], the list view and records give plain names; accept both
    names = [s.get("name") if isinstance(s, Mapping) else s for s in (skills or [])]
    return [str(n).strip() for n in names if n and str(n).strip()]


def embed_text(job: Mapping) -> str:
    """'passage: title | company | skill, skill | description', skipping empty parts.
    Whitespace is collapsed so formatting-only changes don't change text_hash and force a re-embed."""
    parts = [job.get("title"), job.get("company"), ", ".join(_skill_names(job.get("skills"))),
             job.get("description")]
    parts = [re.sub(r"\s+", " ", str(p)).strip() for p in parts if p is not None]
    return PASSAGE_PREFIX + " | ".join(p for p in parts if p)


def text_hash(text: str) -> str:
    """sha256 of the embedded text: if it hasn't changed, the stored vector is still valid."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_pgvector(vec: Sequence[float]) -> str:
    """pgvector's text form '[0.1,0.2]', passed as %s::vector, so no pgvector Python package is needed."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# The model files, pinned to one commit of the author's repo and checked by sha256, so a changed or
# tampered upload can never silently change the vectors. The qint8 file is ~118 MB instead of 470 MB fp32.
HF_REPO = "intfloat/multilingual-e5-small"
HF_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_FILE = "model_qint8_avx512_vnni.onnx"
TOKENIZER_FILE = "tokenizer.json"
FILES = {  # local name -> (path in the repo, sha256)
    MODEL_FILE: ("onnx/model_qint8_avx512_vnni.onnx",
                 "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
    TOKENIZER_FILE: ("onnx/tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL_DIR = os.path.join(ROOT, "models", "me5-small")
MAX_TOKENS = 512  # the model's position limit; longer texts are cut, which keeps title/company/skills (they come first)


class OnnxEmbedder:
    """multilingual-e5-small run with onnxruntime + tokenizers + numpy (no torch, a far smaller install).
    Prefix contract: embed_passages expects texts from embed_text, which already start with "passage: ";
    embed_query takes the raw search words and adds "query: ". Either prefix is added only if missing."""
    dims = 384
    model = f"multilingual-e5-small@qint8-{HF_REVISION[:7]}"

    # batch_size 1 by default (measured on the PC): job texts vary in length, so batching mostly computes padding
    # (1 was ~40% faster than 4), a batch of 16 at 512 tokens needs ~450 MB more RAM, and the int8 model scales
    # its activations per batch, so padding nudges a text's vector (cosine ~0.998 vs alone). With 1, the same
    # text always gets exactly the same vector, whatever it was embedded alongside.
    def __init__(self, model_dir: str, batch_size: int = 1):
        # imported here, so `import jobpilot.embed` works on a machine without these packages
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self._np = np
        self.batch_size = batch_size
        opts = ort.SessionOptions()
        # one thread: the 12:00 run and the small API container share their CPU; extra threads only add RAM
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        # no arena: ORT would otherwise keep the largest batch's memory for good; this hands it back to the OS
        opts.enable_cpu_mem_arena = False
        self._session = ort.InferenceSession(os.path.join(model_dir, MODEL_FILE), sess_options=opts,
                                             providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tok = Tokenizer.from_file(os.path.join(model_dir, TOKENIZER_FILE))
        self._tok.enable_truncation(max_length=MAX_TOKENS)
        # no fixed length: each batch is padded only to its own longest text, not always to 512
        self._tok.enable_padding(pad_id=self._tok.token_to_id("<pad>"), pad_token="<pad>")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        np = self._np
        out = []
        for i in range(0, len(texts), self.batch_size):
            enc = self._tok.encode_batch(texts[i:i + self.batch_size])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:  # some exports have this BERT input, others don't
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feeds)[0]  # (batch, tokens, 384): one vector per token
            # mean pooling: average the token vectors into one per text, ignoring padding (mask 0),
            # otherwise a short text batched with a long one would be diluted by pad vectors
            m = mask[:, :, None].astype(hidden.dtype)
            vecs = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            # L2 normalisation: scale to length 1, so the dot product is the cosine similarity
            vecs /= np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
            out.extend(vecs.astype(float).tolist())
        return out

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return self._embed([t if t.startswith(PASSAGE_PREFIX) else PASSAGE_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text if text.startswith(QUERY_PREFIX) else QUERY_PREFIX + text])[0]


def load_embedder(model_dir: str = DEFAULT_MODEL_DIR) -> tuple[Embedder | None, str]:
    """(embedder, "") or (None, reason to log). Never raises: a missing model only skips embeddings."""
    missing = [f for f in FILES if not os.path.isfile(os.path.join(model_dir, f))]
    if missing:
        return None, f"model files missing in {model_dir} ({', '.join(missing)}); run python -m jobpilot.embed --download"
    try:
        return OnnxEmbedder(model_dir), ""
    except Exception as e:  # packages not installed, corrupt file, ...
        return None, f"model not loaded ({type(e).__name__}: {e})"


# every job with its skills and the hash stored for this model (NULL: no vector yet), in one round trip.
# Skills are sorted in Python, not by ORDER BY: local and Azure may use different collations, and the
# text must be identical on both or the memo misses and the two databases get different vectors.
JOBS_TO_CHECK_SQL = """
SELECT j.id, j.title, j.company, j.description,
       array_remove(array_agg(s.name), NULL) AS skills, e.text_hash
FROM jobs j
LEFT JOIN job_skills js ON js.job_id = j.id
LEFT JOIN skills s ON s.skill_id = js.skill_id
LEFT JOIN job_embeddings e ON e.job_id = j.id AND e.model = %s
GROUP BY j.id, e.text_hash
ORDER BY j.id"""
UPSERT_JOB_VECTOR_SQL = (
    "INSERT INTO job_embeddings (job_id, model, embedding, dims, text_hash) VALUES (%s, %s, %s::vector, %s, %s) "
    "ON CONFLICT (job_id, model) DO UPDATE SET embedding = EXCLUDED.embedding, dims = EXCLUDED.dims, "
    "text_hash = EXCLUDED.text_hash, updated_at = now()")
UPSERT_CV_VECTOR_SQL = (
    "INSERT INTO cv_embeddings (model, embedding, text_hash) VALUES (%s, %s::vector, %s) "
    "ON CONFLICT (model) DO UPDATE SET embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, "
    "updated_at = now()")
CHUNK = 50  # commit every 50 vectors: a crash or timeout keeps what's done, and the next run resumes from there


def _sync_cv(conn, embedder: Embedder, cv_text: str, memo: dict) -> None:
    # The CV is embedded as a "query: ", not a "passage: ". e5 is trained asymmetrically: a short "what I'm
    # looking for" (query) is matched against the documents that answer it (passages). Ranking jobs for the CV
    # is that retrieval task, the CV being the question and the jobs the answers, and it puts the CV on the
    # same side as /search queries. Long CVs are cut at 512 tokens, so the profile should lead with the essentials.
    text = QUERY_PREFIX + re.sub(r"\s+", " ", cv_text).strip()
    if text == QUERY_PREFIX:
        return
    h = text_hash(text)
    row = conn.execute("SELECT text_hash FROM cv_embeddings WHERE model = %s", (embedder.model,)).fetchone()
    if row and row[0] == h:
        return
    if h not in memo:
        memo[h] = embedder.embed_query(text)
    conn.execute(UPSERT_CV_VECTOR_SQL, (embedder.model, to_pgvector(memo[h]), h))
    conn.commit()


def sync_embeddings(conn, embedder: Embedder, cv_text: str | None = None, memo: dict | None = None) -> int:
    """Embeds every job whose vector for embedder.model is missing or was made from different text, plus the
    CV when cv_text is given; returns how many job vectors were written. Commits per chunk of CHUNK jobs.
    memo maps model -> {text_hash -> vector}: pass the same dict for the second database in a run (local,
    then Azure) and identical texts are embedded only once. Keyed by model, so a shared memo can never hand
    one model's vector to another (same text, same dims, but numbers that mean different things)."""
    memo = (memo if memo is not None else {}).setdefault(embedder.model, {})
    if cv_text:
        _sync_cv(conn, embedder, cv_text, memo)
    todo, empty = [], []
    for job_id, title, company, description, skills, stored in conn.execute(JOBS_TO_CHECK_SQL, (embedder.model,)):
        text = embed_text({"title": title, "company": company, "skills": sorted(skills), "description": description})
        if text == PASSAGE_PREFIX:  # nothing to embed: a vector of no words is noise that would rank at random
            if stored is not None:
                empty.append(job_id)
        elif stored != (h := text_hash(text)):
            todo.append((job_id, text, h))
    conn.commit()  # ends the read transaction, so each chunk below is its own short one
    if empty:  # the text was emptied since: drop the stale vector rather than keep describing the old text
        conn.execute("DELETE FROM job_embeddings WHERE model = %s AND job_id = ANY(%s)", (embedder.model, empty))
        conn.commit()
    written = 0
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        new = list(dict.fromkeys(h for _, _, h in chunk if h not in memo))  # dedupe: identical reposts embed once
        texts = {h: t for _, t, h in chunk}
        for h, vec in zip(new, embedder.embed_passages([texts[h] for h in new])):
            memo[h] = vec
        with conn.cursor() as cur:
            cur.executemany(UPSERT_JOB_VECTOR_SQL,
                            [(job_id, embedder.model, to_pgvector(memo[h]), embedder.dims, h) for job_id, _, h in chunk])
        conn.commit()
        written += len(chunk)
    return written


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(model_dir: str = DEFAULT_MODEL_DIR, log: Callable[[str], None] = print) -> None:
    """Fetch the pinned files over HTTPS. Each file goes to a temp name and is renamed only after its sha256
    matches, so a cut or tampered download never leaves a file that looks valid. Verified files are skipped."""
    os.makedirs(model_dir, exist_ok=True)
    for name, (repo_path, sha) in FILES.items():
        dest = os.path.join(model_dir, name)
        if os.path.isfile(dest) and _sha256(dest) == sha:
            log(f"{name}: already verified")
            continue
        url = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{repo_path}"
        tmp = dest + ".part"
        h = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    h.update(chunk)
                    f.write(chunk)
            if h.hexdigest() != sha:
                raise ValueError(f"{name}: sha256 mismatch (got {h.hexdigest()}, expected {sha})")
            os.replace(tmp, dest)  # atomic on one disk: dest is either the old file or the verified one
        finally:
            try:
                os.remove(tmp)
            except OSError:  # already renamed, or briefly locked (antivirus): don't hide the real error
                pass
        log(f"{name}: downloaded and verified")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobpilot.embed", description="Embedding model tools")
    parser.add_argument("--download", action="store_true", help=f"download the pinned {HF_REPO} files")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR, help="where the model files live")
    args = parser.parse_args(argv)
    if not args.download:
        parser.print_help()
        return 1
    try:
        download(args.model_dir)
    except Exception as e:
        print(f"ERROR: download failed: {e}", file=sys.stderr)
        return 1
    start = time.perf_counter()
    embedder, reason = load_embedder(args.model_dir)  # proves the files actually load, not just that they exist
    if embedder is None:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    print(f"{embedder.model} loaded in {time.perf_counter() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
