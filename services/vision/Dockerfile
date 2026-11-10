FROM python:3.13.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY requirements.txt constraints.txt ./
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN python -m pip install --no-cache-dir \
        torch==2.13.0 torchvision==0.28.0 \
        --index-url "${PYTORCH_INDEX_URL}" --constraint constraints.txt \
    && python -m pip install --no-cache-dir --constraint constraints.txt --requirement requirements.txt
COPY app.py token_output.py ./

RUN useradd --system --uid 10003 --create-home inference \
    && install -d -o inference -g inference /models
USER 10003

EXPOSE 8102
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8102"]
