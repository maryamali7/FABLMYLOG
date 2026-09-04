FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV BOT_MODE=paper BOT_HOST=0.0.0.0 BOT_PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app"]
