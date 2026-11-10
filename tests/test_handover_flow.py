from fastapi.testclient import TestClient

from tests.helpers import create_confirmed_handover


def create_transcribed_handover(client: TestClient, filename: str = "shift.m4a") -> str:
    created = client.post(
        "/api/v1/audio-records",
        json={
            "shift_date": "2026-09-02",
            "shift_code": "night",
            "filename": filename,
            "size_bytes": 1024,
            "mime_type": "audio/mp4",
        },
    )
    assert created.status_code == 201, created.text
    audio_id = created.json()["audio"]["id"]
    completed = client.post(f"/api/v1/audio-records/{audio_id}/uploads:complete", json={})
    assert completed.status_code == 202
    assert client.post(f"/api/v1/audio-records/{audio_id}:transcribe").status_code == 202
    return audio_id


def test_handover_end_to_end(client: TestClient) -> None:
    result = create_confirmed_handover(client)
    audio_id = result["audio_id"]

    record = client.get(f"/api/v1/audio-records/{audio_id}")
    assert record.status_code == 200
    assert record.json()["business_status"] == "confirmed"
    assert record.json()["current_transcript_version_id"]
    assert record.json()["current_summary_version_id"]

    segments = client.get(f"/api/v1/audio-records/{audio_id}/segments")
    assert segments.status_code == 200
    assert len(segments.json()) == 3
    assert segments.json()[1]["start_ms"] == 8000

    versions = client.get(f"/api/v1/audio-records/{audio_id}/versions").json()
    assert len(versions["transcripts"]) == 1
    assert len(versions["summaries"]) == 1
    assert versions["summaries"][0]["transcript_version_id"] == versions["transcripts"][0]["id"]
    assert len(versions["summaries"][0]["source_segment_ids"]) == 3

    listed = client.get("/api/v1/audio-records")
    assert listed.status_code == 200
    assert audio_id in {item["id"] for item in listed.json()}

    job = client.get(f"/api/v1/jobs/{result['job_id']}")
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    assert job.json()["progress"] == 100


def test_confirmed_audio_is_delete_protected(client: TestClient) -> None:
    audio_id = create_confirmed_handover(client)["audio_id"]
    response = client.delete(f"/api/v1/audio-records/{audio_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
    assert client.post(f"/api/v1/audio-records/{audio_id}:confirm").status_code == 409
    assert client.post(f"/api/v1/audio-records/{audio_id}:transcribe").status_code == 409
    assert client.post(f"/api/v1/audio-records/{audio_id}:summarize").status_code == 409
    edited = client.put(
        f"/api/v1/audio-records/{audio_id}/transcript",
        json={"full_text": "不得覆盖已确认记录", "segments": []},
    )
    assert edited.status_code == 409


def test_audio_input_and_version_integrity(client: TestClient) -> None:
    unsupported = client.post(
        "/api/v1/audio-records",
        json={
            "shift_date": "2026-09-02",
            "shift_code": "night",
            "filename": "notes.txt",
            "size_bytes": 12,
            "mime_type": "text/plain",
        },
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"

    audio_id = create_transcribed_handover(client)
    overlapping = client.put(
        f"/api/v1/audio-records/{audio_id}/transcript",
        json={
            "full_text": "重叠分段",
            "segments": [
                {"start_ms": 0, "end_ms": 5000, "text": "第一段"},
                {"start_ms": 4000, "end_ms": 6000, "text": "第二段"},
            ],
        },
    )
    assert overlapping.status_code == 422

    outside_duration = client.put(
        f"/api/v1/audio-records/{audio_id}/transcript",
        json={
            "full_text": "超出时长",
            "segments": [{"start_ms": 0, "end_ms": 25000, "text": "超出时长"}],
        },
    )
    assert outside_duration.status_code == 409


def test_summary_rejects_evidence_from_another_audio(client: TestClient) -> None:
    first_id = create_transcribed_handover(client, "first.m4a")
    second_id = create_transcribed_handover(client, "second.m4a")
    second_versions = client.get(f"/api/v1/audio-records/{second_id}/versions").json()
    foreign_transcript_id = second_versions["transcripts"][0]["id"]

    response = client.put(
        f"/api/v1/audio-records/{first_id}/summary",
        json={
            "transcript_version_id": foreign_transcript_id,
            "content": {
                "operating_status": ["运行正常"],
                "pending_items": [],
                "risks": [],
                "attention_items": [],
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


def test_manual_operation_record_history(client: TestClient) -> None:
    created = client.post(
        "/api/v1/manual-operation-records",
        json={
            "occurred_at": "2026-09-02T08:30:00+08:00",
            "record_type": "巡检补录",
            "content": "现场确认阀门无新增渗漏。",
            "structured_data": {"valve": "V-2"},
        },
    )
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]

    updated = client.put(
        f"/api/v1/manual-operation-records/{record_id}",
        json={"content": "现场复核完成。", "structured_data": {"valve": "V-2"}},
    )
    assert updated.status_code == 200

    detail = client.get(f"/api/v1/manual-operation-records/{record_id}")
    assert detail.status_code == 200
    assert detail.json()["record_type"] == "巡检补录"
    assert detail.json()["occurred_at"] == "2026-09-02T00:30:00+00:00"
    listed = client.get("/api/v1/manual-operation-records")
    assert record_id in {item["id"] for item in listed.json()}
    versions = client.get(f"/api/v1/manual-operation-records/{record_id}/versions")
    assert [item["version"] for item in versions.json()] == [1, 2]
