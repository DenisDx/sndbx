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
        openssh-server \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd /root/.ssh /opt/sndbx-image \
    && chmod 700 /root/.ssh \
    && touch /root/.ssh/authorized_keys \
    && chmod 600 /root/.ssh/authorized_keys

COPY app.py /opt/sndbx-image/app.py
