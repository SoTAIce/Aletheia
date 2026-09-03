"""Local Cross-Encoder implementations of the reranker provider contract."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Sequence


class TransformersCrossEncoderProvider:
    """Score query/document pairs with a locally loaded Transformers model."""

    def __init__(
        self,
        model_name: str,
        cache_dir: str | Path,
        *,
        offline: bool = True,
        device: str | None = None,
        batch_size: int = 8,
        max_length: int = 512,
        use_fp16: bool = True,
        scorer: Callable[[str, Sequence[str]], Sequence[float]] | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(8, int(max_length))
        self._injected_scorer = scorer
        if scorer is not None:
            self.device = device or "injected"
            self.use_fp16 = False
            return

        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.use_fp16 = bool(use_fp16 and self.device.startswith("cuda"))
        load_kwargs: dict[str, Any] = {
            "cache_dir": str(self.cache_dir),
            "local_files_only": offline,
        }
        if self.use_fp16:
            load_kwargs["dtype"] = torch.float16
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, **load_kwargs)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            model_name, **load_kwargs
        )
        self._model.to(self.device)
        self._model.eval()

    def _score_sync(self, question: str, documents: Sequence[str]) -> list[float]:
        if self._injected_scorer is not None:
            return [float(score) for score in self._injected_scorer(question, documents)]

        scores: list[float] = []
        with self._torch.inference_mode():
            for start in range(0, len(documents), self.batch_size):
                batch = list(documents[start : start + self.batch_size])
                inputs = self._tokenizer(
                    [question] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    return_tensors="pt",
                    max_length=self.max_length,
                )
                inputs = {name: tensor.to(self.device) for name, tensor in inputs.items()}
                logits = self._model(**inputs, return_dict=True).logits.view(-1).float()
                scores.extend(float(score) for score in logits.cpu().tolist())
        if len(scores) != len(documents):
            raise ValueError("local Transformers reranker did not score every passage")
        return scores

    async def score(
        self, question: str, documents: Sequence[str], model: str
    ) -> Sequence[float]:
        if model != self.model_name:
            raise ValueError(f"requested reranker model {model!r} does not match provider model")
        return await asyncio.to_thread(self._score_sync, question, documents)


class LocalCrossEncoderRerankerProvider(TransformersCrossEncoderProvider):
    """Runtime BGE provider used by the knowledge retrieval chain."""
