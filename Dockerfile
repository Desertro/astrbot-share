FROM python:3.12-slim
WORKDIR /AstrBot
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/astrbot-venv PATH="/opt/astrbot-venv/bin:/root/.local/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libssl-dev ca-certificates bash ffmpeg fonts-noto-cjk \
    curl gnupg git ripgrep libgl1 libglib2.0-0 \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY . /AstrBot
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --inexact \
    && uv pip install --python /opt/astrbot-venv/bin/python -r requirements-plugins.txt --override local-dependency-overrides.txt \
    && python -m playwright install --with-deps chromium
EXPOSE 6185 6199
CMD ["python", "main.py"]
