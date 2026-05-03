"""Container lifecycle integration for the xauditor-coder-service runtime.

Lifecycle commands (`xauditor coder {init,build,start,stop,reset,status}`)
plus the auto-start preflight branch are wired through this package.
Mirrors `xauditor.integrations.portal` in shape; smaller because the
coder service is a single container rather than a network of two.
"""

from xauditor.integrations.coder.helpers import (
    build_run_argv,
    derive_socket_path,
    endpoint_kind,
    endpoint_is_local,
)
from xauditor.integrations.coder.package import (
    CoderPackageInfo,
    CoderPackageMissing,
    resolve_coder_package,
)
from xauditor.integrations.coder.runtime import (
    CoderRuntimeManager,
    CoderRuntimeStatus,
    InMemoryCoderRuntimeManager,
)

__all__ = [
    "CoderPackageInfo",
    "CoderPackageMissing",
    "CoderRuntimeManager",
    "CoderRuntimeStatus",
    "InMemoryCoderRuntimeManager",
    "build_run_argv",
    "derive_socket_path",
    "endpoint_is_local",
    "endpoint_kind",
    "resolve_coder_package",
]
