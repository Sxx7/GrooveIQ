"""Layer-4 embedding-quality tests — the *semantic* contract of the 64-dim
audio embedding that drives all similarity search.

Layers 1-3 (``test_analysis_fixtures``, ``test_onnx_contracts``,
``analysis_health``) treat the embedding as an opaque blob: they assert it
is non-null, unit-norm and 64-dimensional. None of those properties is
violated when the embedding is *wrong*.

That is exactly how d33a880 slipped through. The hand-rolled mel-spectrogram
fed Discogs-EffNet out-of-distribution input — 128 mel bands where the model
expects 96, 96-frame patches where it expects 128, and a transposed patch
axis — and every embedding collapsed onto a single direction. The vectors
were still non-null, still unit-norm, still 64-dim, so Layer 1's
``embedding_norm`` / ``embedding_dim`` bands passed, Layer 2's ONNX I/O
shapes were unchanged, and Layer 3 never inspects the embedding column. But
FAISS nearest-neighbour search returned near-random tracks: a melodic-dubstep
seed surfaced calm orchestral film scores.

The embedding's real contract is *similarity-preserving*: tracks that sound
alike must land close together, tracks that sound different must land apart.
This module tests that contract directly, in two parts that map onto the two
halves of the d33a880 fix:

  Part A — ``TestFaissCentering`` (always runs; no ONNX needed)
      The mean-centring half. EffNet embeddings carry a large shared DC
      component; ``faiss_index`` centres the index so inner-product search
      compares the genre-bearing residual rather than the shared component.
      Synthetic DC-dominated embeddings prove centring expands genre
      separation by an order of magnitude, that query vectors are centred
      too, and that the production index keeps centring enabled.

  Part B — ``TestEmbeddingSemanticQuality`` (skipped without the ONNX bundle)
      The mel-front-end half. Runs four classes of synthetic audio
      (percussive / tonal / distorted / noise — three variants each) through
      the real ``_analyze_file()`` pipeline and asserts the resulting
      embeddings cluster by class: intra-class tracks closer than inter-class
      tracks, and FAISS kNN returning same-class neighbours. A collapsed
      embedding fails every assertion in this part.

Skipped (cleanly) when the Essentia ONNX models are not on disk — same shape
as Layer 1.
"""

from __future__ import annotations

import base64
import itertools
import os

import numpy as np
import pytest

from app.services.faiss_index import FaissIndex, clap_index, effnet_index

# ---------------------------------------------------------------------------
# Thresholds — calibrated against the v2.8 pipeline (the d33a880 fix).
#
#                                       healthy (v2.8)   collapsed (pre-2.8)
#   Part B  intra/inter cosine gap          +0.32              +0.03
#   Part B  mean inter-class cosine          0.40               0.78
#   Part B  FAISS kNN same-class purity      1.00               0.45
#   Part A  centred genre-separation gap    +1.12                 —
#   Part A  uncentred genre-separation gap     —                +0.04
#
# Every threshold sits well inside the healthy/collapsed margin, so a real
# regression fails loudly while ONNX-provider noise (CPU vs CUDA vs OpenVINO)
# — which shifts intra and inter together — does not. The Part B metrics are
# deliberately *relative* (intra vs inter) for that reason.
# ---------------------------------------------------------------------------

_MIN_SEPARATION_GAP = 0.15  # mean(intra cosine) - mean(inter cosine)
_MAX_INTER_CLASS_COSINE = 0.65  # collapse detector — different classes look alike
_MIN_KNN_PURITY = 0.80  # fraction of FAISS neighbours sharing the seed's class
_MIN_CENTERED_GAP = 0.50  # centred index must keep genre structure
_MAX_UNCENTERED_GAP = 0.10  # uncentred index compresses it to ~nothing


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _separation(embeddings: dict) -> tuple[list[float], list[float]]:
    """Split all pairwise cosines into (intra-class, inter-class) lists.

    Keys are ``(class_label, variant)`` tuples; embeddings are unit-norm so
    the cosine is a plain dot product. ``None`` embeddings are skipped.
    """
    keys = [k for k in sorted(embeddings) if embeddings[k] is not None]
    intra: list[float] = []
    inter: list[float] = []
    for a, b in itertools.combinations(keys, 2):
        cos = float(np.dot(embeddings[a], embeddings[b]))
        (intra if a[0] == b[0] else inter).append(cos)
    return intra, inter


def _build_faiss_index(embeddings: dict, *, center: bool) -> tuple[FaissIndex, dict]:
    """Build a ``FaissIndex`` over ``{(class_label, variant): vector}``.

    Mirrors ``FaissIndex.rebuild()`` minus the SQL load — the DB path is
    already covered by ``test_faiss_index.py``; here the subject under test
    is the embedding content and the centring maths. Returns the populated
    index and a ``{track_id: class_label}`` map.
    """
    idx = FaissIndex(dim=64, name="embedding-quality-test", center=center)
    rows: list[tuple[str, str]] = []
    labels: dict[str, object] = {}
    for label, variant in sorted(embeddings):
        vec = embeddings[(label, variant)]
        if vec is None:
            continue
        tid = f"{label}/{variant}"
        labels[tid] = label
        rows.append((tid, base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")))
    index, track_ids, id_map, matrix, centroid, _ = idx._build_sync(rows)
    idx._index = index
    idx._id_to_track = track_ids
    idx._track_to_id = id_map
    idx._embeddings = matrix
    idx._centroid = centroid
    return idx, labels


def _knn_purity(idx: FaissIndex, labels: dict, k: int) -> float:
    """Fraction of each track's top-*k* FAISS neighbours that share its class,
    averaged over every track in the index."""
    same = 0
    total = 0
    for tid in idx._id_to_track:
        for neighbour_id, _score in idx.search_by_track_id(tid, k=k):
            same += int(labels[neighbour_id] == labels[tid])
            total += 1
    return same / total if total else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Part A — FAISS mean-centring (no ONNX required)
# ═══════════════════════════════════════════════════════════════════════════
#
# EffNet embeddings carry a large shared (DC) component. Without centring,
# inner-product similarity is dominated by it and "leaves every track
# ~equally close" (d33a880). These fixtures reproduce that pathology with
# pure linear algebra so the centring half of the fix has a deterministic,
# platform-independent regression test.

_PART_A_CLUSTERS = 4
_PART_A_PER_CLUSTER = 3


def _dc_dominated_embeddings(dc_scale: float = 5.0, jitter: float = 0.05, seed: int = 7) -> dict:
    """Synthetic embeddings mimicking the EffNet pathology: a large shared
    DC component, a small per-cluster residual, and tiny per-track jitter.

    With ``dc_scale=5`` every pair has cosine > 0.95 — the cluster signal is
    a few percent of the vector, just like a real EffNet embedding cloud.
    Returns ``{(cluster, variant): unit-norm 64-dim vector}``.
    """
    rng = np.random.RandomState(seed)
    dc = np.zeros(64, dtype=np.float32)
    dc[0] = 1.0  # the shared component every track points along

    embeddings: dict = {}
    for cluster in range(_PART_A_CLUSTERS):
        residual = np.zeros(64, dtype=np.float32)
        residual[1 + cluster] = 1.0  # one distinct direction per cluster
        for variant in range(_PART_A_PER_CLUSTER):
            jit = rng.randn(64).astype(np.float32) * jitter
            jit[0] = 0.0  # keep the jitter out of the DC axis
            raw = dc_scale * dc + residual + jit
            embeddings[(cluster, variant)] = (raw / np.linalg.norm(raw)).astype(np.float32)
    return embeddings


def _search_score_gap(idx: FaissIndex, labels: dict) -> float:
    """Mean intra-class minus mean inter-class FAISS search score.

    Measured on the *indexed* representation — i.e. the centred residual when
    the index has ``center=True`` — so it reflects what nearest-neighbour
    search actually compares.
    """
    intra: list[float] = []
    inter: list[float] = []
    all_ids = list(idx._id_to_track)
    for tid in all_ids:
        for neighbour_id, score in idx.search_by_track_id(tid, k=len(all_ids)):
            (intra if labels[neighbour_id] == labels[tid] else inter).append(score)
    return float(np.mean(intra)) - float(np.mean(inter))


class TestFaissCentering:
    """Part A — the mean-centring half of d33a880. Pure linear algebra; runs
    everywhere, no ONNX models needed."""

    def test_effnet_index_is_centered(self):
        """The production 64-dim index MUST keep mean-centring enabled.
        Flipping it off re-introduces the d33a880 retrieval failure."""
        assert effnet_index.center is True, (
            "effnet_index.center is False — EffNet embeddings carry a large "
            "shared DC component; without centring, FAISS inner-product "
            "search is dominated by it and returns near-random tracks."
        )
        assert effnet_index.dim == 64

    def test_clap_index_is_not_centered(self):
        """CLAP lives in its own joint text-audio space and is intentionally
        not centred. Pinned so the asymmetry stays deliberate, not drift."""
        assert clap_index.center is False

    def test_centroid_is_the_mean_of_inputs(self):
        """A ``center=True`` build stores the centroid; it must equal the
        mean of the (unit-norm) input vectors."""
        embeddings = _dc_dominated_embeddings()
        idx, _labels = _build_faiss_index(embeddings, center=True)
        expected = np.mean([embeddings[k] for k in sorted(embeddings)], axis=0)
        assert idx._centroid is not None
        np.testing.assert_allclose(idx._centroid, expected, rtol=0, atol=1e-5)

    def test_plain_index_has_no_centroid(self):
        """A ``center=False`` build must not compute or store a centroid."""
        embeddings = _dc_dominated_embeddings()
        idx, _labels = _build_faiss_index(embeddings, center=False)
        assert idx._centroid is None

    def test_centering_expands_genre_separation(self):
        """The headline. On DC-dominated embeddings — where every track is
        ~equally close — mean-centring expands the intra-vs-inter separation
        from near-zero to large. This is the d33a880 "+0.006 -> +0.61"
        genre-separation property captured as a test."""
        embeddings = _dc_dominated_embeddings()
        plain, plain_labels = _build_faiss_index(embeddings, center=False)
        centered, centered_labels = _build_faiss_index(embeddings, center=True)

        plain_gap = _search_score_gap(plain, plain_labels)
        centered_gap = _search_score_gap(centered, centered_labels)

        assert plain_gap <= _MAX_UNCENTERED_GAP, (
            f"uncentred genre-separation gap {plain_gap:.4f} > {_MAX_UNCENTERED_GAP} "
            f"— the DC-dominated fixture no longer reproduces the pathology; "
            f"recalibrate _dc_dominated_embeddings()."
        )
        assert centered_gap >= _MIN_CENTERED_GAP, (
            f"centred genre-separation gap {centered_gap:.4f} < {_MIN_CENTERED_GAP} "
            f"— mean-centring no longer recovers genre structure. Check "
            f"FaissIndex(center=True) and faiss_index._center_and_normalise()."
        )
        # Centring must be the thing that helps — not a no-op.
        assert centered_gap > plain_gap * 3

    def test_query_side_centering_is_applied(self):
        """``search()`` must subtract the centroid from the *query* vector,
        not only from the indexed vectors. Querying a centred index with its
        own centroid therefore reduces to the zero vector and returns []."""
        embeddings = _dc_dominated_embeddings()
        centered, _ = _build_faiss_index(embeddings, center=True)
        plain, _ = _build_faiss_index(embeddings, center=False)

        assert centered._centroid is not None
        # Centred index: query == centroid -> centroid - centroid == 0 -> [].
        assert centered.search(centered._centroid, k=5) == [], (
            "search() did not subtract the centroid from the query vector — "
            "query-side centring regressed; the index would compare a raw "
            "query against centred index entries."
        )
        # Plain index: the very same vector is just an ordinary query.
        assert plain.search(centered._centroid, k=5) != []

    def test_centered_index_retrieves_same_cluster(self):
        """End-to-end on the centred index: every track's nearest neighbours
        belong to its own cluster."""
        embeddings = _dc_dominated_embeddings()
        centered, labels = _build_faiss_index(embeddings, center=True)
        purity = _knn_purity(centered, labels, k=2)
        assert purity >= 0.9, f"centred-index kNN purity {purity:.3f} < 0.9"


# ═══════════════════════════════════════════════════════════════════════════
# Part B — end-to-end embedding semantic quality (needs the ONNX bundle)
# ═══════════════════════════════════════════════════════════════════════════
#
# Skip gate: probe the directory the analysis worker actually loads models
# from (ONNX_MODELS_PATH, else ESSENTIA_MODELS_PATH/onnx, else the default
# cache). Present in the production Docker image, usually absent on a
# developer laptop.


def _resolve_models_dir() -> str:
    """Replicate analysis_worker._get_models_dir()'s resolution (without its
    directory-creating side effect) so the skip gate probes the exact path
    the worker will load from."""
    explicit = os.environ.get("ONNX_MODELS_PATH")
    if explicit:
        return explicit
    base = os.environ.get("ESSENTIA_MODELS_PATH", os.path.expanduser("~/.cache/essentia"))
    return os.path.join(base, "onnx")


_MODELS_DIR = _resolve_models_dir()
_HAS_MODELS = os.path.isdir(_MODELS_DIR) and any(f.endswith(".onnx") for f in os.listdir(_MODELS_DIR))

_skip_without_models = pytest.mark.skipif(
    not _HAS_MODELS,
    reason=f"Essentia ONNX models not found at {_MODELS_DIR} — run inside the Docker image",
)


@pytest.fixture(scope="session")
def _onnx_pipeline():
    """Load the ONNX sessions and the JL projection matrix once per session,
    matching what an analysis worker builds at start-up."""
    from app.services import analysis_worker as aw

    aw._download_models()
    onnx_sessions = aw._init_onnx_sessions()
    if not onnx_sessions:
        pytest.skip("ONNX sessions failed to initialise")

    # Same deterministic 1280->64 projection the worker derives (seed pinned).
    rng = np.random.RandomState(seed=20240101)
    proj = (rng.randn(1280, 64) / np.sqrt(64)).astype(np.float32)
    return onnx_sessions, proj


@pytest.fixture(scope="session")
def _family_embeddings(_onnx_pipeline):
    """Run every fixture-family variant through the real ``_analyze_file()``
    pipeline exactly once. Returns ``{(class, variant): embedding}`` where the
    embedding is a unit-norm 64-dim float32 vector (or ``None`` on failure)."""
    import tempfile

    import essentia.standard as es

    from app.services import analysis_worker as aw
    from tests.fixtures.audio.families import write_families

    onnx_sessions, proj = _onnx_pipeline
    embeddings: dict = {}
    with tempfile.TemporaryDirectory() as fixture_dir:
        paths = write_families(fixture_dir)
        for cls, variants in paths.items():
            for variant, path in variants.items():
                result = aw._analyze_file(path, None, es, onnx_sessions, proj, None)
                emb_b64 = result.get("embedding") if result else None
                if emb_b64:
                    embeddings[(cls, variant)] = np.frombuffer(base64.b64decode(emb_b64), dtype=np.float32)
                else:
                    embeddings[(cls, variant)] = None
    return embeddings


@_skip_without_models
class TestEmbeddingSemanticQuality:
    """Part B — the mel-spectrogram-front-end half of d33a880. Runs the real
    analysis pipeline on synthetic audio of known musical character and
    asserts the embeddings actually carry that character."""

    def test_all_fixtures_produce_valid_embeddings(self, _family_embeddings):
        """Every family fixture yields a finite, unit-norm, 64-dim embedding.
        A NULL embedding here is the #42 / #83 failure mode on real audio."""
        for (cls, variant), vec in _family_embeddings.items():
            tag = f"{cls}/{variant}"
            assert vec is not None, f"{tag}: pipeline produced no embedding"
            assert vec.shape == (64,), f"{tag}: embedding dim {vec.shape} != (64,)"
            assert np.isfinite(vec).all(), f"{tag}: embedding has NaN/Inf"
            norm = float(np.linalg.norm(vec))
            assert 0.99 <= norm <= 1.01, f"{tag}: embedding norm {norm:.4f} not unit-length"

    def test_embeddings_not_collapsed(self, _family_embeddings):
        """The d33a880 failure mode: out-of-distribution mel input collapsed
        every embedding onto one direction, so tracks of *different* musical
        classes looked near-identical. A healthy pipeline keeps them apart."""
        _intra, inter = _separation(_family_embeddings)
        mean_inter = float(np.mean(inter))
        assert mean_inter <= _MAX_INTER_CLASS_COSINE, (
            f"mean inter-class cosine {mean_inter:.4f} > {_MAX_INTER_CLASS_COSINE} "
            f"— embeddings from different musical classes (percussive vs tonal "
            f"vs distorted vs noise) are nearly identical. The audio embedding "
            f"has collapsed onto a shared direction (the pre-2.8 EffNet "
            f"mel-spectrogram front-end bug, d33a880)."
        )

    def test_embeddings_separate_musical_classes(self, _family_embeddings):
        """Same-class tracks must sit closer than different-class tracks.
        This is the genre-separation gap d33a880 moved from +0.006 to +0.61
        on the production library."""
        intra, inter = _separation(_family_embeddings)
        mean_intra = float(np.mean(intra))
        mean_inter = float(np.mean(inter))
        gap = mean_intra - mean_inter
        assert gap >= _MIN_SEPARATION_GAP, (
            f"intra/inter cosine gap {gap:.4f} < {_MIN_SEPARATION_GAP} "
            f"(intra={mean_intra:.4f}, inter={mean_inter:.4f}) — same-class "
            f"tracks are barely closer than random pairs, so the embedding "
            f"carries almost no musical-similarity signal. FAISS search built "
            f"on it returns near-random tracks (d33a880)."
        )

    def test_faiss_retrieves_same_class_neighbours(self, _family_embeddings):
        """End-to-end through the production FAISS path: build a centred
        64-dim index over the family embeddings and confirm each track's
        nearest neighbours share its musical class. A collapsed embedding
        scatters neighbours across classes at ~chance rate."""
        idx, labels = _build_faiss_index(_family_embeddings, center=True)
        nn_same_class = _knn_purity(idx, labels, k=1)
        top2_purity = _knn_purity(idx, labels, k=2)

        assert nn_same_class >= _MIN_KNN_PURITY, (
            f"nearest-neighbour same-class rate {nn_same_class:.3f} < {_MIN_KNN_PURITY} "
            f"— FAISS search returns tracks of the wrong musical class. The "
            f"embedding driving similarity search is degenerate (d33a880)."
        )
        assert top2_purity >= _MIN_KNN_PURITY, f"top-2 same-class purity {top2_purity:.3f} < {_MIN_KNN_PURITY}"
