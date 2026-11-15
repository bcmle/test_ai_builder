FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation .
COPY config ./config
RUN useradd --uid 10001 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8000
CMD ["uvicorn", "ai_builder.api:app", "--host", "0.0.0.0", "--port", "8000"]
