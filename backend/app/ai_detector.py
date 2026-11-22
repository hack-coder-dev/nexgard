from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]

# Keep the CPU workload focused on security-relevant object classes.
TARGET_CLASSES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
}


class YoloV8Detector:
    """Small YOLOv8 COCO ONNX detector using ONNX Runtime CPU inference."""

    def __init__(self, model_path: str | Path, *, confidence: float = 0.42, iou_threshold: float = 0.45):
        try:
            import onnxruntime as ort
        except Exception as exc:  # pragma: no cover - exercised on target server
            raise RuntimeError("ONNX Runtime is not installed. Run bash ./setup-ai.sh as the NexGard user (dev8), not with sudo.") from exc

        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise RuntimeError(f"AI model not found: {self.model_path}. Run bash ./setup-ai.sh as the NexGard user (dev8), not with sudo.")

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(2, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(self.model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.input_height = int(shape[2]) if isinstance(shape[2], int) else 640
        self.input_width = int(shape[3]) if isinstance(shape[3], int) else 640
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.providers = self.session.get_providers()
        self.version = getattr(ort, "__version__", "installed")

    def detect(self, image_bytes: bytes, max_detections: int = 20) -> list[dict[str, Any]]:
        with Image.open(io.BytesIO(image_bytes)) as source:
            original = source.convert("RGB")
        original_width, original_height = original.size
        if not original_width or not original_height:
            return []

        scale = min(self.input_width / original_width, self.input_height / original_height)
        resized_width = max(1, int(round(original_width * scale)))
        resized_height = max(1, int(round(original_height * scale)))
        resized = original.resize((resized_width, resized_height), Image.Resampling.BILINEAR)
        pad_x = (self.input_width - resized_width) // 2
        pad_y = (self.input_height - resized_height) // 2
        canvas = Image.new("RGB", (self.input_width, self.input_height), (114, 114, 114))
        canvas.paste(resized, (pad_x, pad_y))

        image_array = np.asarray(canvas, dtype=np.float32) / 255.0
        tensor = np.transpose(image_array, (2, 0, 1))[None, ...].astype(np.float32, copy=False)
        raw = self.session.run(None, {self.input_name: tensor})[0]
        predictions = np.asarray(raw)
        if predictions.ndim == 3:
            predictions = predictions[0]
        if predictions.ndim != 2:
            raise RuntimeError(f"Unexpected YOLO output shape: {tuple(predictions.shape)}")
        # Standard YOLOv8 detection ONNX output is [84, 8400]. Also accept [8400, 84].
        if predictions.shape[0] < predictions.shape[1] and predictions.shape[0] in (84, 85):
            predictions = predictions.T
        if predictions.shape[1] < 5:
            raise RuntimeError(f"Unsupported YOLO output dimensions: {tuple(predictions.shape)}")

        class_scores = predictions[:, 4:]
        class_ids = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(class_scores.shape[0]), class_ids]
        candidate_indices = np.where(scores >= self.confidence)[0]
        candidates: list[dict[str, Any]] = []
        for index in candidate_indices:
            class_id = int(class_ids[index])
            if class_id not in TARGET_CLASSES:
                continue
            cx, cy, width, height = (float(v) for v in predictions[index, :4])
            x1 = (cx - width / 2 - pad_x) / scale
            y1 = (cy - height / 2 - pad_y) / scale
            x2 = (cx + width / 2 - pad_x) / scale
            y2 = (cy + height / 2 - pad_y) / scale
            x1 = max(0.0, min(float(original_width), x1))
            y1 = max(0.0, min(float(original_height), y1))
            x2 = max(0.0, min(float(original_width), x2))
            y2 = max(0.0, min(float(original_height), y2))
            if x2 <= x1 or y2 <= y1:
                continue
            candidates.append({
                "class_id": class_id,
                "class_name": TARGET_CLASSES[class_id],
                "confidence": float(scores[index]),
                "bbox": [x1, y1, x2, y2],
            })

        # Class-aware NMS. This is intentionally kept in NumPy/Python to avoid
        # a second heavyweight computer-vision dependency on the server.
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        selected: list[dict[str, Any]] = []
        while candidates and len(selected) < max_detections:
            current = candidates.pop(0)
            selected.append(current)
            remaining: list[dict[str, Any]] = []
            ax1, ay1, ax2, ay2 = current["bbox"]
            area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
            for other in candidates:
                if other["class_id"] != current["class_id"]:
                    remaining.append(other)
                    continue
                bx1, by1, bx2, by2 = other["bbox"]
                intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
                area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
                union = area_a + area_b - intersection
                iou = intersection / union if union > 0 else 0.0
                if iou < self.iou_threshold:
                    remaining.append(other)
            candidates = remaining
        return selected
