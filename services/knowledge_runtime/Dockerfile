FROM python:3.13.15-slim-trixie
WORKDIR /service
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY services/knowledge_runtime/requirements.txt services/knowledge_runtime/constraints.txt ./
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.13.0 torchvision==0.28.0 --index-url "${PYTORCH_INDEX_URL}" --constraint constraints.txt \
    && pip install --no-cache-dir --constraint constraints.txt -r requirements.txt
COPY services/knowledge_runtime/main.py services/knowledge_runtime/convert.py ./
COPY app/shared/media/documents.py ./app/shared/media/documents.py
RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker /data \
    && mkdir -p /usr/local/lib/python3.13/site-packages/rapidocr/models \
    && chown worker /usr/local/lib/python3.13/site-packages/rapidocr/models \
    && mkdir -p /home/worker/.cache/huggingface \
    && chown -R worker /home/worker/.cache
USER worker
ENV HF_HOME=/home/worker/.cache/huggingface
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
