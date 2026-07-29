from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .constants import MODEL_BUNDLE_ID


@dataclass(frozen=True)
class BundleFileSpec:
    relative_path: str
    size_bytes: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class LoadedObject:
    value: Any
    summary: Dict[str, int]


class ModelBundleError(RuntimeError):
    """Safe model bundle validation or loading failure."""


BUNDLE_FILE_SPECS = (
    BundleFileSpec(
        "ddpm_512_epoch_800.pth",
        1999585053,
        "a9c6b369e05a6a330b4ab4587060a51c909c350d58fef8e1dcfcf3937a879dd3",
        "ddpm",
    ),
    BundleFileSpec(
        "densenet121-a639ec97.pth",
        32342954,
        "a639ec97d7c33b07ae66f0b5fb7d0192f95a3b11b7576c66c0126c2a727c4395",
        "densenet",
    ),
    BundleFileSpec(
        "SVR_model/Final_Yield_Strength.pkl",
        171661,
        "547c4794329756dcb979518fab921abf599a307ae304bef9bb42e90f3d5945d3",
        "yield_svr",
    ),
    BundleFileSpec(
        "SVR_model/Final_Elongation.pkl",
        14061,
        "debe460fb230a4c5a64dcd8a1882478ef373c71619b9efe580d5e2f7008c6c53",
        "elongation_svr",
    ),
)  # type: Tuple[BundleFileSpec, ...]

_BUNDLE_KINDS = frozenset(
    ("ddpm", "densenet", "yield_svr", "elongation_svr")
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DENSENET_LEGACY_PATTERN = re.compile(
    r"^(.*denselayer\d+\.(?:norm|relu|conv))\.([12])\."
    r"(weight|bias|running_mean|running_var)$"
)


def select_ddpm_state(checkpoint):
    # type: (Any) -> Mapping[str, Any]
    if not isinstance(checkpoint, Mapping):
        raise ModelBundleError("DDPM checkpoint format is invalid.")
    ema = checkpoint.get("ema")
    model = checkpoint.get("model")
    if isinstance(ema, Mapping):
        return ema
    if isinstance(model, Mapping):
        return model
    return checkpoint


def clean_ddpm_state_keys(state):
    # type: (Mapping[str, Any]) -> Dict[str, Any]
    cleaned = OrderedDict()
    for key, value in state.items():
        if not isinstance(key, str) or not key:
            raise ModelBundleError("DDPM state keys are invalid.")
        clean_key = key
        while clean_key.startswith(("module.", "model.")):
            clean_key = clean_key.split(".", 1)[1]
        if not clean_key or clean_key in cleaned:
            raise ModelBundleError("DDPM state keys are invalid.")
        cleaned[clean_key] = value
    return cleaned


def convert_legacy_densenet_keys(state):
    # type: (Mapping[str, Any]) -> Dict[str, Any]
    converted = OrderedDict()
    for key, value in state.items():
        if not isinstance(key, str) or not key:
            raise ModelBundleError("DenseNet state keys are invalid.")
        match = _DENSENET_LEGACY_PATTERN.match(key)
        new_key = (
            match.group(1) + match.group(2) + "." + match.group(3)
            if match is not None
            else key
        )
        if new_key in converted:
            raise ModelBundleError("DenseNet state keys are invalid.")
        converted[new_key] = value
    return converted


def safe_incompatible_keys_summary(missing_keys, unexpected_keys):
    # type: (Iterable[Any], Iterable[Any]) -> Dict[str, int]
    return {
        "missing_key_count": len(tuple(missing_keys)),
        "unexpected_key_count": len(tuple(unexpected_keys)),
    }


def _validated_load_summary(summary):
    # type: (Any) -> Dict[str, int]
    required = {"missing_key_count", "unexpected_key_count"}
    if not isinstance(summary, Mapping) or set(summary) != required:
        raise ModelBundleError("Model bundle load summary is invalid.")
    normalized = {}  # type: Dict[str, int]
    for field_name in required:
        value = summary.get(field_name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ModelBundleError("Model bundle load summary is invalid.")
        normalized[field_name] = value
    if (
        normalized["missing_key_count"] != 0
        or normalized["unexpected_key_count"] != 0
    ):
        raise ModelBundleError("Model bundle state is incompatible.")
    return normalized


class LoadedModelBundle:
    def __init__(self, loaded):
        # type: (Mapping[str, LoadedObject]) -> None
        self.model_bundle_id = MODEL_BUNDLE_ID
        self.ddpm = loaded["ddpm"].value
        self.densenet = loaded["densenet"].value
        self.yield_model = loaded["yield_svr"].value
        self.elongation_model = loaded["elongation_svr"].value
        self.load_summaries = {
            kind: dict(item.summary) for kind, item in loaded.items()
        }
        self._values = tuple(item.value for item in loaded.values())
        self._closed = False

    def close(self):
        # type: () -> None
        if self._closed:
            return
        self._closed = True
        for value in reversed(self._values):
            close = getattr(value, "close", None)
            if callable(close):
                close()


class _DefaultObjectLoader:
    def load(self, spec, path):
        # type: (BundleFileSpec, Path) -> LoadedObject
        if spec.kind == "ddpm":
            return self._load_ddpm(path)
        if spec.kind == "densenet":
            return self._load_densenet(path)
        if spec.kind in ("yield_svr", "elongation_svr"):
            return self._load_svr(path)
        raise ModelBundleError("Model bundle kind is invalid.")

    @staticmethod
    def _load_ddpm(path):
        # type: (Path) -> LoadedObject
        import torch

        from .model_architecture import build_conditional_unet

        checkpoint = torch.load(str(path), map_location="cpu")
        state = clean_ddpm_state_keys(select_ddpm_state(checkpoint))
        model = build_conditional_unet()
        incompatible = model.load_state_dict(state, strict=False)
        model.eval()
        return LoadedObject(
            model,
            safe_incompatible_keys_summary(
                incompatible.missing_keys,
                incompatible.unexpected_keys,
            ),
        )

    @staticmethod
    def _load_densenet(path):
        # type: (Path) -> LoadedObject
        import torch
        import torchvision.models as models

        model = models.densenet121(weights=None)
        state = torch.load(str(path), map_location="cpu")
        if not isinstance(state, Mapping):
            raise ModelBundleError("DenseNet checkpoint format is invalid.")
        converted = convert_legacy_densenet_keys(state)
        incompatible = model.load_state_dict(converted, strict=True)
        features = model.features
        features.eval()
        return LoadedObject(
            features,
            safe_incompatible_keys_summary(
                incompatible.missing_keys,
                incompatible.unexpected_keys,
            ),
        )

    @staticmethod
    def _load_svr(path):
        # type: (Path) -> LoadedObject
        import joblib

        model = joblib.load(str(path))
        return LoadedObject(
            model,
            {
                "missing_key_count": 0,
                "unexpected_key_count": 0,
            },
        )


class ModelBundleLoader:
    def __init__(
        self,
        model_root,  # type: Path
        object_loader=None,  # type: Optional[Any]
        file_specs=None,  # type: Optional[Sequence[BundleFileSpec]]
    ):
        # type: (...) -> None
        self._root = Path(model_root)
        self._object_loader = object_loader or _DefaultObjectLoader()
        self._specs = tuple(
            BUNDLE_FILE_SPECS if file_specs is None else file_specs
        )
        self._validate_spec_set()

    def _validate_spec_set(self):
        # type: () -> None
        relative_paths = []
        kinds = []
        for spec in self._specs:
            if not isinstance(spec, BundleFileSpec):
                raise ModelBundleError("Model bundle definition is invalid.")
            path = PurePosixPath(spec.relative_path)
            if (
                not spec.relative_path
                or "\\" in spec.relative_path
                or path.is_absolute()
                or path.as_posix() != spec.relative_path
                or any(part in ("", ".", "..") for part in path.parts)
                or isinstance(spec.size_bytes, bool)
                or not isinstance(spec.size_bytes, int)
                or spec.size_bytes < 0
                or _SHA256_PATTERN.fullmatch(spec.sha256) is None
                or spec.kind not in _BUNDLE_KINDS
            ):
                raise ModelBundleError("Model bundle definition is invalid.")
            relative_paths.append(spec.relative_path)
            kinds.append(spec.kind)
        if (
            len(self._specs) != 4
            or len(set(relative_paths)) != 4
            or set(kinds) != _BUNDLE_KINDS
        ):
            raise ModelBundleError("Model bundle definition is invalid.")

    def _validate_files(self):
        # type: () -> Tuple[Tuple[BundleFileSpec, Path], ...]
        if not self._root.is_dir():
            raise ModelBundleError("Model bundle root is unavailable.")
        validated = []
        for spec in self._specs:
            path = self._root.joinpath(*PurePosixPath(spec.relative_path).parts)
            try:
                if not path.is_file() or path.is_symlink():
                    raise ModelBundleError(
                        "%s is unavailable." % spec.relative_path
                    )
                if path.stat().st_size != spec.size_bytes:
                    raise ModelBundleError(
                        "%s has an unexpected size." % spec.relative_path
                    )
                digest = sha256()
                with path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != spec.sha256:
                    raise ModelBundleError(
                        "%s has an unexpected digest." % spec.relative_path
                    )
            except ModelBundleError:
                raise
            except (OSError, ValueError):
                raise ModelBundleError(
                    "%s could not be validated." % spec.relative_path
                ) from None
            validated.append((spec, path))
        return tuple(validated)

    def load(self):
        # type: () -> LoadedModelBundle
        validated = self._validate_files()
        loaded = OrderedDict()  # type: Dict[str, LoadedObject]
        try:
            for spec, path in validated:
                item = self._object_loader.load(spec, path)
                if not isinstance(item, LoadedObject):
                    raise ModelBundleError(
                        "%s could not be loaded." % spec.relative_path
                    )
                try:
                    summary = _validated_load_summary(item.summary)
                except ModelBundleError:
                    self._close_partial((item,))
                    raise
                loaded[spec.kind] = LoadedObject(
                    value=item.value,
                    summary=summary,
                )
        except ModelBundleError:
            self._close_partial(loaded.values())
            raise
        except Exception:
            self._close_partial(loaded.values())
            failed_name = spec.relative_path
            raise ModelBundleError(
                "%s could not be loaded." % failed_name
            ) from None
        return LoadedModelBundle(loaded)

    @staticmethod
    def _close_partial(items):
        # type: (Iterable[LoadedObject]) -> None
        for item in reversed(tuple(items)):
            close = getattr(item.value, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
