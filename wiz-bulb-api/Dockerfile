FROM python:3.12-alpine

RUN adduser -D -H -u 10001 app && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY app.py ui.html ./

USER app
ENV WIZ_PORT=8077 \
    WIZ_LOG_LEVEL=INFO

EXPOSE 8077

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WIZ_PORT','8077'), timeout=3)" || exit 1

CMD ["python", "app.py"]
