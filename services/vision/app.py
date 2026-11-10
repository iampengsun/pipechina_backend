from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, UploadFile
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from token_output import completion_tokens
from transformers import AutoModelForImageTextToText, AutoProcessor

app = FastAPI(title="PipeChina Vision Service", version="1.0.0")
_inference_gate = asyncio.Semaphore(max(int(os.getenv("VISION_CONCURRENCY", "1")), 1))
_max_image_bytes = int(os.getenv("VISION_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=60)
    category: str = Field(min_length=1, max_length=40)
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=1, max_length=120)
    evidence: str = Field(min_length=1, max_length=120)
    confidence: float = Field(ge=0, le=1)


class FindingList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[Finding] = Field(max_length=3)


class SceneClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_type: Literal["equipment", "text_only", "other"]


@lru_cache(maxsize=1)
def get_runtime():
    model_id = os.getenv("VISION_MODEL", "openbmb/MiniCPM-V-4.6")
    revision = os.getenv("VISION_MODEL_REVISION", "36f34a661a4bd35d0dc2294cb044d2584646c7d3")
    processor = AutoProcessor.from_pretrained(model_id, revision=revision)
    device = os.getenv("VISION_DEVICE", "cpu")
    dtype = os.getenv("VISION_DTYPE", "float32" if device == "cpu" else "auto")
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        revision=revision,
        dtype=dtype,
        device_map=device,
    )
    return processor, model


def _json_object(text: str) -> dict:
    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        candidate = candidate[first_newline + 1 : candidate.rfind("```")].strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response does not contain a JSON object")
    value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def analyze_image(path: Path, context: str) -> dict:
    # Classify a thumbnail; equipment analysis still receives the original image.
    with tempfile.TemporaryDirectory(prefix="pipechina-scene-") as folder:
        thumbnail = Path(folder) / "scene.png"
        with Image.open(path) as source, ImageOps.exif_transpose(source) as picture:
            picture.thumbnail((512, 512))
            picture.convert("RGB").save(thumbnail)
        scene = _infer_image_json(
            thumbnail,
            "只判断图片场景，不提取文字内容、不分析隐患。"
            "仅有文字、标签、文档、屏幕文字为text_only；"
            "可见真实工业设备或管道为equipment；其余为other。"
            '只输出JSON：{"scene_type":"equipment|text_only|other"}。',
            SceneClassification,
        )
    if scene["scene_type"] != "equipment":
        return {"findings": []}
    return _infer_image_json(
        path,
        "你是油气站场巡检图片辅助分析模型。只描述直接可见的真实设备安全隐患候选，"
        "不得推断不可见泄漏、压力或设备内部状态。警示语和授权声明不是设备隐患。"
        "没有可见隐患时返回空数组。最多3条，文字简短。"
        '{"findings":[{"title":"", "category":"", "severity":"low|medium|high|critical", '
        '"description":"", "evidence":"", "confidence":0.0}]}。'
        f"现场上下文：{context or '未提供'}",
        FindingList,
    )


def _infer_image_json(path: Path, prompt: str, output_type: type[BaseModel]) -> dict:
    processor, model = get_runtime()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "path": str(path.resolve())},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        enable_thinking=False,
        processor_kwargs={
            "downsample_mode": os.getenv("VISION_DOWNSAMPLE_MODE", "4x"),
            "max_slice_nums": int(os.getenv("VISION_MAX_SLICE_NUMS", "4")),
        },
    ).to(model.device)
    # LMFE's adapter imports the old Transformers location; both names refer to the same base class.
    import transformers.tokenization_utils as tokenizer_compat
    from transformers.tokenization_utils_base import PreTrainedTokenizerBase

    if not hasattr(tokenizer_compat, "PreTrainedTokenizerBase"):
        tokenizer_compat.PreTrainedTokenizerBase = PreTrainedTokenizerBase
    from lmformatenforcer import CharacterLevelParserConfig, JsonSchemaParser
    from lmformatenforcer.integrations.transformers import (
        build_transformers_prefix_allowed_tokens_fn,
    )

    parser_config = CharacterLevelParserConfig()
    # LMFE's default alphabet is ASCII; Chinese strings otherwise fall back to premature EOS.
    parser_config.alphabet += "".join(
        chr(code)
        for start, end in [(0x3400, 0xA000), (0x3000, 0x3040), (0xFF00, 0xFFF0)]
        for code in range(start, end)
    )
    parser_config.force_json_field_order = True
    prefix = build_transformers_prefix_allowed_tokens_fn(
        processor.tokenizer,
        JsonSchemaParser(output_type.model_json_schema(), config=parser_config),
    )
    # The checkpoint declares two stop IDs; LMFE recognizes the tokenizer's EOS only.
    # Suppress the alternative pad/end-of-text ID until the JSON parser permits EOS.
    eos = processor.tokenizer.eos_token_id
    stop_ids = model.generation_config.eos_token_id
    stop_ids = stop_ids if isinstance(stop_ids, list) else [stop_ids]
    generated = model.generate(
        **inputs,
        downsample_mode=os.getenv("VISION_DOWNSAMPLE_MODE", "4x"),
        max_new_tokens=(
            64
            if output_type is SceneClassification
            else int(os.getenv("VISION_MAX_NEW_TOKENS", "2048"))
        ),
        max_time=120 if output_type is SceneClassification else 600,
        do_sample=False,
        eos_token_id=eos,
        pad_token_id=processor.tokenizer.pad_token_id,
        suppress_tokens=[token for token in stop_ids if token is not None and token != eos],
        prefix_allowed_tokens_fn=prefix,
    )
    trimmed = [
        completion_tokens(input_ids, output_ids)
        for input_ids, output_ids in zip(inputs.input_ids, generated, strict=True)
    ]
    text = processor.batch_decode(
        trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]
    if "{" not in text or "}" not in text:
        import logging

        logging.getLogger(__name__).warning(
            "vision empty structured output: prompt_tokens=%s generated_tokens=%s "
            "completion_tokens=%s decoded_chars=%s",
            len(inputs.input_ids[0]),
            len(generated[0]),
            len(trimmed[0]),
            len(text),
        )
    return output_type.model_validate(_json_object(text)).model_dump()


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/vision/analyze")
async def analyze(
    file: UploadFile,
    model: str = Form(default="openbmb/MiniCPM-V-4.6"),
    context: str = Form(default=""),
) -> dict:
    configured_model = os.getenv("VISION_MODEL", "openbmb/MiniCPM-V-4.6")
    if model != configured_model:
        raise HTTPException(status_code=409, detail=f"service is loaded with {configured_model}")
    suffix = Path(file.filename or "image.bin").suffix[:16]
    temporary = tempfile.NamedTemporaryFile(prefix="pipechina-vision-", suffix=suffix, delete=False)
    path = Path(temporary.name)
    try:
        await asyncio.to_thread(shutil.copyfileobj, file.file, temporary)
        await asyncio.to_thread(temporary.close)
        file_size = await asyncio.to_thread(lambda: path.stat().st_size)
        if file_size <= 0 or file_size > _max_image_bytes:
            raise HTTPException(status_code=413, detail="image size is outside the allowed range")
        async with _inference_gate:
            return await asyncio.to_thread(analyze_image, path, context)
    except (ValueError, ValidationError) as exc:
        import logging

        logging.getLogger(__name__).warning(
            "vision output rejected: %s", type(exc).__name__, exc_info=True
        )
        raise HTTPException(
            status_code=502,
            detail="vision output failed schema validation",
        ) from exc
    finally:
        if not temporary.closed:
            await asyncio.to_thread(temporary.close)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        await file.close()


_warmup_task: asyncio.Task | None = None


async def _warmup():
    try:
        async with _inference_gate:
            await asyncio.to_thread(get_runtime)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("model warmup failed")
        raise


@app.get("/health/ready")
async def health_ready():
    global _warmup_task
    if get_runtime.cache_info().currsize:
        return {"status": "ready", "models_loaded": True}
    if _warmup_task is None:
        _warmup_task = asyncio.create_task(_warmup())
    if not _warmup_task.done():
        raise HTTPException(503, "models warming up")
    if _warmup_task.cancelled() or _warmup_task.exception() is not None:
        raise HTTPException(503, "model warmup failed; check service logs and restart")
    return {"status": "ready", "models_loaded": True}
