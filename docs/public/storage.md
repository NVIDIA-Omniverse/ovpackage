# Supported Storage Systems

ovpackage supports multiple storage systems. This section covers the supported URL formats,
configuration options, and authentication methods for each.

## Supported Storage and URL Formats

ovpackage supports URLs to S3 buckets, Azure containers/blobs, Google Cloud Storage buckets, and the local file system:

**S3**
    Data on S3 can be accessed using `https://...cloudfront.net`, `s3://...` or `https://...amazonaws.com` URLs.
    ovpackage uses boto3 to access S3 when using `s3://...` or `https://...amazonaws.com` URLs.
    `https://...cloudfront.net` URLs are opened via the client-library.
    For details on authentication, please refer to the S3 authentication section.

> WARNING:
>
>    Plain HTTP is insecure and not recommended for public S3/CDN objects; use HTTPS.
>    HTTP may be used only for local/test endpoints (e.g., MinIO at `http://localhost:9000`).
>
> ovpackage is primarily tested with S3 general purpose buckets without any additional features enabled.
>
> To utilize AWS Transfer Acceleration, the environment variable OVPACKAGE_ENABLE_S3_ACCELERATION must be set to true. This is a global switch,
> and all buckets accessed must have AWS Transfer Acceleration enabled in their settings. Additional costs may be incurred with
> the AWS account; therefore, testing is recommended before activation for large payloads.
>
> To fine-tune S3 multipart upload performance, the following environment variables can be configured:
>
> * `OVPACKAGE_S3_MULTIPART_THRESHOLD`: Minimum file size (in bytes) before multipart uploads are used. Default is 5 MiB (`5242880`).
> * `OVPACKAGE_S3_MULTIPART_CHUNKSIZE`: Size (in bytes) of each part/chunk in a multipart upload. Default is 5 MiB (`5242880`).
> * `OVPACKAGE_S3_MAX_CONCURRENCY`: Maximum number of threads/connections used for multipart uploads. Default is `50`.
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
**Azure**
    Data on Azure Blob Storage can be accessed using `https://<account>.blob.core.windows.net/<container>/...` URLs.
    For details on authentication, please refer to the Azure authentication section.

**Google Cloud Storage**
    Data on Google Cloud Storage can be accessed using `gs://bucket/...` or
    `https://storage.googleapis.com/bucket/...` URLs.
    ovpackage uses the `google-cloud-storage` library to access GCS. Install
    the required dependency with::

        pip install ovpackage[gcs]

    For details on authentication, please refer to the GCS authentication section.

**Local file system**
    Data on the local file system can be accessed using `file://localhost/....` or `file:///...` URLs.
    Any path without a scheme is interpreted as a file path, so you can simply specify `local_folder`.
    ovpackage also accepts `file:local_folder` and treats it as a local directory.

## Authentication

### Storage API Authentication

For Storage API discovery mode, set `OVPACKAGE_STORAGE_API_DISCOVERY_URL` to the discovery endpoint.
ovpackage will call `/api/v1/services` and, if required, authenticate against the discovery service.

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
(default: `ovpackage.storageapi.discovery`).

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

ovpackage list-repo https://myaccount.blob.core.windows.net/mycontainer/ --auth https://myaccount.blob.core.windows.net,DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=...

```
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
* `OVPACKAGE_GCS_EXTRA_RETRIES`: Maximum number of retry attempts for upload/download operations
  (default: `3`).
* `OVPACKAGE_GCS_RETRY_WAIT_SECONDS`: Base delay in seconds between retries with exponential
  back-off (default: `1.0`).

## Storage Semantics and Empty Folders

Different storage systems handle folders differently, which affects how ovpackage operations behave:

**Object storage (S3, Azure Blob Storage, Google Cloud Storage)**
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
