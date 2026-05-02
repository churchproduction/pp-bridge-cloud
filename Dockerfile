FROM python:3.11-slim

# Install LibreOffice headless and tools needed for PowerPoint conversion
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-core \
        fonts-dejavu \
        fonts-liberation \
        poppler-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

EXPOSE 8787

CMD ["python3", "server.py"]
