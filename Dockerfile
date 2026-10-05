FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /srv

# Standard-library only: no requirements installation needed.
COPY app ./app
COPY tests ./tests
COPY smoke.py verify.sh ./
RUN chmod +x verify.sh && python3 -m compileall -q app

EXPOSE 8000

HEALTHCHECK --interval=3s --timeout=3s --start-period=2s --retries=10 \
    CMD python3 -c "import json,os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=2).read()" || exit 1

CMD ["python3", "-m", "app.main"]
