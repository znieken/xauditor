"""HTTP microservice fronting the Claude Code CLI for xauditor.

Lives in ``packages/xauditor-coder-service/`` and is shipped as a
separate Python wheel + container image so the audit runtime can offload
verification to a long-lived worker pool. See the parent project README
for deployment options (sidecar via docker-compose, or remote / k8s).
"""

from xauditor_coder_service.app import create_app

__all__ = ["create_app"]
