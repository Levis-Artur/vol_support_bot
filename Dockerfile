FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --system --uid 10001 --create-home bot
COPY bot.py config.py healthcheck.py telegram_ids.py schema.sql ./
USER bot
CMD ["python", "bot.py"]
