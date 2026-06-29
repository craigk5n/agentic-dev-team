# Per-stack coder image: Go.
# Base = the default coder image; adds the Go toolchain so the coder can build/test
# Go in-sandbox once that behavior is added (Story 5.1 / true TDD).
ARG BASE_IMAGE=dev-agents/event-bus:latest
FROM ${BASE_IMAGE}

ARG GO_VERSION=1.22.5
ADD https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz /tmp/go.tgz
RUN tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}"

LABEL dev-agents.coder-stack="go"
