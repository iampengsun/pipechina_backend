from fastapi.testclient import TestClient


def create_transcribed_meeting(client: TestClient) -> str:
    created = client.post(
        "/api/v1/meetings",
        json={
            "title": "西部输气站月度运行协调会",
            "started_at": "2026-09-20T09:00:00+08:00",
            "ended_at": "2026-09-20T10:30:00+08:00",
            "location": "一号会议室",
            "filename": "monthly-meeting.m4a",
            "size_bytes": 1024,
            "mime_type": "audio/mp4",
            "participants": [
                {"participant_key": "speaker-0", "display_name": "张调度", "role": "主持人"},
                {"participant_key": "speaker-1", "display_name": "李工程师", "role": "设备工程师"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    meeting_id = created.json()["meeting"]["id"]
    completed = client.post(f"/api/v1/meetings/{meeting_id}/uploads:complete", json={})
    assert completed.status_code == 202, completed.text
    transcribed = client.post(f"/api/v1/meetings/{meeting_id}:transcribe")
    assert transcribed.status_code == 202, transcribed.text
    return meeting_id


def test_meeting_upload_transcript_and_manual_correction(client: TestClient) -> None:
    meeting_id = create_transcribed_meeting(client)

    detail = client.get(f"/api/v1/meetings/{meeting_id}")
    assert detail.status_code == 200
    assert len(detail.json()["participants"]) == 2

    transcript = client.get(f"/api/v1/meetings/{meeting_id}/transcript")
    assert transcript.status_code == 200
    assert transcript.json()["version"]["source"] == "ai"
    assert transcript.json()["segments"]

    corrected = client.put(
        f"/api/v1/meetings/{meeting_id}/transcript",
        json={
            "full_text": "张调度：确认下周开展阀门检查。",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 5000,
                    "text": "确认下周开展阀门检查。",
                    "speaker_label": "speaker-0",
                }
            ],
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == 2


def test_meeting_publication_enters_knowledge_review_flow(client: TestClient) -> None:
    client.app.state.settings.knowledge_backend = "fake"
    meeting_id = create_transcribed_meeting(client)
    base = client.post("/api/v1/knowledge/bases", json={"name": "会议知识库"})
    assert base.status_code == 201, base.text

    published = client.post(
        f"/api/v1/meetings/{meeting_id}:publish-to-knowledge",
        json={"knowledge_base_id": base.json()["id"]},
    )
    assert published.status_code == 202, published.text
    result = published.json()
    assert result["index_job_id"]
    assert result["knowledge_status"] == "review"

    version = client.get(f"/api/v1/knowledge/documents/{result['knowledge_document_id']}/versions")
    assert version.status_code == 200
    assert version.json()[0]["status"] == "review"


def test_meeting_rejects_overlapping_manual_segments(client: TestClient) -> None:
    meeting_id = create_transcribed_meeting(client)
    response = client.put(
        f"/api/v1/meetings/{meeting_id}/transcript",
        json={
            "full_text": "重叠",
            "segments": [
                {"start_ms": 0, "end_ms": 5000, "text": "第一段"},
                {"start_ms": 4000, "end_ms": 6000, "text": "第二段"},
            ],
        },
    )
    assert response.status_code == 422
