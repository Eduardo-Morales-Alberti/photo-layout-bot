FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY layout.py export.py config.py bot.py ./

RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "bot.py"]
