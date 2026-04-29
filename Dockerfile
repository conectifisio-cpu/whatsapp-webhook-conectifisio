FROM python:3.11-slim

# Instalar dependências do sistema para Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar e instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium via Playwright (durante o build, não no startup)
RUN playwright install chromium

# Copiar código
COPY . .

# Cloud Run usa PORT=8080
ENV PORT=8080
EXPOSE 8080

# Iniciar gunicorn (sem playwright install — já foi feito no build)
CMD gunicorn --chdir api whatsapp:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
