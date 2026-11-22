from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT_DIR / "models"
OBJECT_MODEL = MODELS_DIR / "yolov8n.onnx"


def get_ai_status() -> dict[str, Any]:
    """Report real local object-detection runtime/model availability."""
    runtime_version = None
    providers: list[str] = []
    runtime_error = ""
    try:
        import onnxruntime as ort  # type: ignore
        providers = list(ort.get_available_providers())
        runtime_version = getattr(ort, "__version__", "installed")
    except Exception as exc:
        runtime_error = str(exc)

    models = {
        "object_detector": OBJECT_MODEL.is_file(),
        "license_plate_detector": (MODELS_DIR / "license_plate_detector.onnx").is_file(),
        "license_plate_ocr": (MODELS_DIR / "license_plate_ocr.onnx").is_file(),
    }
    runtime_status = "not-installed" if runtime_version is None else "runtime-installed"
    ready = bool(runtime_version and models["object_detector"] and "CPUExecutionProvider" in providers)

    if not runtime_version:
        message = "ONNX Runtime is not installed. Run bash ./setup-ai.sh as the NexGard user (dev8), not with sudo."
    elif not models["object_detector"]:
        message = "The AI runtime is installed, but the YOLOv8n object model is missing. Run bash ./setup-ai.sh as the NexGard user (dev8), not with sudo."
    elif ready:
        message = "Local YOLOv8n object detection is ready on the CPU. People, vehicles and common animals are supported."
    else:
        message = "The ONNX Runtime is installed, but its CPU execution provider is unavailable."

    return {
        "runtime": "ONNX Runtime",
        "runtime_version": runtime_version,
        "runtime_status": runtime_status,
        "available_providers": providers,
        "models": models,
        "models_present": sum(models.values()),
        "models_total": len(models),
        "inference_ready": ready,
        "object_detection_ready": ready,
        "license_plate_detection_ready": models["license_plate_detector"],
        "license_plate_ocr_ready": models["license_plate_ocr"],
        "model_name": "YOLOv8n COCO (CPU)",
        "message": message if not runtime_error else f"{message} Runtime error: {runtime_error[:240]}",
    }
