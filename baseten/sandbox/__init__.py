"""Create and work with Baseten sandboxes.

Create one :class:`SandboxClient` up front and reuse it, then create a sandbox
and run a command in it::

    from baseten.sandbox import SandboxClient

    with SandboxClient(api_key="my-api-key") as client:
        sandbox = client.create()
        process = sandbox.process.exec(
            command="echo hello", wait_for_completion=True
        )
        print(process.stdout)

:class:`AsyncSandboxClient` is the same for async code. HTTP/2 is used when the
``h2`` package is installed, which is recommended: ``pip install
"httpx[http2]"``.
"""

from baseten.sandbox._auth import (
    AsyncSandboxTokenProvider,
    SandboxTokenProvider,
    SandboxTokenProviderContext,
)
from baseten.sandbox._client import (
    AsyncSandboxClient,
    AsyncSandboxCreateResult,
    SandboxClient,
    SandboxClientOptions,
    SandboxCreateResult,
)
from baseten.sandbox._common import AsyncSandboxStream, SandboxStream
from baseten.sandbox._errors import (
    ImageBuildError,
    ImageUploadError,
    SandboxAPIError,
    SandboxError,
    SandboxGatewayError,
    SandboxProcessWaitTimeoutError,
)
from baseten.sandbox._fs import (
    AsyncSandboxFileSystem,
    SandboxFileSystem,
    SandboxFileSystemCopyError,
    SandboxFileSystemDirectory,
    SandboxFileSystemEntryType,
    SandboxFileSystemFileInfo,
    SandboxFileSystemFindMatch,
    SandboxFileSystemFindResult,
    SandboxFileSystemGrepMatch,
    SandboxFileSystemGrepResult,
    SandboxFileSystemSubdirectoryInfo,
    SandboxFileSystemWatchEvent,
    SandboxFileSystemWatchOp,
)
from baseten.sandbox._images import (
    AsyncImageClient,
    ImageCleanupResult,
    ImageClient,
    ImageInfo,
    ImageLibraryInfo,
    ImageLibraryVolume,
    ImageLogLine,
    ImageSort,
    ImageStatus,
    ImageTagInfo,
    ImageTagSort,
)
from baseten.sandbox._images_builder import ImageBuilder
from baseten.sandbox._images_ignore import (
    AsyncImageIgnoreFileFunc,
    AsyncImageIgnoreFileProcessor,
    ImageIgnoreFileFunc,
    ImageIgnoreFileOptions,
    ImageIgnoreFileProcessor,
    ImageIgnoreFileProcessorOptions,
    default_image_ignore_file,
)
from baseten.sandbox._info import (
    SandboxEnvValue,
    SandboxExpirationAction,
    SandboxExpirationPolicy,
    SandboxExpirationPolicyDate,
    SandboxExpirationPolicyIdle,
    SandboxExpirationPolicyMaxAge,
    SandboxExpirationPolicyUnknown,
    SandboxInfo,
    SandboxLifecycle,
    SandboxNetwork,
    SandboxNetworkProxy,
    SandboxNetworkProxyRoute,
    SandboxPort,
    SandboxPortProtocol,
    SandboxStatus,
)
from baseten.sandbox._process import (
    AsyncSandboxProcess,
    SandboxProcess,
    SandboxProcessExecEvent,
    SandboxProcessExecEventExit,
    SandboxProcessExecEventOutput,
    SandboxProcessInfo,
    SandboxProcessLogLine,
    SandboxProcessLogs,
    SandboxProcessStatus,
)
from baseten.sandbox._retry import SandboxRetryOptions
from baseten.sandbox._sandbox import AsyncSandbox, Sandbox

__all__ = [
    "AsyncImageClient",
    "AsyncImageIgnoreFileFunc",
    "AsyncImageIgnoreFileProcessor",
    "AsyncSandbox",
    "AsyncSandboxClient",
    "AsyncSandboxCreateResult",
    "AsyncSandboxFileSystem",
    "AsyncSandboxProcess",
    "AsyncSandboxStream",
    "AsyncSandboxTokenProvider",
    "ImageBuildError",
    "ImageBuilder",
    "ImageCleanupResult",
    "ImageClient",
    "ImageIgnoreFileFunc",
    "ImageIgnoreFileOptions",
    "ImageIgnoreFileProcessor",
    "ImageIgnoreFileProcessorOptions",
    "ImageInfo",
    "ImageLibraryInfo",
    "ImageLibraryVolume",
    "ImageLogLine",
    "ImageSort",
    "ImageStatus",
    "ImageTagInfo",
    "ImageTagSort",
    "ImageUploadError",
    "Sandbox",
    "SandboxAPIError",
    "SandboxClient",
    "SandboxClientOptions",
    "SandboxCreateResult",
    "SandboxEnvValue",
    "SandboxError",
    "SandboxExpirationAction",
    "SandboxExpirationPolicy",
    "SandboxExpirationPolicyDate",
    "SandboxExpirationPolicyIdle",
    "SandboxExpirationPolicyMaxAge",
    "SandboxExpirationPolicyUnknown",
    "SandboxFileSystem",
    "SandboxFileSystemCopyError",
    "SandboxFileSystemDirectory",
    "SandboxFileSystemEntryType",
    "SandboxFileSystemFileInfo",
    "SandboxFileSystemFindMatch",
    "SandboxFileSystemFindResult",
    "SandboxFileSystemGrepMatch",
    "SandboxFileSystemGrepResult",
    "SandboxFileSystemSubdirectoryInfo",
    "SandboxFileSystemWatchEvent",
    "SandboxFileSystemWatchOp",
    "SandboxGatewayError",
    "SandboxInfo",
    "SandboxLifecycle",
    "SandboxNetwork",
    "SandboxNetworkProxy",
    "SandboxNetworkProxyRoute",
    "SandboxPort",
    "SandboxPortProtocol",
    "SandboxProcess",
    "SandboxProcessExecEvent",
    "SandboxProcessExecEventExit",
    "SandboxProcessExecEventOutput",
    "SandboxProcessInfo",
    "SandboxProcessLogLine",
    "SandboxProcessLogs",
    "SandboxProcessStatus",
    "SandboxProcessWaitTimeoutError",
    "SandboxRetryOptions",
    "SandboxStatus",
    "SandboxStream",
    "SandboxTokenProvider",
    "SandboxTokenProviderContext",
    "default_image_ignore_file",
]
