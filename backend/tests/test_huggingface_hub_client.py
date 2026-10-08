"""Non-network regression: pinned huggingface_hub HfApi constructor compatibility."""

from __future__ import annotations

import inspect

from huggingface_hub import HfApi

from app.clients.huggingface import HuggingFaceHubClient


def test_hf_api_constructor_has_no_timeout_parameter() -> None:
    """Guard against reintroducing HfApi(..., timeout=...) on 0.27.x."""
    sig = inspect.signature(HfApi.__init__)
    assert "timeout" not in sig.parameters


def test_real_hf_api_adapter_instantiates_without_timeout_kwarg() -> None:
    """Instantiate the real pinned HfApi path (no network) via the adapter."""
    client = HuggingFaceHubClient(token=None, timeout_seconds=7.5, api=None)
    assert client.timeout_seconds == 7.5
    api = client._get_api()
    assert isinstance(api, HfApi)
    # model_info still accepts timeout at call site on 0.27.x
    info_sig = inspect.signature(HfApi.model_info)
    assert "timeout" in info_sig.parameters
    assert "files_metadata" in info_sig.parameters
