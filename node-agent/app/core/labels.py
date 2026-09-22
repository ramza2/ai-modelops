"""ModelOps managed-container Docker label constants."""

from __future__ import annotations

LABEL_MANAGED = "ai.modelops.managed"
LABEL_DEPLOYMENT_ID = "ai.modelops.deployment_id"
LABEL_MODEL_ID = "ai.modelops.model_id"
LABEL_NODE_ID = "ai.modelops.node_id"

MANAGED_LABEL_VALUE = "true"

REQUIRED_LABEL_KEYS = (
    LABEL_MANAGED,
    LABEL_DEPLOYMENT_ID,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
)
