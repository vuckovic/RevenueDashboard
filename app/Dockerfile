FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Make the code readable for appuser regardless of host file permissions
RUN chmod -R a+rX /app && useradd -m appuser
USER appuser
