#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as the NexGard application user (dev8), not with sudo."
  echo "It will ask sudo only when restarting the service."
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Python environment is missing. Run ./setup.sh first."
  exit 1
fi

if ! command -v curl >/dev/null 2>&1 || ! command -v sha256sum >/dev/null 2>&1; then
  echo "curl and sha256sum are required. Install them with: sudo apt install curl coreutils"
  exit 1
fi

mkdir -p models

echo "==> Installing local CPU inference dependencies"
.venv/bin/python -m pip install -r backend/requirements.txt

echo "==> Downloading the 12.8 MB YOLOv8n COCO ONNX object-detection model"
MODEL_URL="https://huggingface.co/webml/yolov8n/resolve/main/onnx/yolov8n.onnx"
MODEL_PATH="models/yolov8n.onnx"
EXPECTED_SHA256="190ba5f1e61411a001683e349d6b2cdb0804c0dc67a5e34cd8ff6fd00ee54b4d"
TMP_PATH="${MODEL_PATH}.download"

if [[ ! -f "$MODEL_PATH" ]] || ! echo "${EXPECTED_SHA256}  ${MODEL_PATH}" | sha256sum -c --status; then
  rm -f "$TMP_PATH"
  curl --fail --location --retry 3 --connect-timeout 15 "$MODEL_URL" -o "$TMP_PATH"
  echo "${EXPECTED_SHA256}  ${TMP_PATH}" | sha256sum -c -
  mv "$TMP_PATH" "$MODEL_PATH"
else
  echo "Existing model checksum is valid."
fi

PYTHONPATH="$ROOT_DIR/backend" .venv/bin/python - <<'PY'
import io
from pathlib import Path

import numpy as np
import onnxruntime as ort
import PIL
from PIL import Image

from app.ai_detector import YoloV8Detector

model = Path("models/yolov8n.onnx")
if not model.is_file() or model.stat().st_size < 1_000_000:
    raise SystemExit(f"Model file is missing or unexpectedly small: {model}")
print("ONNX Runtime:", ort.__version__)
print("Providers:", ", ".join(ort.get_available_providers()))
print("NumPy:", np.__version__)
print("Pillow:", PIL.__version__)

# Exercise the exact NexGard detector path with a synthetic frame. This checks
# model loading, preprocessing, ONNX inference and output parsing without using
# real camera footage or creating an event.
detector = YoloV8Detector(model)
frame = Image.new("RGB", (640, 360), (32, 32, 32))
encoded = io.BytesIO()
frame.save(encoded, format="JPEG", quality=80)
detections = detector.detect(encoded.getvalue())
print("Model inference self-test: PASS")
print("Synthetic-frame detections:", len(detections))
print("Detector providers:", ", ".join(detector.providers))
PY

echo "==> Restarting NexGard so the AI model loads"
sudo systemctl restart nexgard-security
sleep 2
sudo systemctl --no-pager --full status nexgard-security || true

echo
echo "AI installation complete. Open your configured NexGard HTTPS address and visit AI Center."
