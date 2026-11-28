"""Tier-1 local CPU NLI cross-encoder verifier (opt-in via `--fast` / `--verifier nli`, ADR-11).

Design invariants:
- Completely optional: `onnxruntime` and `tokenizers` are imported lazily; base installation never requires them.
- Offline-safe: loads local ONNX artifacts from `CAIRNMIND_MODEL_DIR` or `<data_dir>/models/nli/` without
  making unsolicited network downloads; validates optional `sha256.txt` before loading.
- Fail-closed triage (ADR-11):
  - `secondary_file=True` (test, fixture, lock, minified file) ALWAYS escalates to Tier-2 LLM.
  - `contradiction >= 0.90` rejects immediately at Tier 1 without invoking any LLM.
  - `entailment >= 0.90` on primary files only accepts at Tier 1 (skipping Tier-2 support & names LLM calls).
  - Uncertain scores (`< 0.90` for both) or unavailable ONNX model escalate to Tier-2 LLM; if Tier-2 is also
    unavailable, the caller fails closed with `status="partial"`, `code="needs_llm_review"`.
- Premise uses the ±4-line context window around each citation so negations (`if False:`, `# deprecated`)
  just outside the quote are evaluated rather than dropped.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnmind.agents.support import support_citations

ENTAILMENT_THRESHOLD = 0.90
CONTRADICTION_THRESHOLD = 0.90
DEFAULT_VERIFIER = "llm"
VERIFIER_MODES = ("llm", "nli")
NEEDS_LLM_REVIEW_CODE = "needs_llm_review"


class NliUnavailable(RuntimeError):
    """Raised when the optional ONNX NLI runtime or local model files are unavailable or invalid."""


@dataclass(frozen=True)
class NliScores:
    entailment: float
    neutral: float
    contradiction: float


@dataclass(frozen=True)
class NliDecision:
    decision: str  # "accept" | "reject" | "escalate"
    reason: str
    entailment: float = 0.0
    contradiction: float = 0.0
    neutral: float = 0.0
    model_id: str = "nli-unavailable"


def _softmax(logits: list[float]) -> list[float]:
    peak = max(logits)
    exps = [math.exp(x - peak) for x in logits]
    total = sum(exps) or 1.0
    return [v / total for v in exps]


def resolve_nli_model_dir(data_dir: str | Path | None = None) -> Path | None:
    """Locate the local ONNX NLI model directory from `CAIRNMIND_MODEL_DIR` or `<data_dir>/models/nli`."""
    override = os.environ.get("CAIRNMIND_MODEL_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
        if (base / "model.onnx").is_file():
            return base
        if (base / "nli" / "model.onnx").is_file():
            return base / "nli"
        return base
    if data_dir is not None:
        return Path(data_dir).expanduser() / "models" / "nli"
    return None


def _verify_optional_sha256(model_dir: Path, model_file: Path) -> str:
    """Return SHA-256 of `model.onnx`, raising `NliUnavailable` if `sha256.txt` exists and mismatches."""
    digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
    checksum_file = model_dir / "sha256.txt"
    if checksum_file.is_file():
        expected = checksum_file.read_text(encoding="utf-8").strip().split()[0].lower()
        if expected and expected != digest:
            raise NliUnavailable(f"NLI model checksum mismatch for {model_file.name}: expected {expected[:12]}, got {digest[:12]}")
    return digest


class OnnxNliScorer:
    """Lazy CPU-only ONNX Runtime cross-encoder scorer for NLI entailment/contradiction."""

    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        model_file = self.model_dir / "model.onnx"
        tokenizer_file = self.model_dir / "tokenizer.json"
        if not model_file.is_file() or not tokenizer_file.is_file():
            raise NliUnavailable(
                f"Local ONNX NLI model not found in {self.model_dir} (expected model.onnx and tokenizer.json). "
                "Set CAIRNMIND_MODEL_DIR to a local model folder or fall back to Tier-2 LLM."
            )
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NliUnavailable(
                "Optional ONNX NLI dependencies are not installed; install with `pip install 'cairnmind[nli]'`."
            ) from exc

        self.digest = _verify_optional_sha256(self.model_dir, model_file)
        self.model_id = f"onnx-nli@{self.digest[:16]}"
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self._tokenizer.enable_truncation(max_length=512)
        self._session = ort.InferenceSession(str(model_file), providers=["CPUExecutionProvider"])
        self._input_names = {inp.name for inp in self._session.get_inputs()}
        self._label_map = self._load_label_map()

    def _load_label_map(self) -> dict[str, int]:
        # Default DeBERTa-v3 / MNLI ordering: 0=contradiction, 1=neutral, 2=entailment
        mapping = {"contradiction": 0, "neutral": 1, "entailment": 2}
        config_file = self.model_dir / "config.json"
        if config_file.is_file():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                id2label = cfg.get("id2label")
                if isinstance(id2label, dict):
                    parsed: dict[str, int] = {}
                    for idx_str, label in id2label.items():
                        key = str(label).strip().lower()
                        if "entail" in key:
                            parsed["entailment"] = int(idx_str)
                        elif "contra" in key:
                            parsed["contradiction"] = int(idx_str)
                        elif "neut" in key:
                            parsed["neutral"] = int(idx_str)
                    if len(parsed) == 3:
                        return parsed
            except (OSError, ValueError, TypeError):
                pass
        return mapping

    def score(self, premise: str, hypothesis: str) -> NliScores:
        encoded = self._tokenizer.encode(premise, hypothesis)
        feeds: dict[str, Any] = {
            "input_ids": [encoded.ids],
            "attention_mask": [encoded.attention_mask],
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = [encoded.type_ids]
        outputs = self._session.run(None, feeds)
        raw_logits = [float(v) for v in outputs[0][0]]
        probs = _softmax(raw_logits)
        return NliScores(
            entailment=probs[self._label_map["entailment"]],
            neutral=probs[self._label_map["neutral"]],
            contradiction=probs[self._label_map["contradiction"]],
        )


_SCORER_CACHE: dict[str, OnnxNliScorer] = {}


def load_nli_scorer(data_dir: str | Path | None = None) -> OnnxNliScorer:
    model_dir = resolve_nli_model_dir(data_dir)
    if model_dir is None:
        raise NliUnavailable("No NLI model directory configured.")
    key = str(model_dir.resolve())
    cached = _SCORER_CACHE.get(key)
    if cached is not None:
        return cached
    scorer = OnnxNliScorer(model_dir)
    _SCORER_CACHE[key] = scorer
    return scorer


def evaluate_tier1_nli(
    question: str,
    answer: str,
    sources: list[dict[str, Any]],
    *,
    evidence: dict[str, dict[str, Any]] | None = None,
    source_text_fn=None,
    scorer: Callable[[str, str], NliScores] | Any | None = None,
    data_dir: str | Path | None = None,
    entailment_threshold: float = ENTAILMENT_THRESHOLD,
    contradiction_threshold: float = CONTRADICTION_THRESHOLD,
) -> NliDecision:
    """Run Tier-1 NLI triage over citations using ±4-line context windows (ADR-11).

    `scorer` may be an `OnnxNliScorer`, an object with `.score(premise, hypothesis) -> NliScores`,
    a callable `(premise, hypothesis) -> NliScores`, or None (auto-loads local ONNX model).
    """
    citations = support_citations(sources, evidence, source_text_fn=source_text_fn)
    if not citations:
        return NliDecision("escalate", "no_citations")

    # ADR-11: secondary files (tests/fixtures/locks/minified) ALWAYS escalate to Tier-2 LLM.
    if any(item.get("secondary_file") for item in citations):
        return NliDecision("escalate", "secondary_file_requires_llm")

    active_scorer = scorer
    model_id = "custom-nli"
    if active_scorer is None:
        try:
            active_scorer = load_nli_scorer(data_dir)
        except NliUnavailable as exc:
            return NliDecision("escalate", f"nli_unavailable: {exc}")
    if hasattr(active_scorer, "model_id"):
        model_id = str(active_scorer.model_id)

    score_method = getattr(active_scorer, "score", None)
    score_fn: Callable[[str, str], Any]
    if callable(score_method):
        score_fn = score_method
    elif callable(active_scorer):
        score_fn = active_scorer
    else:
        return NliDecision("escalate", "nli_error: UncallableScorer", model_id=model_id)
    hypothesis = f"Question: {question.strip()}\nAnswer: {answer.strip()}"

    max_entail = 0.0
    max_contra = 0.0
    best_neutral = 0.0
    for item in citations:
        premise = str(item.get("context") or item.get("quote") or "")
        try:
            scores = score_fn(premise, hypothesis)
        except Exception as exc:  # noqa: BLE001 - fail closed into escalation
            return NliDecision("escalate", f"nli_error: {type(exc).__name__}", model_id=model_id)
        if isinstance(scores, dict):
            scores = NliScores(
                entailment=float(scores.get("entailment", 0.0)),
                neutral=float(scores.get("neutral", 0.0)),
                contradiction=float(scores.get("contradiction", 0.0)),
            )
        if scores.contradiction > max_contra:
            max_contra = scores.contradiction
        if scores.entailment > max_entail:
            max_entail = scores.entailment
            best_neutral = scores.neutral

    # Reject on strong contradiction before entailment so a contradicting citation is never overridden.
    if max_contra >= contradiction_threshold:
        return NliDecision(
            "reject",
            f"NLI contradiction ({max_contra:.2f} >= {contradiction_threshold:.2f}).",
            entailment=max_entail,
            contradiction=max_contra,
            neutral=best_neutral,
            model_id=model_id,
        )
    if max_entail >= entailment_threshold:
        return NliDecision(
            "accept",
            f"NLI entailment ({max_entail:.2f} >= {entailment_threshold:.2f}).",
            entailment=max_entail,
            contradiction=max_contra,
            neutral=best_neutral,
            model_id=model_id,
        )
    return NliDecision(
        "escalate",
        f"NLI uncertain (entailment={max_entail:.2f}, contradiction={max_contra:.2f}); escalating to Tier-2 LLM.",
        entailment=max_entail,
        contradiction=max_contra,
        neutral=best_neutral,
        model_id=model_id,
    )
