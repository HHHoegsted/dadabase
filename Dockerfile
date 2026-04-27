FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py

ENV DATA_DIR=/data
ENV PORT=8080

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "app.py"]
