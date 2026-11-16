"""
Router: Person registration & recognition endpoints (Phase 11)
  POST   /api/v1/persons/register        — register a person from a JPEG (multi-face: all faces embedded)
  GET    /api/v1/persons                 — list registered persons
  DELETE /api/v1/persons/{person_key}    — delete a person
  POST   /api/v1/persons/recognize       — one-shot recognition on an uploaded JPEG
  GET    /api/v1/persons/status          — recognition pipeline health

Embeddings are stored, not raw images (one snapshot per registration is kept
for the app gallery). The embedder backend is pluggable — swap for FaceNet
later without changing these APIs.
"""

import logging

from fastapi import APIRouter, Request, Query

from models.person_registry import (
    PersonListResponse,
    PersonRecord,
    PersonRegisterResponse,
    PersonDeleteResponse,
    RecognizeResponse,
    RecognizeFace,
    RecognizeStatusResponse,
)
from services import person_registry, person_recognition

router = APIRouter(prefix="/api/v1/persons", tags=["Person Recognition"])
logger = logging.getLogger(__name__)


def _decode_jpeg(raw: bytes):
    import cv2
    import numpy as np
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


@router.post("/register", response_model=PersonRegisterResponse)
async def register_person(request: Request, name: str = Query(..., min_length=1, max_length=64)):
    """
    Register a person by name. The JPEG photo is the RAW request body and the
    name is a query parameter (FastAPI cannot bind a JSON model and a raw
    body simultaneously):

        POST /api/v1/persons/register?name=Grandpa   (body = JPEG bytes)

    Every face found in the photo becomes a reference embedding.
    """
    raw = await request.body()
    frame = _decode_jpeg(raw) if raw else None
    if frame is None:
        return PersonRegisterResponse(ok=False, error="upload a JPEG photo body")

    faces = person_recognition.detect_faces(frame)
    if not faces:
        return PersonRegisterResponse(ok=False, faces_found=0,
                                      error="no face found in photo")

    embeddings = []
    for (x, y, w, h) in faces:
        crop = frame[max(0, y):y + h, max(0, x):x + w]
        emb = person_recognition.embed_face(crop)
        if emb:
            embeddings.append(emb)
    if not embeddings:
        return PersonRegisterResponse(ok=False, faces_found=len(faces),
                                      error="could not compute embeddings")

    snapshot_path = person_recognition._save_unknown_snapshot(frame)  # reuse snapshot helper
    try:
        record = person_registry.add_person(name, embeddings, snapshot_path)
    except ValueError as exc:
        return PersonRegisterResponse(ok=False, faces_found=len(faces), error=str(exc))
    return PersonRegisterResponse(ok=True, person=record, faces_found=len(faces))


@router.get("", response_model=PersonListResponse)
def list_persons():
    persons = person_registry.list_persons()
    return PersonListResponse(persons=[PersonRecord(**p) for p in persons],
                              count=len(persons))


@router.delete("/{person_key}", response_model=PersonDeleteResponse)
def delete_person(person_key: str):
    return PersonDeleteResponse(deleted=person_registry.delete_person(person_key))


@router.post("/recognize", response_model=RecognizeResponse)
async def recognize_person(request: Request):
    """One-shot recognition: upload a JPEG → per-face known/unknown + match."""
    raw = await request.body()
    frame = _decode_jpeg(raw) if raw else None
    if frame is None:
        return RecognizeResponse(ok=False, faces=[], unknown_count=0,
                                 error="upload a JPEG photo body")
    result = person_recognition.recognize_frame(frame)
    return RecognizeResponse(ok=True, **result)


@router.get("/status", response_model=RecognizeStatusResponse)
def recognize_status():
    return RecognizeStatusResponse(**person_recognition.stats())
