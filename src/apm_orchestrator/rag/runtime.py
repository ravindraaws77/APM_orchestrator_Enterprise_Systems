"""Process-wide ``AnswerModel`` built from settings, mirroring
``apm_orchestrator.tools.get_client``. Tests replace it with
``set_answer_model``."""

from __future__ import annotations

from apm_orchestrator.config import Settings, load_settings
from apm_orchestrator.rag.answer import AnswerModel, AnthropicAnswerModel

_model: AnswerModel | None = None


def build_answer_model(settings: Settings) -> AnthropicAnswerModel:
    """The pinned RAG model (``RAG_MODEL``), never an inherited default."""
    return AnthropicAnswerModel(
        model=settings.rag_model,
        max_tokens=settings.rag_max_tokens,
        api_key=settings.anthropic_api_key,
    )


def get_answer_model() -> AnswerModel:
    global _model
    if _model is None:
        _model = build_answer_model(load_settings())
    return _model


def set_answer_model(model: AnswerModel | None) -> None:
    global _model
    _model = model
