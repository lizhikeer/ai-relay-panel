FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行
RUN useradd -m -u 10001 panel && chown -R panel:panel /app
USER panel

EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/panel-api/system/health',timeout=3).status==200 else 1)"

# 默认只监听回环：面板的唯一入口是 Caddy(:8443)，BFF 无需对局域网暴露，
# 这样少一个无 TLS 的裸入口（最小暴露面）。
# 如确需局域网直连调试，在 .env 里设 BIND_HOST=0.0.0.0 即可，无需重建镜像。
CMD ["sh", "-c", "uvicorn app.main:app --host ${BIND_HOST:-127.0.0.1} --port ${LISTEN_PORT:-18080}"]
