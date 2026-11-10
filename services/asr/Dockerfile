FROM python:3.13.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

WORKDIR /service
COPY requirements.txt constraints.txt ./
RUN python -m pip install --no-cache-dir --constraint constraints.txt --requirement requirements.txt
COPY app.py diarization.py ./

RUN useradd --system --uid 10002 --create-home inference \
    && install -d -o inference -g inference /models
USER 10002

EXPOSE 8101
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8101"]
