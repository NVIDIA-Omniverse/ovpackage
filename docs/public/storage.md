# Supported Storage Systems

ovpackage supports multiple storage systems. This section covers the supported URL formats,
configuration options, and authentication methods for each.

## Supported Storage and URL Formats

ovpackage supports read-only HTTP/HTTPS resources, Nucleus servers, S3 buckets, Azure containers/blobs, Google Cloud Storage buckets, Hugging Face dataset repositories, Storage API services, and the local file system:

**HTTP/HTTPS**
    Generic `http://` and `https://` storage addresses are handled by
    ovpackage's read-only Python HTTP backend. It supports stat, reads, redirects,
    signed URLs, and downloads without the Nucleus client library. Package
    creation and installation reject HTTP destinations before scanning sources
    or inspecting destination files, including installation dry runs. Install
    the HTTP backend with::

        pip install ovpackage[http]

    If the Python HTTP backend is unavailable and the Nucleus client library is
    installed, ovpackage retains clientlib as a compatibility fallback for HTTP(S)
    addresses.
    Configured Storage API services get priority for HTTP(S) addresses. A service
    awaiting connection may claim the first operation to perform discovery with
    the command's authentication credentials. Once its scope is known, addresses
    outside that scope fall through to the HTTP or cloud provider backend.
    HTTP directory listing and the client library's proprietary
    `/.cloudfront.toml` discovery mechanism are not supported.

**Hugging Face**
    Hugging Face dataset repository URLs use the `hf://datasets/<owner>/<repo>`
    form. ovpackage can list, stat, read, install from, and mirror from these
    repositories when the `huggingface_hub` extra is installed. Hugging Face
    storage is read-only for ovpackage, so package creation, import, install
    destinations, and mirror destinations are rejected before writes begin.
    Install this backend with::

        pip install ovpackage[huggingface]

**S3**
    Writable S3 access uses `s3://...` URLs through boto3. Recognized S3
    `https://` URLs also use the S3 backend when it is installed; without the
    S3 extra they fall back to the read-only HTTP backend.
    For details on authentication, please refer to the S3 authentication section.

> WARNING:
>
>    Plain HTTP is insecure and not recommended for public S3/CDN objects; use HTTPS.
>    HTTP may be used only for local/test endpoints (e.g., MinIO at `http://localhost:9000`).
>
> ovpackage is primarily tested with S3 general purpose buckets without any additional features enabled.
>
> For reproducible package reads, enable bucket versioning before writing and
> cataloging content. See cloud etag read compatibility for ETag-based
> read failures in unversioned storage and operations that can trigger them.
>
> To utilize AWS Transfer Acceleration, the environment variable OVPACKAGE_ENABLE_S3_ACCELERATION must be set to true. This is a global switch,
> and all buckets accessed must have AWS Transfer Acceleration enabled in their settings. Additional costs may be incurred with
> the AWS account; therefore, testing is recommended before activation for large payloads.
>
> To fine-tune S3 multipart transfer performance, the following environment variables can be configured.
> They apply to uploads and to multipart downloads:
>
> * `OVPACKAGE_S3_MULTIPART_THRESHOLD`: Minimum file size (in bytes) before multipart transfers are used. Default is 5 MiB (`5242880`).
> * `OVPACKAGE_S3_MULTIPART_CHUNKSIZE`: Size (in bytes) of each part/chunk in a multipart transfer. Default is 5 MiB (`5242880`).
> * `OVPACKAGE_S3_MAX_CONCURRENCY`: Maximum number of concurrent chunk transfers per file. Default is `50`.
>
> These limits act per file; total transfer parallelism is additionally bounded by the global scheduler
> limits (`--jobs`, `--file-transfer-jobs`); see generic parameters.
>
> Example usage:
>
> ```bash
>
> export OVPACKAGE_S3_MULTIPART_THRESHOLD=10485760   # 10 MiB
> export OVPACKAGE_S3_MULTIPART_CHUNKSIZE=10485760   # 10 MiB
> export OVPACKAGE_S3_MAX_CONCURRENCY=32
>
> ```
> Timeout and retry behavior can be tuned with:
>
> * `OVPACKAGE_S3_CONNECT_TIMEOUT_S`: Seconds to wait for a connection to be established. Default is `5`.
> * `OVPACKAGE_S3_READ_TIMEOUT_S`: Seconds of socket-read inactivity before a response or download stream
>   is considered stalled and retried. Default is `60`.
> * `OVPACKAGE_S3_EXTRA_RETRIES`: Number of attempts ovpackage makes when a transfer fails mid-stream; for
>   multipart downloads this applies per chunk. Default is `3`.
> * `OVPACKAGE_S3_RETRY_WAIT_SECONDS`: Base delay in seconds for the exponential backoff between attempts. Default is `1`.
>
> Standard AWS request retries can additionally be tuned with `AWS_RETRY_MODE` and `AWS_MAX_ATTEMPTS`
> (or `retry_mode` / `max_attempts` in `~/.aws/config`); ovpackage passes these through unchanged.
>
**Azure**
    Writable Azure access uses `azure://<account>/<container>/...` URLs.
    Recognized Azure `https://` URLs use the Azure backend when it is
    installed and otherwise fall back to the read-only HTTP backend.
    For details on authentication, please refer to the Azure authentication section.

    Enable blob versioning before writing and cataloging package content.
    Metadata and HTTP-property updates can invalidate ETag-only identities;
    see cloud etag read compatibility.

**Google Cloud Storage**
    Writable Google Cloud Storage access uses `gs://bucket/...` URLs.
    `https://storage.googleapis.com/...` URLs use the GCS backend when it is
    installed and otherwise fall back to the read-only HTTP backend.
    ovpackage uses the `google-cloud-storage` library to access GCS. Install
    the required dependency with::

        pip install ovpackage[gcs]

    For details on authentication, please refer to the GCS authentication section.

**Hugging Face (read-only)**
    Data in Hugging Face dataset repositories can be accessed using
    `hf://datasets/<owner>/<repo>[@<revision>]/...` URLs. ovpackage uses the
    `huggingface_hub` library and currently supports dataset repositories
    only. Install the required dependency with:

```bash

    pip install ovpackage[huggingface]

Hugging Face support is **read-only**: packages can be listed, mirrored, and
installed *from* Hugging Face dataset repositories, but ovpackage does not write
to them. Using a Hugging Face URL as the target of `create`, `install`,
`mirror`, or `import` fails with an error. For example, to mirror a
package from a Hugging Face dataset repository to a local directory:

.. code-block:: bash

    ovpackage mirror my_package v1 --source-repo hf://datasets/my-org/my-dataset \
        --destination-repo ./local_repo --auth hf://datasets/my-org/my-dataset,$HF_TOKEN

For details on authentication, please refer to the Hugging Face authentication section.

**Download cache**

ovpackage reads and downloads Hugging Face files through `huggingface_hub` and
uses its persistent Hub cache, normally `~/.cache/huggingface/hub`. Set
`HF_HOME` to relocate Hugging Face data or `HF_HUB_CACHE` to select the
Hub cache directory. Cached downloads remain after the command finishes;
ovpackage does not impose a size limit or automatically remove old revisions.
Monitor disk usage and clean unused entries with Hugging Face's cache tools.
See the `Hugging Face cache-management guide
<https://huggingface.co/docs/huggingface_hub/main/en/guides/manage-cache>`_
for inspection and cleanup instructions.

```
**Local file system**
    Data on the local file system can be accessed using `file://localhost/....` or `file:///...` URLs.
    Any path without a scheme is interpreted as a file path, so you can simply specify `local_folder`.
    ovpackage also accepts `file:local_folder` and treats it as a local directory.

**Storage API**
    ovpackage supports any storage service implementing the [Storage API](the Storage API specification) specification. The Storage API provides a unified
    interface (gRPC and REST/OpenAPI) that connects to various backend storage solutions.

    The Storage API backend requires `grpcio>=1.73.1` and `protobuf>=6.31.0`,
    with no upper version bounds. `grpcio-tools` is a build-time compiler,
    not a runtime dependency.

    The Storage API backend is **enabled by default** but stays inert until either
    `OVPACKAGE_STORAGE_API_DISCOVERY_URL` or `OVPACKAGE_STORAGE_API_GRPC_ADDRESS` is set;
    when neither is configured `ovpackage version` reports `ENABLED but not configured`
    and the Router falls through to the other backends. Set `OVPACKAGE_DISABLE_STORAGE_API=True`
    to opt out entirely. The legacy `OVPACKAGE_ENABLE_STORAGE_API=False` knob is still
    honored for backward compatibility.

    **Content Hashes via Metadata API**

    When the Storage API server supports the optional Metadata service, ovpackage automatically
    stores and retrieves file content hashes (nucleus hashes) as server-side metadata. This
    enables efficient integrity verification — for example when using `catalog`, `check`,
    or `install` — without re-downloading files to compute hashes locally.

    No user configuration is required. ovpackage negotiates Metadata API availability during
    connection and uses it transparently. Hashes are stored on `write` and `copy`
    operations and retrieved on `stat`.

    When the Metadata service is not available on the server, ovpackage continues to operate
    normally but cannot provide server-side content hashes for Storage API files. In that
    case, `catalog --local-hash` can still be used to compute hashes by downloading files.

    **Service discovery**

    ovpackage discovers the gRPC endpoint of a Storage API service through a lightweight REST-based
    discovery protocol. Set `OVPACKAGE_STORAGE_API_DISCOVERY_URL` to the base URL of the discovery
    service (e.g. `https://my-discovery.example.com`). During initialization ovpackage will:

    1. Call `GET <discovery_url>/api/v1/services` (authenticating if necessary).
    2. Look for an entry with `"type": "storage"` and read its `"grpc"` field.
    3. Connect to the returned gRPC endpoint.

    If you already know the gRPC address, you can bypass discovery entirely by setting
    `OVPACKAGE_STORAGE_API_GRPC_ADDRESS` (e.g. `my-storage-host:443`).

    **URL routing**

    After connecting, ovpackage needs to know which URLs the Storage API server is responsible for.
    The Router asks the server in this order:

    1. **ListRoutes** — If the Capabilities service exposes a `v1alpha` API, ovpackage calls
       `ListRoutes` and receives wildcard patterns (e.g. `https://storage.example.com/**`).
       ovpackage compiles these patterns into regexes for precise URL matching. This is the
       preferred mechanism and can be disabled with `OVPACKAGE_DISABLE_LIST_ROUTES=True`.
    2. **ListTopLevelAddresses** — Fallback when `ListRoutes` is unavailable or returns
       no patterns. Returns a list of URL prefixes that the server handles. URL matching
       is a simple prefix check.
    3. **Pre-connection permissive mode** — If the backend has not yet connected to the
       server (e.g. because authentication was not yet configured), `is_url_in_scope()`
       returns `True` for any URL, allowing the lazy initialization path to trigger on
       the first real operation. Once connected, unknown URLs are *rejected* so that
       they can be handled by other backends (S3, Azure, GCS, Hugging Face, etc.).

    **Routing**

    URLs are automatically routed to the backend that supports them
    natively.

    When the Storage API is enabled, it fetches its routing table
    from the server on connect and takes precedence for any URLs it
    claims.  If the Storage API serves URLs that overlap with another
    enabled backend (e.g. `s3://` URLs), set `routes` in
    `ovpackage.toml` so those URLs are routed to the Storage API from
    the start -- otherwise they may go to the native backend while
    the Storage API connection is still being established.

    Generic `http://` / `https://` URLs that no other backend
    claims are handled by the read-only HTTP backend, with the Nucleus
    client library as a compatibility fallback when HTTP support is unavailable.

    **Multiple Storage API services**

    ovpackage can connect to more than one Storage API service at the same
    time. The service named `default` is the one configured by the
    `OVPACKAGE_STORAGE_API_*` environment variables; every other
    `[storage_api.<name>]` table in `ovpackage.toml` becomes an additional
    service connected alongside it. Each service owns the URLs in its
    `routes` scope, so a URL in service A's scope routes to A and a URL
    in service B's scope routes to B -- which also makes it possible to
    `mirror` (copy) a package from a repo on one service to a repo on
    another:

```bash

    ovpackage mirror \
        --source-repo https://east.content.example.com/my-repo \
        --destination-repo https://west.content.example.com/my-repo \
        my-package 1.0.0

If two services' scopes overlap (both claim the same URL), ovpackage logs a
warning at startup naming the conflict and routes the overlapping URL to
the first-configured service; give each service a **disjoint** `routes`
list in `ovpackage.toml` to resolve it.

**ovpackage.toml configuration**

Storage API settings -- including the `routes` mentioned above --
can be persisted in an optional TOML configuration file. A named
(non-`default`) service must set `discovery_url` (there is no
environment-variable fallback for it).

.. code-block:: toml

    [storage_api.default]
    # Persistent default discovery URL (overridden by
    # OVPACKAGE_STORAGE_API_DISCOVERY_URL when that env var is set).
    discovery_url = "https://storage-api.example.com"

    # Override the route table advertised by the server.
    routes = [
        "omniverse://content.example.com/**",
        "https://content.example.com/**",
    ]

    # A second service, connected at the same time. Its `routes` must
    # not overlap another service's scope.
    [storage_api.staging]
    discovery_url = "https://storage-api.staging.example.com"
    routes = ["https://staging.content.example.com/**"]

ovpackage reads `OVPACKAGE_CONFIG_FILE` if set, otherwise
`~/.nvidia-omniverse/config/ovpackage.toml` (if it exists).

**Initialization lifecycle**

During `ovpackage.initialize()` (or `ContextManager.__aenter__`):

1. `Router.initialize()` creates all enabled backend objects, including `StorageAPIStorageGRPC`.
2. `WrappEventLoopThread.initialize()` starts the async event loop.
3. **Eager initialization** — ovpackage immediately calls `eager_initialize()` on each
   configured Storage API service. This runs discovery, connects the gRPC channel,
   negotiates protocol versions, and populates routes/TLAs, then warns about any
   overlapping route scopes between services. Per-service failures (e.g. the server is
   unreachable or credentials are missing) are isolated: they log a warning and fall
   back to lazy initialization for that service without affecting the others.
4. **Lazy initialization** — If eager init was skipped, the first storage operation on a
   Storage API URL triggers `_ensure_grpc_stubs()`, which performs the same discovery,
   connect, negotiate, and route-fetch sequence on demand.

**Protocol negotiation**

The Storage API proto surface has two version tracks: `v1beta` (stable) and `v1alpha`
(experimental). After connecting, ovpackage calls `ListServices` to discover which versions the
server supports for each service (Capabilities, FileObject, FileFolder, Metadata). By default,
ovpackage prefers `v1beta` for `capabilities`/`filefolder` and `v1alpha` for `fileobject`,
then falls back based on service-specific availability.

**Environment variables**

The following environment variables configure the Storage API backend:

.. list-table::
   :header-rows: 1
   :widths: 35 10 55

   * - Variable
     - Default
     - Description
   * - `OVPACKAGE_DISABLE_STORAGE_API`
     - `False`
     - Set to `True` to disable the Storage API backend entirely.
   * - `OVPACKAGE_ENABLE_STORAGE_API`
     - `True`
     - Legacy knob; set to `False` to disable. New configurations should use `OVPACKAGE_DISABLE_STORAGE_API` instead.
   * - `OVPACKAGE_STORAGE_API_DISCOVERY_URL`
     - *(empty)*
     - Base URL of the discovery service (e.g. `https://my-discovery.example.com`). Setting this activates the backend.
   * - `OVPACKAGE_STORAGE_API_GRPC_ADDRESS`
     - *(empty)*
     - Direct gRPC endpoint. Used when discovery is not configured. Setting this activates the backend.
   * - `OVPACKAGE_STORAGE_API_CAPABILITIES`
     - *(empty)*
     - Pin capabilities service version (`v1alpha` or `v1beta`). If pinned and unavailable, ovpackage fails fast.
   * - `OVPACKAGE_STORAGE_API_FILEOBJECT`
     - *(empty)*
     - Pin fileobject service version (`v1alpha` or `v1beta`). Default selection prefers `v1alpha`.
   * - `OVPACKAGE_STORAGE_API_FILEFOLDER`
     - *(empty)*
     - Pin filefolder service version (`v1alpha` or `v1beta`). Default selection prefers `v1beta`.
   * - `OVPACKAGE_STORAGE_API_METADATA`
     - *(empty)*
     - Pin metadata service version (currently `v1alpha`). If pinned and unavailable, ovpackage fails fast.
   * - `OVPACKAGE_DISABLE_LIST_ROUTES`
     - `False`
     - Set to `True` to skip `ListRoutes` and always use `ListTopLevelAddresses`.
   * - `OVPACKAGE_STORAGE_API_DISABLE_ENUMERATE`
     - `False`
     - Set to `True` to skip `Enumerate` and use recursive `ListStat` traversal.
   * - `OVPACKAGE_STORAGE_API_DISABLE_METADATA`
     - `False`
     - Set to `True` to skip Metadata service wiring and best-effort metadata reads/writes even if advertised.
   * - `OVPACKAGE_STORAGE_API_UPLOAD_RETRIES`
     - ``3`[
     - Maximum attempts for Storage API uploads, including gRPC write streams and redirect-based uploads. Values]( 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_DOWNLOAD_RETRIES`
     - `3`
     - Maximum attempts for redirect-based downloads. Values < 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_READ_RETRIES`
     - `3`
     - Maximum attempts for Storage API read-side gRPC calls, including route discovery, `Stat`, `Enumerate`/`ListStat` listing streams, and streamed reads/downloads. Values < 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_METADATA_RETRIES`
     - `3`
     - Maximum attempts for Storage API metadata gRPC calls. Values < 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_ENABLE_RANGE_DOWNLOAD`
     - `True`
     - Enable opportunistic ranged download for redirect responses. When unsupported by the target, ovpackage falls back to single-stream download.
   * - `OVPACKAGE_STORAGE_API_DOWNLOAD_RANGE_PART_SIZE`
     - `8388608`
     - Target byte range size for redirect range downloads (8 MiB by default). Values < 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_DOWNLOAD_RANGE_MAX_CONCURRENCY`
     - `8`
     - Maximum per-file concurrent range requests for redirect downloads. Values < 1 are clamped to 1.
   * - `OVPACKAGE_STORAGE_API_AUTH_CLIENT_NAME`
     - *(empty)*
     - Override the OAuth client profile name used in `/api/v1/auth-config`.
   * - `OVPACKAGE_STORAGE_API_KEYRING_SERVICE`
     - `ovpackage.storageapi.discovery`
     - Keyring service name for storing refresh tokens.

ovpackage also accepts legacy aliases without the extra underscore after `STORAGE`
(for example `OVPACKAGE_STORAGEAPI_UPLOAD_RETRIES` and
`OVPACKAGE_STORAGEAPI_DOWNLOAD_RETRIES`), but those names are deprecated.

```

## S3 and Azure Identity Reads and ETag Changes

**Read compatibility change:** ovpackage now enforces the ETag recorded in S3 and
Azure `resource_identity_v2` during reads, downloads, and source copies.
S3 requests use `IfMatch` or `CopySourceIfMatch`; Azure requests use the
corresponding ETag match conditions. If the recorded ETag no longer matches
the selected object, ovpackage fails the operation instead of accepting the
current object or falling back to its source address. This affects install,
export, freeze, mirror, and identity reads used for catalog or build-manifest
hashing.

An ETag-only identity has no immutable version to select. Changing the ETag
at the same address therefore invalidates that identity, **even if the file
bytes are unchanged**. Known examples include:

* **S3 multipart re-upload with a different part size.** A multipart ETag
  depends on the parts, so re-uploading identical bytes with different part
  boundaries can produce a different ETag.
* **S3 metadata replacement through an in-place** `CopyObject`.
  A single-request copy of a multipart-uploaded object, within the
  `CopyObject` size limit, creates a single-part object and replaces its
  multipart ETag with a single-part ETag. The metadata update can thus
  invalidate an ETag-only catalog entry without changing its file bytes.
  See the [AWS object integrity documentation
 ](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).
* **Azure blob metadata or HTTP-property updates.** `Set Blob Metadata`
  and `Set Blob Properties` change the blob's ETag, including updates to
  properties such as `Content-Type` or `Cache-Control`. The file bytes
  do not have to change. See the [Azure concurrency documentation
 ](https://learn.microsoft.com/en-us/azure/storage/blobs/concurrency-manage)
  and [blob properties and metadata documentation
 ](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-properties-metadata).

**Recommended setup:** enable S3 bucket versioning and Azure blob versioning
before writing and cataloging package content, so ovpackage can record immutable
native version IDs alongside ETags. Later replacements then create new
versions while the catalog continues to select the original version and its
ETag. Retain referenced versions for as long as their packages must remain
consumable; lifecycle policies or explicit deletion must not remove them.
Enabling versioning later does not upgrade existing ETag-only catalog
identities. S3 consumers also need s3 consumer permissions to read
recorded versions.

## Authentication

### Storage API Authentication

**Quick start**

```bash

# 1. Point at your discovery service (this activates the backend;
#    no separate enable knob is required).
export OVPACKAGE_STORAGE_API_DISCOVERY_URL=https://my-discovery.example.com

# 2a. Interactive login (opens browser)
ovpackage list-repo file-storage-id://repo --interactive-auth

# 2b. Bearer token (CI / scripted)
ovpackage list-repo file-storage-id://repo \
    --auth https://my-discovery.example.com,<TOKEN>

# 2c. Client-credentials (machine-to-machine)
ovpackage list-repo file-storage-id://repo \
    --auth https://my-discovery.example.com,<CLIENT_ID>,<CLIENT_SECRET>

```
After a successful interactive login the refresh token is stored in the system
keyring, so subsequent runs do not require `--interactive-auth`.

For Storage API discovery mode, set `OVPACKAGE_STORAGE_API_DISCOVERY_URL` to the discovery endpoint.
ovpackage will call `/api/v1/services` and, if required, authenticate against the discovery service.

See the *Service discovery* section above for the full discovery sequence.

Interactive authentication can be enabled using `--interactive-auth`. In this mode, ovpackage starts
an OpenID browser flow and listens on `http://127.0.0.1:35101/openid` for the callback.

It is also possible to provide a bearer token explicitly with `--auth` (or `OVPACKAGE_AUTH`) using
the format `server_url,bearer_token` where `server_url` must match the discovery server URL:

```bash

ovpackage list-repo file-storage-id://repo --auth https://my-discovery.example.com,<TOKEN>

```
For non-interactive machine-to-machine authentication (CI/CD, service accounts), use the OAuth2
client credentials flow by providing a `client_id` and `client_secret`:

```bash

ovpackage list-repo file-storage-id://repo --auth https://my-discovery.example.com,<CLIENT_ID>,<CLIENT_SECRET>

```
ovpackage will use these credentials to obtain an access token from the discovery service's token
endpoint using `grant_type=client_credentials`. The token is automatically refreshed before
expiry using the same credentials.

To select a specific OAuth client profile from discovery `/api/v1/auth-config`, use
`OVPACKAGE_STORAGE_API_AUTH_CLIENT_NAME`. If not set, ovpackage falls back to
`OMNI_STORAGE_CLIENT_NAME`, then `client_library`, then `ovpackage`.

ovpackage stores refresh tokens in the system keyring and reuses them on subsequent runs.
The keyring service name can be overridden with `OVPACKAGE_STORAGE_API_KEYRING_SERVICE`
(default: `ovpackage.storageapi.discovery`); the username under which each refresh token
is stored is the normalised discovery URL (the value of
`OVPACKAGE_STORAGE_API_DISCOVERY_URL` with any trailing slash stripped, e.g.
`https://my-discovery.example.com`). On every successful refresh exchange ovpackage writes
the rotated refresh token back to the same entry, so a stash made out-of-band stays
current as long as ovpackage keeps using it.

Authentication source precedence is:

1. manual bearer token from `--auth` or `OVPACKAGE_AUTH`
2. client credentials from `--auth` or `OVPACKAGE_AUTH` (client_id + client_secret)
3. refresh token from keyring
4. interactive auth flow (when `--interactive-auth` is enabled)

If keyring access fails (for example on a host without a keyring backend), ovpackage logs
a warning and continues without persisted-token reuse.

When `--interactive-auth` is not enabled and authentication is required, ovpackage fails with guidance
to either provide `--auth`/`OVPACKAGE_AUTH` or enable interactive authentication.

The discovery server must expose OAuth client configuration for one of the configured profile names
in `/api/v1/auth-config`.

### Azure Authentication

ovpackage accesses Azure Blob Storage using the `azure-storage-blob` library. Authentication is done through
the standard Azure SDK mechanisms. For details, please refer to the
[Azure Storage authentication documentation](https://learn.microsoft.com/en-us/azure/storage/common/storage-auth).

It is also possible to provide credentials for Azure through the CLI `--auth` parameter or the
`OVPACKAGE_AUTH` environment variable using the format `server_url,connection_string`:

```bash

ovpackage list-repo https://myaccount.blob.core.windows.net/mycontainer/ --auth "https://myaccount.blob.core.windows.net,DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=...;EndpointSuffix=core.windows.net"

```
For real credentials, store the complete connection string in an environment variable
and expand it using the syntax appropriate for your shell rather than placing the
credential directly in shell history.

#### Azure Transfer Tuning

The Azure backend exposes Azure SDK transfer settings for both uploads and downloads:

* `OVPACKAGE_AZURE_UPLOAD_MAX_CONCURRENCY` (default `1`)
* `OVPACKAGE_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE` (default `67108864` bytes, 64 MiB)
* `OVPACKAGE_AZURE_UPLOAD_MAX_BLOCK_SIZE` (default `4194304` bytes, 4 MiB)
* `OVPACKAGE_AZURE_DOWNLOAD_MAX_CONCURRENCY` (default `1`)
* `OVPACKAGE_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE` (default `33554432` bytes, 32 MiB)
* `OVPACKAGE_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE` (default `4194304` bytes, 4 MiB)

Values must be positive integers. Invalid or non-positive values are logged and clamped to a safe fallback.

### S3 Authentication

When ovpackage directly accesses S3 via boto3, authentication and other configuration is done through
the standard boto3 mechanisms. For details, please refer to the
[boto3 documentation for credentials](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html),
and configuration via [environment variables](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/configuration.html#using-environment-variables)
and [config files](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/configuration.html#using-a-configuration-file).

It is also possible to provide credentials for S3 through the CLI `--auth` parameter or the
`OVPACKAGE_AUTH` environment variable - please see the command line help for details.

#### S3 Consumer Permissions

Catalogs now record S3 object versions in `resource_identity_v2` when S3
returns a `VersionId`. This includes objects in buckets where versioning was
previously enabled and has since been suspended. Consumers of these catalogs
need **the new permission** `s3:GetObjectVersion` on the source objects, in
addition to the usual `s3:GetObject` for current-version reads and metadata
and `s3:ListBucket` for bucket listings.

ovpackage sends the recorded version on `HeadObject`, `GetObject`, and the
source of `CopyObject` requests. This applies to install, export, freeze,
mirror, and identity downloads used for catalog or build-manifest hashing.
`s3:GetObject` alone does not authorize reads selected by `versionId`;
see the `AWS GetObject permissions documentation
<https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html>`_.
ETag-only identities without `versionId` continue to use current-version
read permissions.

For example, a consumer role reading from `my-bucket` can use this policy
(replace the bucket name and restrict object prefixes as appropriate):

```json

{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-bucket"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::my-bucket/*"
    }
  ]
}

```
Copying to S3 also requires the existing destination write permissions,
including `s3:PutObject`. Encryption and explicit policy denies can impose
additional restrictions.

Catalog and check operations that only list objects or inspect current-version
metadata can succeed even when a later identity read is denied. Hashing that
reads identified bytes can expose the missing permission during catalog or
build-manifest instead. Successful current-version metadata checks therefore
do not establish that a role can consume the recorded versions. On a
version-qualified access-denied error, ovpackage names `s3:GetObjectVersion` as
a permission to check; an HTTP 403 does not identify the exact policy or
encryption restriction that caused the denial.

### GCS Authentication

ovpackage accesses Google Cloud Storage using the `google-cloud-storage` library (installed via the
`[gcs]` extra).

#### Credential Resolution Order

ovpackage resolves GCS credentials in the following order.  The first source that provides valid
credentials wins; later sources are not consulted.

1. **Explicit credentials via** `--auth` **or** `OVPACKAGE_AUTH`:
   The format is `server_url,credentials_file_or_json`.  The second value can be a path to a
   service-account JSON key file or an inline JSON string.  Omit the second value to fall back to
   later sources::

       ovpackage list-repo gs://my-bucket --auth gs://my-bucket,/path/to/service-account.json

2. **ovpackage-specific environment variables**:

   * `OVPACKAGE_GCS_CREDENTIALS_FILE`: Path to a service-account JSON key file.
   * `OVPACKAGE_GCS_PROJECT`: Google Cloud project ID (optional, used alongside credentials).

   If `OVPACKAGE_GCS_CREDENTIALS_FILE` is set, ovpackage uses that file directly and does not probe
   Application Default Credentials.

3. **Google Application Default Credentials (ADC)**:
   If no explicit credentials are provided, ovpackage calls
   [google.auth.default()](https://cloud.google.com/docs/authentication/application-default-credentials),
   which checks the following sub-sources in order:

   a. `GOOGLE_APPLICATION_CREDENTIALS` environment variable — path to any supported credential
      file (service-account key, external-account config, etc.).
   b. **gcloud CLI default credentials** — created by running
      `gcloud auth application-default login`, stored in
      `~/.config/gcloud/application_default_credentials.json` (Linux/macOS) or
      `%APPDATA%\gcloud\application_default_credentials.json` (Windows).
   c. **GCE metadata server** — available on Compute Engine, Cloud Run, GKE, etc.
      Disabled by default in ovpackage (see no gce check below).

4. **Anonymous credentials (fallback)**:
   If none of the above produce credentials, ovpackage falls back to anonymous access and logs a
   warning.  Operations against private buckets will fail with a permission error.

#### Using Lesser-Privileged Credentials

If your default credentials (e.g. from `gcloud auth application-default login`) have broad
permissions and you want to run a specific ovpackage command with reduced privileges, you can
override credentials for a single invocation.  Because the resolution order above is
strictly prioritised, any higher-priority source overrides lower ones:

```bash

# Override with a read-only service account key via --auth (highest priority)
ovpackage list-repo gs://my-bucket --auth gs://my-bucket,/path/to/readonly-sa.json

# Or point GOOGLE_APPLICATION_CREDENTIALS to a restricted key for one command
GOOGLE_APPLICATION_CREDENTIALS=/path/to/readonly-sa.json ovpackage list-repo gs://my-bucket

# Or use the ovpackage-specific env var
OVPACKAGE_GCS_CREDENTIALS_FILE=/path/to/readonly-sa.json ovpackage list-repo gs://my-bucket

```
#### GCE Metadata Server and `NO_GCE_CHECK`

As the last step of Application Default Credentials discovery (step 3c), the Google auth library
attempts to contact the GCE metadata server at `http://metadata.google.internal/`.  On machines
that are **not** running on Google Compute Engine this request hangs for several seconds before
timing out.

To avoid this, ovpackage sets `NO_GCE_CHECK=true` by default.  This skips only the metadata server
probe; all other credential sources (steps 1–3b) work normally.

* **Running on Compute Engine / GKE / Cloud Run?**  Set `NO_GCE_CHECK=false` in your
  environment so that metadata-based credentials are discovered.
* **Already set in your environment?**  ovpackage does not overwrite an existing `NO_GCE_CHECK`
  value — your setting is preserved.
* **GCS modules are already imported before you use ovpackage?** Set `NO_GCE_CHECK=true` to
  avoid the metadata server probe before importing any GCS module.

#### Troubleshooting

**"No GCS credentials found … Using anonymous credentials"**
   No credentials were discovered at any level.  The most common fix is to run::

       gcloud auth application-default login

   or set `OVPACKAGE_GCS_CREDENTIALS_FILE` to point to a service-account key file.

**"Permission denied" / 403 on a private bucket**
   Credentials were found but lack the required IAM permissions on the target bucket or objects.
   Verify that the authenticated identity has at least `roles/storage.objectViewer` (for reads)
   or `roles/storage.objectAdmin` (for writes).

**Slow startup (~10 s delay before any GCS operation)**
   `NO_GCE_CHECK` may have been explicitly set to `false` while running outside of GCE.
   Remove the variable or set it to `true`::

       export NO_GCE_CHECK=true

#### Additional Environment Variables

* `OVPACKAGE_GCS_ENDPOINT`: Override the GCS API endpoint (useful for local testing with
  [fake-gcs-server](https://github.com/fsouza/fake-gcs-server)).  When set, anonymous
  credentials are used automatically if no other credentials are configured.

GCS operations use the `google-cloud-storage` client's [built-in retry and exponential
back-off](https://cloud.google.com/storage/docs/retry-strategy#python). Each of the following
variables, **when set**, overrides the corresponding `google-cloud-storage` setting (see the
[google.api_core Retry reference](https://googleapis.dev/python/google-api-core/latest/retry.html));
when unset, the library's own default is used. They are
useful for slow or overloaded connections — for example, raising the total deadline avoids
the `RetryError: Timeout ... exceeded` seen on large cross-cloud mirrors.

* `OVPACKAGE_GCS_TIMEOUT_SECONDS`: Total deadline in seconds for a GCS operation across all
  internal retry attempts (the retry deadline).
* `OVPACKAGE_GCS_RETRY_INITIAL_SECONDS`: Delay in seconds before the first retry.
* `OVPACKAGE_GCS_RETRY_MAXIMUM_SECONDS`: Maximum delay in seconds between retries.
* `OVPACKAGE_GCS_RETRY_MULTIPLIER`: Multiplier applied to the delay after each retry (exponential back-off).
* `OVPACKAGE_GCS_REQUEST_TIMEOUT_SECONDS`: Timeout in seconds for a single HTTP request to GCS
  (the per-request transport timeout; the `google-cloud-storage` default is 60 seconds).
  Raise it on connections too slow to complete an individual request within that limit —
  the per-request `TimeoutError: The write operation timed out`. Raising
  `OVPACKAGE_GCS_TIMEOUT_SECONDS` alone does not help there: it only retries the same
  request-timeout-bounded attempt for longer.

### Hugging Face Authentication

ovpackage accesses Hugging Face dataset repositories using the `huggingface_hub`
library (installed via the `[huggingface]` extra). Access is read-only, so a
token only needs read permission for the repositories you use as a source.

#### Hugging Face Credential Resolution

1. **Explicit token via** `--auth` **or** `OVPACKAGE_AUTH`:
   The format is `server_url,token[,endpoint]`. The `server_url` is the
   dataset repository root, for example `hf://datasets/my-org/my-dataset`:

```bash

ovpackage list-repo hf://datasets/my-org/my-dataset --auth hf://datasets/my-org/my-dataset,hf_xxx

```
2. **ovpackage-specific environment variables**:

   * `OVPACKAGE_HUGGINGFACE_TOKEN` or `OVPACKAGE_HF_TOKEN`: Hugging Face access token.
   * `OVPACKAGE_HUGGINGFACE_ENDPOINT` or `OVPACKAGE_HF_ENDPOINT`: Optional custom Hub endpoint.

3. **Standard Hugging Face configuration**:
   If no explicit ovpackage token is provided, ovpackage passes `token=None` to
   `huggingface_hub` so the library can use its normal token discovery,
   including `HF_TOKEN` and locally saved credentials from `hf auth login`.

#### Content Hashes

Hugging Face repository file metadata does not expose ovpackage nucleus hashes.
ovpackage therefore leaves `ListEntry.nucleus_hash` empty for this backend and
resolves the requested branch or tag to a commit SHA. Catalogs keep the
user-provided URL in `source_path` and store the commit-qualified URL in
`resource_identity_v2`. ovpackage downloads and hashes that exact identity, so a
subsequent branch or tag update cannot change the bytes associated with the
catalog entry.

## Storage Semantics and Empty Folders

Different storage systems handle folders differently, which affects how ovpackage operations behave:

**Object storage and repository-prefix storage (S3, Azure Blob Storage, Google Cloud Storage, Hugging Face)**
    Folders are *implicit* — they exist only as prefixes in object keys. When all files in a
    "folder" are deleted, the folder automatically ceases to exist. There is no concept of an
    empty folder.

**File system-based storage (local file system)**
    Folders are *first-class citizens* — they exist independently of the files they contain.
    Empty folders can exist and persist after all files within them are removed.

This distinction has practical implications:

- **Package creation**: ovpackage only captures files, not folders. Empty folders in the source
  are not included in packages. If you need an empty folder in your package, add a placeholder
  file (e.g., `.keep`) to it.

- **Uninstall and delete operations**: On file system-based storage, ovpackage removes files but
  does not automatically remove the folders that contained them. Empty folders may remain and
  need to be cleaned up manually if desired.
