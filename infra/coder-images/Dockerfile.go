# Per-stack image: Go. Used both as the coder sandbox and the CI job container.
# Base = the default coder image (+ git); adds the Go toolchain and node (checkout).
ARG BASE_IMAGE=dev-agents/event-bus:latest
FROM ${BASE_IMAGE}

ARG GO_VERSION=1.22.5
ADD https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz /tmp/go.tgz
RUN tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}"

# Node.js — required when this image is the CI job container (actions/checkout is JS).
ARG NODE_VERSION=20.15.0
ADD https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz /tmp/node.tgz
RUN tar -C /usr/local --strip-components=1 -xzf /tmp/node.tgz && rm /tmp/node.tgz

LABEL dev-agents.coder-stack="go"
