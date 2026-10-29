# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# MinIO community images are no longer published. Build the test server from
# RELEASE.2025-10-15T17-29-55Z, pinned to its source commit.
FROM golang:1.26-alpine AS build
RUN CGO_ENABLED=0 go install github.com/minio/minio@9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a

FROM alpine:3.23
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /go/bin/minio /usr/local/bin/minio
ENTRYPOINT ["/usr/local/bin/minio"]
