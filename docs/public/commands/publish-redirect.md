# Publish-redirect

## Description

The publish-redirect command creates redirect packages in a redirect repository for all packages found in one or more source repositories. Redirect packages contain only `.wrapp` files (no data files) with `source_path` entries pointing to the original source locations. This enables CloudFront/CDN URL redirection while preserving data access to the original storage locations.

Basic usage::

    ovpackage publish-redirect --source-repo omniverse://dev.example.com --redirect-repo omniverse://cdn.example.com

For CLI options, run `ovpackage publish-redirect --help`.

## External URL

When hosting packages on a CDN, use the `--external-url` option to specify the external base URL. This rewrites `source_path` entries to use the CDN URL::

    ovpackage publish-redirect --source-repo omniverse://dev.example.com --redirect-repo omniverse://cdn.example.com --external-url https://cdn.example.com

## Multiple Source Repositories

You can consolidate packages from multiple source repositories into a single redirect repository by specifying `--source-repo` multiple times::

    ovpackage publish-redirect --source-repo omniverse://dev.example.com --source-repo omniverse://staging.example.com --redirect-repo omniverse://cdn.example.com

The command verifies there are no conflicting package versions (same name/version with different content) across source repositories before creating the redirects. Identical duplicates are handled automatically.

## Python API Reference

See the `ovpackage.publish_redirect` function docstring for the full Python API reference (signature, parameters, and error handling).
