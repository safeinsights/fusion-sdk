# Dependency-footprint tripwire (ADR 0003): the package must install and run on a
# slim image with nothing but pip. No compilers, no extras.
FROM python:3.12-slim
WORKDIR /sdk
COPY python/ /sdk/python/
RUN pip install --no-cache-dir --no-deps /sdk/python \
 && python -c "import safeinsights_fusion, sys; print('safeinsights_fusion', safeinsights_fusion.__version__, sys.version)"
COPY docker/smoke-python.sh /sdk/smoke.sh
USER nobody
ENTRYPOINT ["/bin/sh", "/sdk/smoke.sh"]
