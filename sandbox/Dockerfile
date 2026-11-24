# Ephemeral execution environment for remediation commands.
#
# This image exists so remediation never runs on the host or inside the agent
# container. It is intentionally minimal: no credentials baked in, no docker
# socket, no package manager cache.
#
# It is NOT part of the default compose profile. See docs/PROJECT.md ("Sandbox")
# before giving it real cluster reachability.
FROM alpine:3.21

ARG TARGETARCH=amd64

RUN apk add --no-cache bash ca-certificates curl jq openssl postgresql17-client \
    && KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl \
    && adduser -D -u 10001 runner

WORKDIR /work
USER runner

CMD ["echo 'sandbox ready: pass a command, e.g. make sandbox CMD=\"kubectl get pods\"'"]
