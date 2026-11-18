# the base image is fixed here, build.yaml is deprecated: whatever BUILD_FROM the Supervisor passes is not used;
# python:3.12-slim exists for aarch64 and amd64, Docker picks the right one
FROM python:3.12-slim

# Home Assistant passes the add-on version; the code of the matching release tag is built into the image
ARG BUILD_VERSION
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

WORKDIR /opt/braincall
ADD https://github.com/janlenck/braincall/archive/refs/tags/v${BUILD_VERSION}.tar.gz /tmp/src.tar.gz
RUN tar -xzf /tmp/src.tar.gz --strip-components=1 \
    && rm /tmp/src.tar.gz \
    && pip install -r requirements.txt

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
