# Publish-redirect

## Description

The publish-redirect command creates redirect packages in a redirect repository for all packages found in one or more source repositories. Redirect packages contain only `.wrapp` files (no data files) with `source_path` entries pointing to the original source locations. This enables CloudFront/CDN URL redirection while preserving data access to the original storage locations.

`publish-redirect` does not generate SimReady package-standard sidecars for
redirect packages; sidecars are produced by `create`. See
package standard sidecars.

Basic usage:

```bash

ovpackage publish-redirect --source-repo omniverse://dev.example.com --redirect-repo omniverse://cdn.example.com

```
Use `--strip-source` or `--canonical-source <url>` to keep internal source
addresses out of the redirect `.wrapp` files. Because a redirect package's
`repository` field points at the redirect repo and is load-bearing, only the
`source` field is affected here. See source provenance options for details.

For CLI options, run `ovpackage publish-redirect --help`.

## External URL

When hosting packages on a CDN, use the `--external-url` option to specify the external base URL. This rewrites `source_path` entries to use the CDN URL:

```bash

ovpackage publish-redirect --source-repo omniverse://dev.example.com --redirect-repo omniverse://cdn.example.com --external-url https://cdn.example.com

```
Whenever a content address is rewritten, both `resource_identity` and
`resource_identity_v2` are cleared, regardless of the target backend. The
catalog hash, size, and modification time are preserved. Subsequent reads use
the rewritten address and verify the recorded hash when one is available.

Publication does not read or validate the target content, so the CDN or other
content target need not be ready yet. Successful publication is not a guarantee
that the target data exists or matches the catalog.

Content addresses that do not change retain their existing identities and
metadata, including metadata-only redirects without `--external-url`.
Deprecated identities are never promoted to v2 identities.

## Multiple Source Repositories

You can consolidate packages from multiple source repositories into a single redirect repository by specifying `--source-repo` multiple times:

```bash

ovpackage publish-redirect --source-repo omniverse://dev.example.com --source-repo omniverse://staging.example.com --redirect-repo omniverse://cdn.example.com

```
The command verifies there are no conflicting package versions (same name/version with different content) across source repositories before creating the redirects. Identical duplicates are handled automatically.

## Python API Reference

See the `ovpackage.publish_redirect` function docstring for the full Python API reference (signature, parameters, and error handling).
