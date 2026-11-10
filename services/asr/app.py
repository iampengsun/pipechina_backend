from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

app = FastAPI(title="PipeChina ASR Service", version="1.0.0")
_inference_gate = asyncio.Semaphore(max(int(os.getenv("ASR_CONCURRENCY", "1")), 1))
_max_audio_bytes = int(os.getenv("ASR_MAX_AUDIO_BYTES", str(500 * 1024 * 1024)))


@lru_cache(maxsize=1)
def get_model() -> WhisperModel:
    return WhisperModel(
        os.getenv("ASR_MODEL", "large-v3"),
        device=os.getenv("ASR_DEVICE", "cpu"),
        compute_type=os.getenv("ASR_COMPUTE_TYPE", "int8"),
        download_root=os.getenv("ASR_MODEL_CACHE", "/models"),
    )


def transcribe_file(
    path: Path,
    *,
    language: str | None,
    hotwords: list[str],
) -> dict:
    segments_iter, info = get_model().transcribe(
        str(path),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        hotwords="，".join(hotwords) or None,
        word_timestamps=False,
    )
    segments = []
    full_text: list[str] = []
    for index, segment in enumerate(segments_iter):
        text = segment.text.strip()
        if not text:
            continue
        segments.append(
            {
                "index": index,
                "start_ms": max(round(segment.start * 1000), 0),
                "end_ms": max(round(segment.end * 1000), 1),
                "text": text,
                "confidence": None,
            }
        )
        full_text.append(text)
    if not segments:
        raise ValueError("no speech was recognized")
    from diarization import label_speakers

    diarization = label_speakers(path, segments)
    return {
        "diarization": diarization,
        "request_id": str(uuid4()),
        "language": info.language,
        "duration_ms": max(round(info.duration * 1000), segments[-1]["end_ms"], 1),
        "segments": segments,
        "full_text": "".join(full_text),
    }


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/transcriptions")
async def transcribe(
    file: UploadFile,
    model: str = Form(default="large-v3"),
    language: str = Form(default="zh"),
    hotwords: str = Form(default="[]"),
) -> dict:
    configured_model = os.getenv("ASR_MODEL", "large-v3")
    if model != configured_model:
        raise HTTPException(status_code=409, detail=f"service is loaded with {configured_model}")
    try:
        parsed_hotwords = json.loads(hotwords)
        if not isinstance(parsed_hotwords, list) or not all(
            isinstance(item, str) for item in parsed_hotwords
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="hotwords must be a JSON string array") from exc

    suffix = Path(file.filename or "audio.bin").suffix[:16]
    temporary = tempfile.NamedTemporaryFile(prefix="pipechina-asr-", suffix=suffix, delete=False)
    path = Path(temporary.name)
    try:
        await asyncio.to_thread(shutil.copyfileobj, file.file, temporary)
        await asyncio.to_thread(temporary.close)
        file_size = await asyncio.to_thread(lambda: path.stat().st_size)
        if file_size <= 0 or file_size > _max_audio_bytes:
            raise HTTPException(status_code=413, detail="audio size is outside the allowed range")
        async with _inference_gate:
            return await asyncio.to_thread(
                transcribe_file,
                path,
                language=language or None,
                hotwords=parsed_hotwords,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if not temporary.closed:
            await asyncio.to_thread(temporary.close)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        await file.close()


_warmup_task: asyncio.Task | None = None


async def _warmup():
    from diarization import get_encoder

    try:
        async with _inference_gate:
            await asyncio.to_thread(get_model)
            await asyncio.to_thread(get_encoder)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("model warmup failed")
        raise


@app.get("/health/ready")
async def health_ready():
    global _warmup_task
    from diarization import get_encoder

    if get_model.cache_info().currsize and get_encoder.cache_info().currsize:
        return {"status": "ready", "models_loaded": True}
    if _warmup_task is None:
        _warmup_task = asyncio.create_task(_warmup())
    if not _warmup_task.done():
        raise HTTPException(503, "models warming up")
    if _warmup_task.cancelled() or _warmup_task.exception() is not None:
        raise HTTPException(503, "model warmup failed; check service logs and restart")
    return {"status": "ready", "models_loaded": True}
