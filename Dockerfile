FROM python:3.11-slim

# Define o diretório de trabalho
WORKDIR /app

# Copia os requisitos e instala os pacotes Python (incluindo o Playwright)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# O PULO DO GATO: Instala o Chromium E todas as dependências nativas do Linux automaticamente
RUN playwright install chromium
RUN playwright install-deps chromium

# Copia o restante do código da Conectifisio
COPY . .

# Configura a porta padrão do Cloud Run
ENV PORT=8080
EXPOSE 8080

# Inicia o servidor com gunicorn
CMD gunicorn --chdir api whatsapp:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
