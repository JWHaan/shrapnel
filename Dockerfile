# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
# Read the pinned sources as data; the selector does not import or execute them.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && chmod -R a+rX /opt/sqlglot
COPY blast.py selector.py static_map.py fast_impact.py escape_reads.py mapping_impact.py /app/
COPY maps/map.json.gz maps/calls.json.gz maps/escape_reads.json maps/mapping_reads.json /app/maps/
RUN python /app/static_map.py && python /app/fast_impact.py /opt/sqlglot && python -m compileall -q /app && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
