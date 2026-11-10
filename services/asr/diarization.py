"""Local speaker embeddings and bounded clustering; anonymous labels, not identity proof."""

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_encoder():
    from huggingface_hub import snapshot_download
    from speechbrain.inference.classifiers import EncoderClassifier

    folder = snapshot_download(
        repo_id="speechbrain/spkrec-ecapa-voxceleb",
        revision=os.getenv("SPEAKER_MODEL_REVISION", "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"),
        cache_dir=os.getenv("ASR_MODEL_CACHE", "/models") + "/speaker",
        allow_patterns=["hyperparams.yaml", "*.ckpt", "label_encoder.txt"],
    )
    return EncoderClassifier.from_hparams(
        source=folder,
        savedir=str(Path(folder) / "runtime"),
        overrides={"pretrained_path": folder},
        run_opts={"device": "cpu"},
    )


def cluster_embeddings(embeddings, threshold):
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    values = np.asarray(embeddings, dtype="float64")
    if len(values) == 1:
        return [0]
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(values).all() or (norms <= 1e-10).any():
        raise ValueError("invalid speaker embeddings")
    values = values / norms
    labels = fcluster(
        linkage(values, method="average", metric="cosine"), t=threshold, criterion="distance"
    )
    identities = {}
    return [identities.setdefault(int(label), len(identities)) for label in labels]


def label_speakers(path, segments):
    import numpy as np
    import torch
    from faster_whisper.audio import decode_audio

    waveform = decode_audio(str(path), sampling_rate=16000)
    encoder = get_encoder()
    windows, owners = [], []
    for index, segment in enumerate(segments):
        start = int(segment["start_ms"] * 16)
        end = min(int(segment["end_ms"] * 16), len(waveform))
        for offset in range(start, end, 32000):
            clip = waveform[offset : min(offset + 48000, end)]
            if len(clip) < 16000:
                continue
            windows.append(clip)
            owners.append(index)
    if len(windows) > int(os.getenv("SPEAKER_MAX_WINDOWS", "3000")):
        raise ValueError("speaker clustering budget exceeded; split the recording")
    if not windows:
        return {"status": "insufficient_speech", "speakers": 0}
    embeddings = []
    with torch.inference_mode():
        for clip in windows:
            vector = encoder.encode_batch(torch.from_numpy(np.array(clip)).unsqueeze(0))
            embeddings.append(vector.squeeze().cpu().numpy())
    labels = cluster_embeddings(embeddings, float(os.getenv("SPEAKER_CLUSTER_THRESHOLD", "0.50")))
    for index, segment in enumerate(segments):
        votes = [label for owner, label in zip(owners, labels, strict=True) if owner == index]
        if votes:
            label = max(set(votes), key=lambda value: (votes.count(value), -value))
            segment["speaker_label"] = f"SPEAKER_{label:02d}"
    return {
        "status": "completed",
        "speakers": len(set(labels)),
        "method": "ecapa_cosine_average_linkage",
        "identity_verified": False,
        "cluster_threshold": float(os.getenv("SPEAKER_CLUSTER_THRESHOLD", "0.50")),
        "model_revision": os.getenv(
            "SPEAKER_MODEL_REVISION", "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
        ),
    }
