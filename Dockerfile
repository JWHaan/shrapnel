# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# sqlglot at the pinned commit, with bytecode compiled once so each run imports fast
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q -j 4 /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot
COPY blast.py selector.py /app/
COPY maps /app/maps
RUN python -m compileall -q /app && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
