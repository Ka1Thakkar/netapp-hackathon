"""
ML Pipeline Module (Initial Placeholder)
========================================

This module provides a lightweight abstraction for:
- Feature engineering from in-memory access event aggregates
- Training / updating a classification model for dataset temperature (hot|warm|cold)
- Generating placement recommendations with confidence scores
- Persisting and loading model metadata (future: SQLite-backed registry)

Current State (MVP):
- Avoids hard dependency on scikit-learn unless installed.
- Provides a pluggable interface (`BaseTemperatureModel`) and a simple heuristic fallback.
- Supports incremental "online" updates in a simplified form.
- Designed for future integration with a proper DAO/data layer and scheduled retraining.

Intended Evolution:
1. Introduce SQLite-backed persistence for model artifacts, metrics, versioning.
2. Replace heuristic fallback with trained classifier (e.g., RandomForest, SGDClassifier).
3. Add regression path for predicted future access counts.
4. Integrate policy constraints to refine recommended_tier and candidate locations.
5. Add explainability metadata (feature importances, SHAP values) for transparency.

NOTE:
The running environment may not yet contain scikit-learn. If absent, the module
falls back to the heuristic rule logic also found in the API layer. Once
scikit-learn is added to the container image (e.g., `pip install scikit-learn`),
the `SklearnTemperatureModel` will become active automatically.

"""

from __future__ import annotations

import os
import time
import json
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# ------------------------------------------------------------------------------
# Optional scikit-learn import (lazy)
# ------------------------------------------------------------------------------
try:
    import numpy as np  # type: ignore
    from sklearn.base import BaseEstimator  # type: ignore
    from sklearn.linear_model import SGDClassifier  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore

    SKLEARN_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    SKLEARN_AVAILABLE = False
    logger.info("scikit-learn not available: %s (using heuristic fallback)", e)


# ------------------------------------------------------------------------------
# Data Structures
# ------------------------------------------------------------------------------


class FeatureVector(Dict[str, float]):
    """
    Typed alias for clarity; each feature key maps to a numeric value.
    Non-numeric values should be encoded before ingestion.
    """


class TemperaturePrediction:
    def __init__(
        self,
        tier: str,
        confidence: float,
        features: FeatureVector,
        model_version: str,
        reason: Dict[str, Any],
    ):
        self.tier = tier
        self.confidence = confidence
        self.features = features
        self.model_version = model_version
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "confidence": self.confidence,
            "features": dict(self.features),
            "model_version": self.model_version,
            "reason": self.reason,
        }


# ------------------------------------------------------------------------------
# Base Model Interface
# ------------------------------------------------------------------------------


class BaseTemperatureModel:
    """
    Abstract interface for temperature classification models.
    """

    def __init__(self, version: str):
        self.version = version

    def predict(self, feats: FeatureVector) -> TemperaturePrediction:
        raise NotImplementedError

    def partial_fit(self, batch: List[Tuple[FeatureVector, str]]) -> None:
        """
        Incremental training hook. `batch` contains (features, label).
        """
        raise NotImplementedError

    def export_metadata(self) -> Dict[str, Any]:
        return {"version": self.version, "type": self.__class__.__name__}


# ------------------------------------------------------------------------------
# Heuristic Fallback Model
# ------------------------------------------------------------------------------


class HeuristicTemperatureModel(BaseTemperatureModel):
    """
    Rule-based fallback:
    - age <= 60s => hot
    - 60s < age <= 600s => warm
    - age > 600s => cold
    Confidence scores are arbitrary but stable.
    """

    def predict(self, feats: FeatureVector) -> TemperaturePrediction:
        last_access_age_s = feats.get("last_access_age_s", math.inf)

        if math.isinf(last_access_age_s):
            tier = "cold"
            confidence = 0.55
            reason = {"rule": "no_access_recorded"}
        elif last_access_age_s <= 60:
            tier = "hot"
            confidence = 0.80
            reason = {"rule": "last_access<=60s", "age": last_access_age_s}
        elif last_access_age_s <= 600:
            tier = "warm"
            confidence = 0.70
            reason = {"rule": "60s<last_access<=600s", "age": last_access_age_s}
        else:
            tier = "cold"
            confidence = 0.75
            reason = {"rule": "last_access>600s", "age": last_access_age_s}

        return TemperaturePrediction(
            tier=tier,
            confidence=confidence,
            features=feats,
            model_version=self.version,
            reason=reason,
        )

    def partial_fit(self, batch: List[Tuple[FeatureVector, str]]) -> None:
        # No-op; rules are static.
        pass


# ------------------------------------------------------------------------------
# scikit-learn Model (activated only if available)
# ------------------------------------------------------------------------------


class SklearnTemperatureModel(BaseTemperatureModel):
    """
    Simple incremental classifier using SGDClassifier.
    Target labels: hot|warm|cold.

    Features expected (extendable):
    - read_count
    - write_count
    - bytes_read
    - bytes_written
    - last_access_age_s
    - size_bytes
    """

    def __init__(self, version: str):
        super().__init__(version)
        self.scaler = StandardScaler()
        self.model = SGDClassifier(loss="log_loss")
        self._classes = ["hot", "warm", "cold"]
        self._fitted = False
        self._feature_order = [
            "read_count",
            "write_count",
            "bytes_read",
            "bytes_written",
            "last_access_age_s",
            "size_bytes",
        ]

    def _vectorize(self, feats: FeatureVector) -> List[float]:
        return [feats.get(name, 0.0) for name in self._feature_order]

    def partial_fit(self, batch: List[Tuple[FeatureVector, str]]) -> None:
        if not batch:
            return
        X = [self._vectorize(f) for f, _ in batch]
        y = [lbl for _, lbl in batch]

        # Fit/update scaler
        if not self._fitted:
            X_scaled = self.scaler.fit_transform(X)
            self.model.partial_fit(X_scaled, y, classes=self._classes)
            self._fitted = True
        else:
            X_scaled = self.scaler.transform(X)
            self.model.partial_fit(X_scaled, y)

    def predict(self, feats: FeatureVector) -> TemperaturePrediction:
        vec = [self._vectorize(feats)]
        if not self._fitted:
            # Cold start fallback
            heuristic = HeuristicTemperatureModel(version="heuristic-fallback")
            hp = heuristic.predict(feats)
            hp.model_version = f"{self.version}-coldstart"
            hp.reason["note"] = "model_not_fitted"
            return hp

        vec_scaled = self.scaler.transform(vec)
        proba = self.model.predict_proba(vec_scaled)[0]
        label_idx = int(proba.argmax())
        tier = self.model.classes_[label_idx]
        confidence = float(proba[label_idx])
        reason = {
            "proba": dict(zip(self.model.classes_, map(float, proba))),
            "selected": tier,
        }
        return TemperaturePrediction(
            tier=tier,
            confidence=confidence,
            features=feats,
            model_version=self.version,
            reason=reason,
        )


# ------------------------------------------------------------------------------
# Pipeline Orchestrator
# ------------------------------------------------------------------------------


class TemperaturePipeline:
    """
    Orchestrates feature extraction and prediction/training workflow.
    """

    def __init__(self):
        self.version = f"pipeline-{int(time.time())}"
        if SKLEARN_AVAILABLE:
            self.model: BaseTemperatureModel = SklearnTemperatureModel(self.version)
        else:
            self.model = HeuristicTemperatureModel(self.version)
        logger.info(
            "TemperaturePipeline initialized (model=%s)", self.model.__class__.__name__
        )

    # ------------------------------------------------------------------
    # Feature Engineering
    # ------------------------------------------------------------------
    def build_features(
        self,
        access_stats: Dict[str, Any],
        dataset_meta: Dict[str, Any],
    ) -> FeatureVector:
        """
        access_stats: aggregated counts, e.g.
          {
            "read_count": int,
            "write_count": int,
            "bytes_read": int,
            "bytes_written": int,
            "last_access_ts": float or None
          }

        dataset_meta: basic metadata, e.g.
          {
            "size_bytes": int,
            ...
          }
        """
        now_ts = time.time()
        last_access_ts = access_stats.get("last_access_ts")
        age = (
            math.inf
            if last_access_ts is None
            else max(0.0, now_ts - float(last_access_ts))
        )

        feats: FeatureVector = {
            "read_count": float(access_stats.get("read_count", 0)),
            "write_count": float(access_stats.get("write_count", 0)),
            "bytes_read": float(access_stats.get("bytes_read", 0)),
            "bytes_written": float(access_stats.get("bytes_written", 0)),
            "last_access_age_s": age,
            "size_bytes": float(dataset_meta.get("size_bytes", 0)),
        }
        return feats

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------
    def predict_temperature(
        self,
        access_stats: Dict[str, Any],
        dataset_meta: Dict[str, Any],
    ) -> TemperaturePrediction:
        feats = self.build_features(access_stats, dataset_meta)
        return self.model.predict(feats)

    # ------------------------------------------------------------------
    # Training / Update
    # ------------------------------------------------------------------
    def update_model(
        self,
        labeled_batch: List[Tuple[Dict[str, Any], Dict[str, Any], str]],
    ) -> Dict[str, Any]:
        """
        labeled_batch: list of tuples:
        [
          (access_stats, dataset_meta, label),
          ...
        ]
        """
        feature_label_pairs: List[Tuple[FeatureVector, str]] = []
        for access_stats, dataset_meta, label in labeled_batch:
            feats = self.build_features(access_stats, dataset_meta)
            feature_label_pairs.append((feats, label))

        try:
            self.model.partial_fit(feature_label_pairs)
            return {
                "status": "ok",
                "updated_records": len(feature_label_pairs),
                "model_version": self.model.version,
                "model_type": self.model.__class__.__name__,
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("Model update failed: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "updated_records": 0,
                "model_version": self.model.version,
            }

    # ------------------------------------------------------------------
    # Persistence (Placeholder)
    # ------------------------------------------------------------------
    def save_metadata(self, path: str) -> None:
        meta = {
            "version": self.model.version,
            "model_type": self.model.__class__.__name__,
            "timestamp": time.time(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            logger.info("Saved model metadata to %s", path)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to save metadata: %s", e)

    def load_metadata(self, path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            logger.info("Loaded model metadata from %s", path)
            return meta
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load metadata: %s", e)
            return None


# ------------------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------------------

_pipeline: Optional[TemperaturePipeline] = None


def get_pipeline() -> TemperaturePipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = TemperaturePipeline()
    return _pipeline


# ------------------------------------------------------------------------------
# Example usage (manual test)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    pipe = get_pipeline()
    example_access = {
        "read_count": 42,
        "write_count": 3,
        "bytes_read": 10_000,
        "bytes_written": 2_000,
        "last_access_ts": time.time() - 75,  # 75 seconds ago
    }
    example_meta = {
        "size_bytes": 5_000_000,
    }

    pred = pipe.predict_temperature(example_access, example_meta)
    print("Prediction:", json.dumps(pred.to_dict(), indent=2))

    # Simulate labeled training batch
    batch = [
        (example_access, example_meta, "warm"),
        (
            {
                "read_count": 500,
                "write_count": 10,
                "bytes_read": 1_000_000,
                "bytes_written": 50_000,
                "last_access_ts": time.time() - 10,
            },
            {"size_bytes": 7_500_000},
            "hot",
        ),
    ]
    update_result = pipe.update_model(batch)
    print("Update:", json.dumps(update_result, indent=2))

    # Predict again post update
    pred2 = pipe.predict_temperature(example_access, example_meta)
    print("Prediction after update:", json.dumps(pred2.to_dict(), indent=2))
