# build-server carrier image: a minimal container holding the static binary.
#
# The binary is NOT run from this image in production — the Argo run-agent pod
# uses an initContainer built from this image to copy /build-server onto a shared
# volume, which a stock Haskell toolchain sidecar (e.g. docker.io/haskell:9.10)
# then executes. That keeps the server decoupled from the toolchain image, so the
# toolchain version can be swapped without rebuilding this. busybox provides the
# `cp`/`sh` the initContainer copy needs; the image is also runnable directly
# (ENTRYPOINT) for local testing.
#
#   docker build -f buildserver/Dockerfile -t ghcr.io/<owner>/refactor-build-server:latest buildserver

FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
# Fully static, stripped. No modules to download (stdlib only).
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /build-server .

FROM busybox:1.36-musl
COPY --from=build /build-server /build-server
EXPOSE 8080
ENTRYPOINT ["/build-server"]
