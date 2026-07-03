"""
Kenzy Speaker service.

Identifies enrolled speakers from raw PCM audio using SpeechBrain's
ECAPA-TDNN model.  Embeddings are stored as numpy arrays on disk — one
.npy file per speaker containing all enrolled utterance embeddings of
shape (N, embedding_dim).  Identification uses the cosine similarity
between the new embedding and the per-speaker centroid (mean of all
stored embeddings).

Endpoints
---------
  GET  /health
  POST /identify   → speaker name + confidence
  POST /enroll     → add an utterance embedding for a named speaker
  GET  /speakers   → list enrolled speakers + sample counts
  POST /speakers/{name}/rename → rename a speaker profile
  DELETE /speakers/{name} → remove a speaker profile
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from kenzy import kenzy_version
from kenzy.fastapi_auth import (
    install_backup_endpoint,
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
    install_upgrade_endpoint,
)
from kenzy.logutil import quiet_health_access_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IdentifyRequest(BaseModel):
    audio_b64: str  # base64-encoded int16 PCM at 16 kHz mono
    room_id: str | None = None


class IdentifyResponse(BaseModel):
    speaker: str
    confidence: float


class EnrollRequest(BaseModel):
    audio_b64: str
    name: str


class EnrollResponse(BaseModel):
    status: str
    name: str
    sample_count: int


class SpeakerInfo(BaseModel):
    name: str
    samples: int  # number of enrolled utterance embeddings


class SpeakersResponse(BaseModel):
    speakers: list[SpeakerInfo]


class RenameRequest(BaseModel):
    new_name: str


class StatusResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App + module-level state
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy Speaker Service", version="0.1.0")

_classifier: Any = None
_embeddings_dir: Path = Path("data/speakers")
_identify_threshold: float = 0.25
_unknown_speaker: str = "unknown"
_sem: asyncio.Semaphore | None = None
_model_name: str = ""  # surfaced on /health for the dashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_embedding(pcm: bytes) -> np.ndarray[Any, Any]:
    """Extract a speaker embedding from raw int16 PCM at 16 kHz."""
    import torch

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    waveform = torch.tensor(audio).unsqueeze(0)  # (1, samples)
    with torch.no_grad():
        emb = _classifier.encode_batch(waveform)  # (1, 1, dim)
    embedding: np.ndarray[Any, Any] = emb.squeeze().numpy()  # (dim,)
    return embedding


def _cosine_sim(a: np.ndarray[Any, Any], b: np.ndarray[Any, Any]) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / (denom + 1e-8))


def _safe_speaker_name(name: str) -> str:
    """Validate a speaker name used to build a ``<name>.npy`` path (F-5).

    Names map directly to filenames, so reject anything that could escape the
    embeddings dir or be otherwise unsafe. Returns the trimmed name.
    """
    name = name.strip()
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
        or any(ord(c) < 32 for c in name)
    ):
        raise HTTPException(status_code=400, detail="invalid speaker name")
    return name


def _speaker_path(name: str) -> Path:
    return _embeddings_dir / f"{name}.npy"


def _load_embeddings() -> dict[str, np.ndarray[Any, Any]]:
    """Return {speaker_name: centroid_embedding} for all enrolled speakers."""
    result: dict[str, np.ndarray[Any, Any]] = {}
    for p in _embeddings_dir.glob("*.npy"):
        stored = np.load(p)  # (N, dim)
        result[p.stem] = stored.mean(axis=0)
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": kenzy_version(),
        "model": _model_name,
        "threshold": _identify_threshold,
    }


@app.post("/identify", response_model=IdentifyResponse)
async def identify(req: IdentifyRequest) -> IdentifyResponse:
    assert _sem is not None
    pcm = base64.b64decode(req.audio_b64)
    loop = asyncio.get_running_loop()

    async with _sem:
        embedding = await loop.run_in_executor(None, _get_embedding, pcm)

    centroids = _load_embeddings()
    if not centroids:
        return IdentifyResponse(speaker=_unknown_speaker, confidence=0.0)

    best_name = _unknown_speaker
    best_score = -1.0
    for name, centroid in centroids.items():
        score = _cosine_sim(embedding, centroid)
        if score > best_score:
            best_score = score
            best_name = name if score >= _identify_threshold else _unknown_speaker

    log.info("[%s] speaker=%s confidence=%.3f", req.room_id or "?", best_name, best_score)
    return IdentifyResponse(speaker=best_name, confidence=round(best_score, 4))


@app.post("/enroll", response_model=EnrollResponse)
async def enroll(req: EnrollRequest) -> EnrollResponse:
    name = _safe_speaker_name(req.name)  # validate input before anything else
    assert _sem is not None

    pcm = base64.b64decode(req.audio_b64)
    loop = asyncio.get_running_loop()

    async with _sem:
        embedding = await loop.run_in_executor(None, _get_embedding, pcm)

    path = _speaker_path(name)
    if path.exists():
        existing = np.load(path)
        updated = np.vstack([existing, embedding.reshape(1, -1)])
    else:
        updated = embedding.reshape(1, -1)

    np.save(path, updated)
    count = len(updated)
    log.info("Enrolled '%s' — %d sample(s) stored", name, count)
    return EnrollResponse(status="ok", name=name, sample_count=count)


@app.get("/speakers", response_model=SpeakersResponse)
async def list_speakers() -> SpeakersResponse:
    speakers: list[SpeakerInfo] = []
    for p in sorted(_embeddings_dir.glob("*.npy")):
        try:
            samples = int(np.load(p).shape[0])
        except Exception:
            samples = 0
        speakers.append(SpeakerInfo(name=p.stem, samples=samples))
    return SpeakersResponse(speakers=speakers)


@app.delete("/speakers/{name}", response_model=StatusResponse)
async def delete_speaker(name: str) -> StatusResponse:
    name = _safe_speaker_name(name)
    path = _speaker_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Speaker '{name}' not found")
    path.unlink()
    log.info("Deleted speaker profile: %s", name)
    return StatusResponse(status="ok")


@app.post("/speakers/{name}/rename", response_model=EnrollResponse)
async def rename_speaker(name: str, req: RenameRequest) -> EnrollResponse:
    name = _safe_speaker_name(name)
    new_name = _safe_speaker_name(req.new_name)
    src = _speaker_path(name)
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Speaker '{name}' not found")
    dst = _speaker_path(new_name)
    if dst.exists():
        raise HTTPException(status_code=409, detail=f"Speaker '{new_name}' already exists")
    src.rename(dst)
    count = int(np.load(dst).shape[0])
    log.info("Renamed speaker profile: %s → %s", name, new_name)
    return EnrollResponse(status="ok", name=new_name, sample_count=count)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _classifier, _embeddings_dir, _identify_threshold, _unknown_speaker, _sem, _model_name

    import uvicorn  # type: ignore[import-untyped]

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import effective_bind, load_service_config, start_registration

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("speaker")
    start_registration("speaker", cfg)  # auto-announce to the server (dashboard + pipeline)

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)
    install_upgrade_endpoint(app, "speaker")
    # Backup slice: the enrolled voice embeddings — the one part of a Kenzy
    # deployment that cannot be regenerated. Served under the canonical archive
    # path so the server's merged backup is complete even when this service
    # runs on its own host.
    install_backup_endpoint(app, lambda: [(_embeddings_dir, "data/speakers")])

    _embeddings_dir = Path(cfg.get("embeddings_dir", "data/speakers"))
    _embeddings_dir.mkdir(parents=True, exist_ok=True)

    _identify_threshold = float(cfg.get("identify_threshold", 0.25))
    _unknown_speaker = str(cfg.get("unknown_speaker", "unknown"))

    try:
        from speechbrain.pretrained import EncoderClassifier  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("speechbrain is not installed – run: pip install speechbrain") from exc

    model_source = cfg.get("model_source", "speechbrain/spkrec-ecapa-voxceleb")
    _model_name = str(model_source).rstrip("/").split("/")[-1]
    model_save_dir = cfg.get("model_save_dir", "models/speaker")
    log.info("Loading speaker model from %s…", model_save_dir)
    _classifier = EncoderClassifier.from_hparams(
        source=model_source,
        savedir=model_save_dir,
        run_opts={"device": "cpu"},
    )
    log.info("Speaker model ready.")

    _sem = asyncio.Semaphore(1)

    uvicorn.run(
        app,
        host=effective_bind(cfg),
        port=int(cfg.get("port", 8768)),
        log_level=str(cfg.get("log_level", "info")).lower(),
    )


if __name__ == "__main__":
    main()
