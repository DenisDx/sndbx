FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG APT_MIRROR=http://mirrors.aliyun.com/ubuntu

RUN if [ -n "$APT_MIRROR" ]; then \
        printf "deb %s jammy main restricted universe multiverse\ndeb %s jammy-updates main restricted universe multiverse\ndeb %s jammy-backports main restricted universe multiverse\ndeb %s jammy-security main restricted universe multiverse\n" \
            "$APT_MIRROR" "$APT_MIRROR" "$APT_MIRROR" "$APT_MIRROR" > /etc/apt/sources.list; \
    fi

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        openssh-server \
        ca-certificates \
        curl \
        gnupg \
        git \
        iproute2 \
        socat \
    && rm -rf /var/lib/apt/lists/*

# Node 20+ is required for official MCP servers (ESM/top-level await support).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install MCP server packages (official + community shell variants)
RUN npm install -g @modelcontextprotocol/server-filesystem mcp-shell-server

RUN pip3 install --no-cache-dir mcp-server-git

RUN mkdir -p /run/sshd /root/.ssh /root/shared /opt/sndbx-image \
    && chmod 700 /root/.ssh \
    && touch /root/.ssh/authorized_keys \
    && chmod 600 /root/.ssh/authorized_keys

COPY app.py /opt/sndbx-image/app.py
