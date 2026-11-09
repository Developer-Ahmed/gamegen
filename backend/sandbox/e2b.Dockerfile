FROM e2bdev/code-interpreter:latest

USER root

ARG GODOT_VERSION=4.7.2
ARG GODOT_RELEASE=stable

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --retry 3 \
      "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-${GODOT_RELEASE}/Godot_v${GODOT_VERSION}-${GODOT_RELEASE}_linux.x86_64.zip" \
      --output /tmp/godot.zip \
    && unzip /tmp/godot.zip -d /tmp/godot \
    && install -m 0755 "/tmp/godot/Godot_v${GODOT_VERSION}-${GODOT_RELEASE}_linux.x86_64" /usr/local/bin/godot \
    && rm -rf /tmp/godot /tmp/godot.zip

COPY backend/sandbox/fetch_zip_member.py /tmp/fetch_zip_member.py

RUN python3 /tmp/fetch_zip_member.py \
      "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-${GODOT_RELEASE}/Godot_v${GODOT_VERSION}-${GODOT_RELEASE}_export_templates.tpz" \
      "templates/web_nothreads_release.zip" \
      "/home/user/.local/share/godot/export_templates/${GODOT_VERSION}.${GODOT_RELEASE}/web_nothreads_release.zip" \
    && chown -R user:user /home/user/.local \
    && rm /tmp/fetch_zip_member.py

USER user

RUN godot --headless --version
