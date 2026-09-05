FROM python:3.12-slim
WORKDIR /app
RUN adduser --disabled-password --gecos "" appuser
# Requirements copied first so the dependency layer is cached across source
# edits, which is most of the rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chown -R appuser:appuser /app
USER appuser
