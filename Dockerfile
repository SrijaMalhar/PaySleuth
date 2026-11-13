FROM python:3.12-slim

# A non-root user: the container needs no root, and a compromised process
# should not be able to write to the image. UID 1000 because Hugging Face
# Spaces starts the container as that uid, and files owned by anyone else
# become unreadable when the platform switches user at runtime.
RUN useradd --create-home --uid 1000 paysleuth
WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY paysleuth/ paysleuth/
COPY sample_data/ sample_data/
COPY tools/ tools/
USER paysleuth

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# One image, two roles: the API by default, the worker with the command
# overridden, so both always run the same code.
#   docker run ... paysleuth                          -> API
#   docker run ... paysleuth python -m paysleuth.worker -> worker
CMD ["sh", "-c", "uvicorn paysleuth.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
