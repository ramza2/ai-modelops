"""Read-only Hugging Face Hub client adapter (M7-A).

Uses the official ``huggingface_hub`` package. Credentials are never persisted;
an optional token may be read from settings/env for gated metadata only.

``HfApi`` (huggingface_hub 0.27.x) does **not** accept a constructor timeout.
Call-level timeouts are passed where supported (e.g. ``model_info``); the
service layer also bounds blocking Hub work with ``asyncio.wait_for``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import get_settings
from app.core.errors import DependencyUnavailableError


@dataclass(frozen=True, slots=True)
class HFModelCard:
    repository_id: str
    revision: str | None = None
    pipeline_tag: str | None = None
    tags: list[str] = field(default_factory=list)
    architectures: list[str] = field(default_factory=list)
    gated: bool | None = None
    private: bool | None = None
    downloads: int | None = None
    likes: int | None = None
    siblings: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] | None = None
    estimated_download_size_bytes: int | None = None


class HuggingFaceHubPort(Protocol):
    def list_models(
        self,
        *,
        query: str | None,
        pipeline_tag: str | None,
        limit: int,
    ) -> list[HFModelCard]: ...

    def get_model(
        self,
        repository_id: str,
        *,
        revision: str | None = None,
    ) -> HFModelCard: ...


def _sibling_dicts(raw_siblings: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not raw_siblings:
        return out
    for item in raw_siblings:
        if isinstance(item, dict):
            out.append(
                {
                    "rfilename": str(
                        item.get("rfilename") or item.get("path") or ""
                    ),
                    "size": item.get("size"),
                }
            )
            continue
        name = getattr(item, "rfilename", None) or getattr(item, "path", None)
        size = getattr(item, "size", None)
        if name is None:
            continue
        out.append({"rfilename": str(name), "size": size})
    return out


def _card_from_model_info(info: Any) -> HFModelCard:
    repo_id = str(getattr(info, "id", None) or getattr(info, "modelId", "") or "")
    tags_raw = getattr(info, "tags", None) or []
    tags = [str(t) for t in tags_raw]
    siblings = _sibling_dicts(getattr(info, "siblings", None))
    config = getattr(info, "config", None)
    if config is not None and not isinstance(config, dict):
        config = None
    architectures: list[str] = []
    if isinstance(config, dict):
        arch = config.get("architectures")
        if isinstance(arch, list):
            architectures = [str(a) for a in arch]

    gated = getattr(info, "gated", None)
    if gated is not None and not isinstance(gated, bool):
        gated = True

    sha = getattr(info, "sha", None)
    downloads = getattr(info, "downloads", None)
    likes = getattr(info, "likes", None)
    private = getattr(info, "private", None)
    pipeline_tag = getattr(info, "pipeline_tag", None)

    size_total = 0
    size_found = False
    for sib in siblings:
        size = sib.get("size")
        if size is None:
            continue
        try:
            size_total += int(size)
            size_found = True
        except (TypeError, ValueError):
            continue

    return HFModelCard(
        repository_id=repo_id,
        revision=str(sha) if sha else None,
        pipeline_tag=str(pipeline_tag) if pipeline_tag else None,
        tags=tags,
        architectures=architectures,
        gated=bool(gated) if gated is not None else None,
        private=bool(private) if private is not None else None,
        downloads=int(downloads) if downloads is not None else None,
        likes=int(likes) if likes is not None else None,
        siblings=siblings,
        config=config,
        estimated_download_size_bytes=size_total if size_found else None,
    )


class HuggingFaceHubClient:
    """Thin wrapper around ``huggingface_hub.HfApi`` (sync; call via to_thread)."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float | None = None,
        api: Any | None = None,
    ) -> None:
        settings = get_settings()
        self._token = token if token is not None else (settings.hf_hub_token or None)
        self._timeout = (
            settings.hf_hub_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        self._api = api

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        try:
            from huggingface_hub import HfApi
        except ImportError as exc:  # pragma: no cover - dependency pin
            raise DependencyUnavailableError(
                "huggingface_hub is not installed.",
                details={"error": type(exc).__name__},
            ) from exc
        # huggingface_hub 0.27.x: HfApi.__init__ has no timeout parameter.
        return HfApi(token=self._token)

    def list_models(
        self,
        *,
        query: str | None,
        pipeline_tag: str | None,
        limit: int,
    ) -> list[HFModelCard]:
        api = self._get_api()
        try:
            iterator = api.list_models(
                search=query or None,
                pipeline_tag=pipeline_tag,
                limit=limit,
                sort="downloads",
                direction=-1,
                full=True,
            )
            return [_card_from_model_info(item) for item in iterator]
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Hugging Face Hub catalog request failed.",
                details={"error": type(exc).__name__},
            ) from exc

    def get_model(
        self,
        repository_id: str,
        *,
        revision: str | None = None,
    ) -> HFModelCard:
        api = self._get_api()
        try:
            info = api.model_info(
                repository_id,
                revision=revision,
                timeout=self._timeout,
                files_metadata=True,
            )
            return _card_from_model_info(info)
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Hugging Face Hub model lookup failed.",
                details={
                    "repository_id": repository_id,
                    "error": type(exc).__name__,
                },
            ) from exc


def build_huggingface_client() -> HuggingFaceHubClient:
    return HuggingFaceHubClient()
