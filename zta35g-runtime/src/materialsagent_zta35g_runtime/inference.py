from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import math
from numbers import Real
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .constants import (
    GUIDE_SCALE,
    IMG_SIZE,
    MAX_WARNINGS,
    MODEL_BUNDLE_ID,
    NUM_SAMPLES,
    PROCESS_PARAMETER_ORDER,
    TIMESTEPS,
)
from .contracts import safe_error
from .model_architecture import cosine_beta_schedule
from .model_bundle import ModelBundleLoader


class InferenceNotLoadedError(RuntimeError):
    pass


class EngineLoadError(RuntimeError):
    pass


class SemGenerationError(RuntimeError):
    pass


class MechanicalPredictionError(RuntimeError):
    pass


class InvalidModelOutputError(RuntimeError):
    pass


@dataclass(frozen=True)
class InferenceResult:
    status: str
    completed_outputs: Tuple[str, ...]
    failed_outputs: Tuple[str, ...]
    data: Dict[str, Any]
    images: Tuple[Dict[str, Any], ...]
    warnings: Tuple[Dict[str, Any], ...]
    diagnostics: Tuple[Dict[str, Any], ...]
    model_bundle_id: str
    error: Optional[Dict[str, Any]]


def _utc_now():
    # type: () -> datetime
    return datetime.now(timezone.utc)


def _utc_text(value):
    # type: (datetime) -> str
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _diagnostic(
    step,  # type: str
    status,  # type: str
    started_at,  # type: datetime
    started_counter,  # type: float
    error_code=None,  # type: Optional[str]
    safe_message=None,  # type: Optional[str]
):
    # type: (...) -> Dict[str, Any]
    completed_at = _utc_now()
    duration_ms = max(
        0.0, round((perf_counter() - started_counter) * 1000.0, 3)
    )
    return {
        "step": step,
        "status": status,
        "started_at": _utc_text(started_at),
        "completed_at": _utc_text(completed_at),
        "duration_ms": duration_ms,
        "error_code": error_code,
        "safe_error_message": safe_message,
    }


def _runtime_error(
    code,  # type: str
    message,  # type: str
    failed_step,  # type: str
):
    # type: (...) -> Dict[str, Any]
    return safe_error(
        code=code,
        safe_message=message,
        retryable=False,
        failed_step=failed_step,
        details={},
    )["error"]


def _warning(code, message):
    # type: (str, str) -> Dict[str, Any]
    return {"code": code, "safe_message": message}


def _validated_image_payload(image, sem_requested):
    # type: (Any, bool) -> Dict[str, Any]
    import numpy as np

    if (
        not isinstance(image, np.ndarray)
        or image.shape != (IMG_SIZE, IMG_SIZE)
        or image.ndim != 2
        or image.dtype.str != "<f4"
        or not image.flags.c_contiguous
        or image.flags.f_contiguous
        or not np.isfinite(image).all()
        or float(image.min()) < -1.0
        or float(image.max()) > 1.0
    ):
        raise InvalidModelOutputError()
    buffer = BytesIO()
    np.save(buffer, image, allow_pickle=False)
    payload = buffer.getvalue()
    return {
        "image_role": (
            "generated_sem" if sem_requested else "intermediate_sem"
        ),
        "requested_output": sem_requested,
        "dtype": "float32",
        "numpy_dtype": "<f4",
        "shape": [IMG_SIZE, IMG_SIZE],
        "channel_layout": "GRAYSCALE_2D",
        "value_range": [-1.0, 1.0],
        "encoding": "base64+npy",
        "byte_order": "little",
        "array_order": "C",
        "sha256": sha256(payload).hexdigest(),
        "data_base64": b64encode(payload).decode("ascii"),
    }


def _validated_performance(values):
    # type: (Any) -> Dict[str, Any]
    if (
        not isinstance(values, (tuple, list))
        or len(values) != 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, Real)
            or not math.isfinite(float(item))
            for item in values
        )
    ):
        raise MechanicalPredictionError()
    return {
        "yield_strength": {
            "value": float(values[0]),
            "unit": "MPa",
        },
        "elongation": {
            "value": float(values[1]),
            "unit": "%",
        },
    }


class ZTA35GInferenceEngine:
    def __init__(self, components):
        # type: (Any) -> None
        self._components = components
        self._loaded = False
        self._closed = False
        self._load_failed = False
        self._components_closed = False
        self._load_lock = Lock()

    def load(self):
        # type: () -> None
        with self._load_lock:
            if self._loaded:
                return
            if self._closed:
                raise EngineLoadError("Inference engine is closed.")
            if self._load_failed:
                raise EngineLoadError("Model bundle could not be loaded.")
            try:
                self._components.load()
            except Exception:
                self._load_failed = True
                self._close_components_once()
                raise EngineLoadError("Model bundle could not be loaded.") from None
            self._loaded = True

    def is_loaded(self):
        # type: () -> bool
        return self._loaded

    @property
    def model_bundle_id(self):
        # type: () -> Any
        return getattr(self._components, "model_bundle_id", None)

    @property
    def device_kind(self):
        # type: () -> str
        if not self._loaded:
            return "unknown"
        value = getattr(self._components, "device_kind", "unknown")
        return value if value in ("cuda", "cpu") else "unknown"

    def execute(
        self,
        process_parameters,  # type: Mapping[str, Any]
        requested_outputs,  # type: Sequence[str]
        runtime_parameters,  # type: Mapping[str, Any]
    ):
        # type: (...) -> InferenceResult
        if not self._loaded:
            raise InferenceNotLoadedError("Inference engine is not loaded.")
        requested = tuple(requested_outputs)
        sem_requested = "sem_image" in requested
        mechanical_requested = "mechanical_properties" in requested
        diagnostics = []
        warnings = []

        sem_started = _utc_now()
        sem_counter = perf_counter()
        try:
            image = self._components.generate_sem(
                process_parameters,
                runtime_parameters["seed"],
                runtime_parameters["num_samples"],
                runtime_parameters["guide_scale"],
                runtime_parameters["timesteps"],
            )
            image_payload = _validated_image_payload(
                image, sem_requested
            )
        except InvalidModelOutputError:
            message = "Model returned an invalid SEM image."
            diagnostics.append(
                _diagnostic(
                    "sem_generation",
                    "FAILED",
                    sem_started,
                    sem_counter,
                    "INVALID_MODEL_OUTPUT",
                    message,
                )
            )
            return InferenceResult(
                status="FAILED",
                completed_outputs=(),
                failed_outputs=requested,
                data={},
                images=(),
                warnings=(),
                diagnostics=tuple(diagnostics),
                model_bundle_id=self.model_bundle_id,
                error=_runtime_error(
                    "INVALID_MODEL_OUTPUT",
                    message,
                    "sem_generation",
                ),
            )
        except Exception:
            message = "SEM generation failed."
            diagnostics.append(
                _diagnostic(
                    "sem_generation",
                    "FAILED",
                    sem_started,
                    sem_counter,
                    "SEM_GENERATION_FAILED",
                    message,
                )
            )
            return InferenceResult(
                status="FAILED",
                completed_outputs=(),
                failed_outputs=requested,
                data={},
                images=(),
                warnings=(),
                diagnostics=tuple(diagnostics),
                model_bundle_id=self.model_bundle_id,
                error=_runtime_error(
                    "SEM_GENERATION_FAILED",
                    message,
                    "sem_generation",
                ),
            )

        diagnostics.append(
            _diagnostic(
                "sem_generation",
                "SUCCEEDED",
                sem_started,
                sem_counter,
            )
        )
        images = (image_payload,)
        if not mechanical_requested:
            return InferenceResult(
                status="SUCCEEDED",
                completed_outputs=requested,
                failed_outputs=(),
                data={},
                images=images,
                warnings=(),
                diagnostics=tuple(diagnostics),
                model_bundle_id=self.model_bundle_id,
                error=None,
            )

        performance_started = _utc_now()
        performance_counter = perf_counter()
        try:
            performance = self._components.predict_mechanical(
                process_parameters, image
            )
            data = _validated_performance(performance)
        except Exception:
            message = "Mechanical property prediction failed."
            error_code = "MECHANICAL_PROPERTY_PREDICTION_FAILED"
            diagnostics.append(
                _diagnostic(
                    "mechanical_property_prediction",
                    "FAILED",
                    performance_started,
                    performance_counter,
                    error_code,
                    message,
                )
            )
            warnings.append(_warning(error_code, message))
            warnings = warnings[:MAX_WARNINGS]
            completed = ("sem_image",) if sem_requested else ()
            return InferenceResult(
                status=(
                    "PARTIALLY_SUCCEEDED"
                    if completed
                    else "FAILED"
                ),
                completed_outputs=completed,
                failed_outputs=("mechanical_properties",),
                data={},
                images=images,
                warnings=tuple(warnings),
                diagnostics=tuple(diagnostics),
                model_bundle_id=self.model_bundle_id,
                error=_runtime_error(
                    error_code,
                    message,
                    "mechanical_property_prediction",
                ),
            )

        diagnostics.append(
            _diagnostic(
                "mechanical_property_prediction",
                "SUCCEEDED",
                performance_started,
                performance_counter,
            )
        )
        return InferenceResult(
            status="SUCCEEDED",
            completed_outputs=requested,
            failed_outputs=(),
            data=data,
            images=images,
            warnings=(),
            diagnostics=tuple(diagnostics),
            model_bundle_id=self.model_bundle_id,
            error=None,
        )

    def close(self):
        # type: () -> None
        with self._load_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._close_components_once()
            finally:
                self._loaded = False

    def _close_components_once(self):
        # type: () -> None
        if self._components_closed:
            return
        self._components_closed = True
        self._components.close()


class TorchZTA35GComponents:
    """Real model components; heavy imports and weight reads happen in load."""

    model_bundle_id = MODEL_BUNDLE_ID

    def __init__(self, model_root):
        # type: (Path) -> None
        self._model_root = Path(model_root)
        self._bundle = None  # type: Optional[Any]
        self._torch = None  # type: Optional[Any]
        self._numpy = None  # type: Optional[Any]
        self._device = None  # type: Optional[Any]
        self._betas = None  # type: Optional[Any]
        self._alphas = None  # type: Optional[Any]
        self._alphas_cumprod = None  # type: Optional[Any]
        self._load_failed = False

    @property
    def device_kind(self):
        # type: () -> str
        if self._device is None:
            return "unknown"
        kind = getattr(self._device, "type", str(self._device))
        return kind if kind in ("cuda", "cpu") else "unknown"

    def load(self):
        # type: () -> None
        if self._bundle is not None:
            return
        if self._load_failed:
            raise EngineLoadError("Model bundle could not be loaded.")
        import numpy as np
        import torch

        bundle = None
        try:
            bundle = ModelBundleLoader(self._model_root).load()
            device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu"
            )
            bundle.ddpm.to(device).eval()
            bundle.densenet.to(device).eval()
            betas = cosine_beta_schedule(torch, TIMESTEPS).to(device)
            alphas = (1.0 - betas).to(device)
            alphas_cumprod = torch.cumprod(
                alphas, dim=0
            ).to(device)
        except Exception:
            self._load_failed = True
            if bundle is not None:
                try:
                    bundle.close()
                except Exception:
                    pass
            self._clear_loaded_state()
            raise
        self._bundle = bundle
        self._torch = torch
        self._numpy = np
        self._device = device
        self._betas = betas
        self._alphas = alphas
        self._alphas_cumprod = alphas_cumprod

    def _clear_loaded_state(self):
        # type: () -> None
        self._bundle = None
        self._torch = None
        self._numpy = None
        self._device = None
        self._betas = None
        self._alphas = None
        self._alphas_cumprod = None

    def _require_loaded(self):
        # type: () -> None
        if (
            self._bundle is None
            or self._torch is None
            or self._numpy is None
            or self._device is None
        ):
            raise InferenceNotLoadedError()

    def generate_sem(
        self,
        process_parameters,  # type: Mapping[str, Any]
        seed,  # type: int
        num_samples,  # type: int
        guide_scale,  # type: float
        timesteps,  # type: int
    ):
        # type: (...) -> Any
        self._require_loaded()
        torch = self._torch
        np = self._numpy
        if (
            num_samples != NUM_SAMPLES
            or guide_scale != GUIDE_SCALE
            or timesteps != TIMESTEPS
        ):
            raise SemGenerationError()
        process_values = np.array(
            [
                process_parameters[name]
                for name in PROCESS_PARAMETER_ORDER
            ],
            dtype=np.float32,
        )
        train_min = np.array(
            [900.0, 1.0, 670.0, 1.0], dtype=np.float32
        )
        train_max = np.array(
            [1100.0, 5.0, 790.0, 5.0], dtype=np.float32
        )
        normalized = (process_values - train_min) / (
            train_max - train_min
        )
        condition = torch.tensor(
            normalized, device=self._device
        ).float().unsqueeze(0)
        unconditional = self._bundle.ddpm.null_condition.unsqueeze(
            0
        ).expand(num_samples, -1)
        generator = torch.Generator(device=self._device)
        generator.manual_seed(seed)
        values = torch.randn(
            (num_samples, 1, IMG_SIZE, IMG_SIZE),
            device=self._device,
            generator=generator,
        )
        with torch.no_grad():
            for index in range(timesteps - 1, -1, -1):
                timestep = torch.full(
                    (num_samples,),
                    index,
                    device=self._device,
                    dtype=torch.long,
                )
                conditional_noise = self._bundle.ddpm(
                    values, timestep, condition
                )
                unconditional_noise = self._bundle.ddpm(
                    values, timestep, unconditional
                )
                predicted_noise = unconditional_noise + guide_scale * (
                    conditional_noise - unconditional_noise
                )
                alpha = self._alphas[index]
                alpha_hat = self._alphas_cumprod[index]
                beta = self._betas[index]
                noise = (
                    torch.randn(
                        values.shape,
                        dtype=values.dtype,
                        device=self._device,
                        generator=generator,
                    )
                    if index > 0
                    else torch.zeros_like(values)
                )
                sqrt_alpha = torch.sqrt(
                    torch.clamp(alpha, min=1e-5)
                )
                sqrt_one_minus_alpha_hat = torch.sqrt(
                    torch.clamp(1.0 - alpha_hat, min=1e-5)
                )
                sqrt_alpha_hat = torch.sqrt(
                    torch.clamp(alpha_hat, min=1e-5)
                )
                predicted_x0 = (
                    values
                    - sqrt_one_minus_alpha_hat * predicted_noise
                ) / sqrt_alpha_hat
                predicted_x0 = torch.clamp(
                    predicted_x0, min=-1.0, max=1.0
                )
                safe_noise = (
                    values - sqrt_alpha_hat * predicted_x0
                ) / sqrt_one_minus_alpha_hat
                values = (1 / sqrt_alpha) * (
                    values
                    - (
                        (1 - alpha)
                        / sqrt_one_minus_alpha_hat
                    )
                    * safe_noise
                ) + torch.sqrt(beta) * noise
                if torch.isnan(values).any() or torch.isinf(values).any():
                    raise InvalidModelOutputError()
            values = torch.clamp(values, -1.0, 1.0)
        return (
            values[0, 0]
            .detach()
            .to("cpu")
            .contiguous()
            .numpy()
            .astype("<f4", copy=False)
        )

    def predict_mechanical(self, process_parameters, image):
        # type: (Mapping[str, Any], Any) -> Tuple[float, float]
        self._require_loaded()
        torch = self._torch
        np = self._numpy
        tensor = torch.from_numpy(image).to(self._device)
        tensor = tensor.unsqueeze(0).unsqueeze(0)
        tensor = (tensor + 1.0) / 2.0
        tensor = torch.clamp(tensor, 0.0, 1.0).repeat(
            1, 3, 1, 1
        )
        mean = torch.tensor(
            [0.485, 0.456, 0.406],
            device=self._device,
        ).view(1, 3, 1, 1)
        standard_deviation = torch.tensor(
            [0.229, 0.224, 0.225],
            device=self._device,
        ).view(1, 3, 1, 1)
        tensor = (tensor - mean) / standard_deviation
        with torch.no_grad():
            raw = self._bundle.densenet(tensor)
            spatial = torch.nn.functional.relu(raw, inplace=False)
            average = torch.nn.functional.adaptive_avg_pool2d(
                spatial, (1, 1)
            ).flatten(1)
            maximum = torch.nn.functional.adaptive_max_pool2d(
                spatial, (1, 1)
            ).flatten(1)
            minimum = -torch.nn.functional.adaptive_max_pool2d(
                -spatial, (1, 1)
            ).flatten(1)
        image_features = torch.cat(
            (average, maximum, minimum), dim=1
        ).mean(dim=0).cpu().numpy()
        process_values = np.array(
            [
                process_parameters[name]
                for name in PROCESS_PARAMETER_ORDER
            ],
            dtype=np.float32,
        )
        model_input = np.concatenate(
            (
                process_values.reshape(1, -1),
                image_features.reshape(1, -1),
            ),
            axis=1,
        )
        yield_strength = self._bundle.yield_model.predict(
            model_input
        )[0]
        elongation = self._bundle.elongation_model.predict(
            model_input
        )[0]
        return float(yield_strength), float(elongation)

    def close(self):
        # type: () -> None
        if self._bundle is not None:
            self._bundle.close()
        self._clear_loaded_state()
