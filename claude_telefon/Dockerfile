ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

# Home Assistant passes the add-on version; the code of the matching release tag is built into the image
ARG BUILD_VERSION
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

WORKDIR /opt/claude-telefon
ADD https://github.com/janlenck/claude-telefon/archive/refs/tags/v${BUILD_VERSION}.tar.gz /tmp/src.tar.gz
RUN tar -xzf /tmp/src.tar.gz --strip-components=1 \
    && rm /tmp/src.tar.gz \
    && pip install -r requirements.txt

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
