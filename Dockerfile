FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN test -f config.json || cp config.example.json config.json

EXPOSE 8080

CMD ["python", "main.py"]

