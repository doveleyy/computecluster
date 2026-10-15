FROM python:3.13-slim
WORKDIR /app
COPY server.py /app/server.py
USER 1000:1000
EXPOSE 8080
CMD ["python", "/app/server.py"]
