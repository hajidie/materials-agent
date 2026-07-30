import ast
import io
import json
import tokenize
from hashlib import sha256
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from materialsagent_zta35g_runtime.model_bundle import (
    BUNDLE_FILE_SPECS,
    BundleFileSpec,
    LoadedObject,
    ModelBundleError,
    ModelBundleLoader,
    _DefaultObjectLoader,
    clean_ddpm_state_keys,
    convert_legacy_densenet_keys,
    safe_incompatible_keys_summary,
    select_ddpm_state,
)


class _FakeLoader:
    def __init__(self, summaries=None, fail_kind=None):
        self.calls = []
        self.closed = []
        self.summaries = dict(summaries or {})
        self.fail_kind = fail_kind

    def load(self, spec, path):
        self.calls.append((spec.relative_path, path.name))
        if spec.kind == self.fail_kind:
            raise RuntimeError(
                "private.key.name C:\\private\\absolute\\weight"
            )
        return LoadedObject(
            value=_Closable(spec.relative_path, self.closed),
            summary=self.summaries.get(
                spec.kind,
                {
                    "missing_key_count": 0,
                    "unexpected_key_count": 0,
                },
            ),
        )


class _Closable:
    def __init__(self, name, closed):
        self.name = name
        self.closed = closed

    def close(self):
        self.closed.append(self.name)


def _spec(relative_path, payload, kind):
    return BundleFileSpec(
        relative_path=relative_path,
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        kind=kind,
    )


def _valid_bundle(tmp_path):
    payloads = {
        "ddpm.pth": b"ddpm",
        "dense.pth": b"dense",
        "models/yield.pkl": b"yield",
        "models/elongation.pkl": b"elongation",
    }
    specs = (
        _spec("ddpm.pth", payloads["ddpm.pth"], "ddpm"),
        _spec("dense.pth", payloads["dense.pth"], "densenet"),
        _spec("models/yield.pkl", payloads["models/yield.pkl"], "yield_svr"),
        _spec(
            "models/elongation.pkl",
            payloads["models/elongation.pkl"],
            "elongation_svr",
        ),
    )
    for relative_path, payload in payloads.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return specs


def test_fixed_bundle_metadata_matches_confirmed_manifest():
    facts = {
        spec.relative_path: (spec.size_bytes, spec.sha256)
        for spec in BUNDLE_FILE_SPECS
    }
    assert facts == {
        "ddpm_512_epoch_800.pth": (
            1999585053,
            "a9c6b369e05a6a330b4ab4587060a51c909c350d58fef8e1dcfcf3937a879dd3",
        ),
        "densenet121-a639ec97.pth": (
            32342954,
            "a639ec97d7c33b07ae66f0b5fb7d0192f95a3b11b7576c66c0126c2a727c4395",
        ),
        "SVR_model/Final_Yield_Strength.pkl": (
            171661,
            "547c4794329756dcb979518fab921abf599a307ae304bef9bb42e90f3d5945d3",
        ),
        "SVR_model/Final_Elongation.pkl": (
            14061,
            "debe460fb230a4c5a64dcd8a1882478ef373c71619b9efe580d5e2f7008c6c53",
        ),
    }


def test_missing_size_and_hash_failures_call_no_object_loader(tmp_path):
    specs = _valid_bundle(tmp_path)
    loader = _FakeLoader()

    (tmp_path / specs[0].relative_path).unlink()
    with pytest.raises(ModelBundleError) as missing:
        ModelBundleLoader(tmp_path, loader, specs).load()
    assert specs[0].relative_path in str(missing.value)
    assert str(tmp_path) not in str(missing.value)
    assert loader.calls == []

    specs = _valid_bundle(tmp_path)
    (tmp_path / specs[1].relative_path).write_bytes(b"x")
    with pytest.raises(ModelBundleError) as wrong_size:
        ModelBundleLoader(tmp_path, loader, specs).load()
    assert specs[1].relative_path in str(wrong_size.value)
    assert str(tmp_path) not in str(wrong_size.value)
    assert loader.calls == []

    specs = _valid_bundle(tmp_path)
    expected_size = specs[2].size_bytes
    (tmp_path / specs[2].relative_path).write_bytes(b"x" * expected_size)
    with pytest.raises(ModelBundleError) as wrong_hash:
        ModelBundleLoader(tmp_path, loader, specs).load()
    assert specs[2].relative_path in str(wrong_hash.value)
    assert str(tmp_path) not in str(wrong_hash.value)
    assert loader.calls == []


def test_only_exact_safe_unique_relative_file_spec_set_is_accepted(tmp_path):
    specs = _valid_bundle(tmp_path)
    unsafe = BundleFileSpec("../outside", 1, "0" * 64, "ddpm")

    for invalid_specs in (
        specs + (specs[0],),
        specs[:-1],
        specs[:-1] + (unsafe,),
        specs[:-1]
        + (BundleFileSpec("extra.bin", 1, "0" * 64, "unknown"),),
    ):
        with pytest.raises(ModelBundleError):
            ModelBundleLoader(tmp_path, _FakeLoader(), invalid_specs)


def test_all_files_validate_before_loader_and_loaded_bundle_closes(tmp_path):
    specs = _valid_bundle(tmp_path)
    object_loader = _FakeLoader()

    bundle = ModelBundleLoader(tmp_path, object_loader, specs).load()

    assert [item[0] for item in object_loader.calls] == [
        spec.relative_path for spec in specs
    ]
    assert bundle.model_bundle_id == "zta35g-sem-original-bundle"
    assert bundle.load_summaries["ddpm"]["missing_key_count"] == 0
    bundle.close()
    bundle.close()
    assert sorted(object_loader.closed) == sorted(
        spec.relative_path for spec in specs
    )


def _assigned_self_attributes(class_node):
    attributes = set()
    for node in ast.walk(class_node):
        targets = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        elif isinstance(node, ast.AugAssign):
            targets.append(node.target)
        for target in targets:
            for item in ast.walk(target):
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.value, ast.Name)
                    and item.value.id == "self"
                ):
                    attributes.add(item.attr)
    return attributes


_IGNORED_TOKEN_NAMES = {
    "ENCODING",
    "NL",
    "NEWLINE",
    "INDENT",
    "DEDENT",
    "COMMENT",
    "ENDMARKER",
}


def _method_structure_digest(source, class_node, method_name):
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )

    segment = ast.get_source_segment(source, method)
    assert segment is not None

    semantic_tokens = []
    for token in tokenize.tokenize(
        io.BytesIO(segment.encode("utf-8")).readline
    ):
        token_name = tokenize.tok_name[token.type]
        if token_name in _IGNORED_TOKEN_NAMES:
            continue
        semantic_tokens.append((token_name, token.string))

    payload = json.dumps(
        semantic_tokens,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _init_defaults(class_node):
    initializer = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    arguments = initializer.args.args[1:]
    defaults = initializer.args.defaults
    names = arguments[len(arguments) - len(defaults) :]
    return {
        argument.arg: ast.literal_eval(default)
        for argument, default in zip(names, defaults)
    }


def test_ddpm_architecture_preserves_original_state_dict_module_names():
    source_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "materialsagent_zta35g_runtime"
        / "model_architecture.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }

    expected_attributes = {
        "SinusoidalPositionEmbeddings": {"dim"},
        "ConditionEmbeddings": {"emb_dim", "half_dim", "mlp"},
        "ConditionalUNet": {
            "time_mlp",
            "cond_mlp",
            "null_condition",
            "conv0",
            "downs",
            "mid_block1",
            "mid_attn",
            "mid_block2",
            "ups",
            "output",
        },
        "ResidualBlock": {
            "time_mlp",
            "cond_mlp",
            "conv1",
            "conv2",
            "bnorm1",
            "bnorm2",
            "relu",
            "dropout",
            "shortcut",
        },
        "SelfAttention": {"channels", "mha", "ln", "ff_self"},
        "DownSample": {"op"},
        "UpSample": {"op"},
    }
    expected_defaults = {
        "SinusoidalPositionEmbeddings": {},
        "ConditionEmbeddings": {"input_dim": 4, "emb_dim": 128},
        "ResidualBlock": {"dropout": 0.1},
        "SelfAttention": {},
        "DownSample": {},
        "UpSample": {},
        "ConditionalUNet": {
            "image_channels": 1,
            "down_channels": (64, 128, 256, 512, 1024),
            "up_channels": (1024, 512, 256, 128, 64),
            "time_embedding_dim": 128,
            "condition_input_dim": 4,
            "condition_embedding_dim": 128,
            "dropout_rate": 0.0,
        },
    }
    # These formatting-independent semantic-token digests were reviewed
    # against the read-only original. They lock every layer constructor,
    # channel/kernel argument, loop, and forward mathematical operation/order.
    expected_method_structures = {
        "SinusoidalPositionEmbeddings": {
            "__init__": (
                "5a5b43ecdb6db33e18690bb805b4706486356ce6e52fabe4"
                "46782f8514236eac"
            ),
            "forward": (
                "94886d86cef4d0ce2467b89acfc8446fd33fb38c28e41fff"
                "6495d0e79518b89d"
            ),
        },
        "ConditionEmbeddings": {
            "__init__": (
                "a1cbe9911c9c9abfd432cdf354862519d3bb827ba97dd19f"
                "91e94de92854c974"
            ),
            "forward": (
                "97ca0166902248d054938b0421cdcae8edc60208910380b70"
                "e6076fc6c80e349"
            ),
        },
        "ResidualBlock": {
            "__init__": (
                "dd841306ab5c0fc4ba804ff54af4efca2b34ca959c86a8f"
                "878a3168519ff1942"
            ),
            "forward": (
                "510fd6f40bd93c61bf3a7e37b5d35f9d5dc4c602ab2bad9"
                "1cea56b3b7714c1bf"
            ),
        },
        "SelfAttention": {
            "__init__": (
                "4604925a4159736404f6329a3594d52b0a22be463e22dbd0"
                "427bda3ec266a588"
            ),
            "forward": (
                "9646d5abfeff36368e2b1070a70637ba3f8c5db18d9dc532"
                "741b674ecd54a810"
            ),
        },
        "DownSample": {
            "__init__": (
                "aa380c550f93d93ed493c4f079fe325274579521fa44b4b7a"
                "c145e5dca7ee117"
            ),
            "forward": (
                "e23c5f0afd6daf705306f9684d99ca14e9adfd642e9ad541"
                "d3bc0e0507ce528d"
            ),
        },
        "UpSample": {
            "__init__": (
                "4dc6d342a69b7763527e992142d9bfc71c017c87b4c9556e"
                "2af272c10ed9a007"
            ),
            "forward": (
                "e23c5f0afd6daf705306f9684d99ca14e9adfd642e9ad541"
                "d3bc0e0507ce528d"
            ),
        },
        "ConditionalUNet": {
            "__init__": (
                "25859884856f50d10f4474ec3de71b4b00eb51191e713771"
                "7745d51a4ec2c97c"
            ),
            "forward": (
                "c2872e069a47b3b8d0093482128a25f40ceb7ed1ee7d3c73"
                "dd2f65c1d997cb6d"
            ),
        },
    }
    forbidden = {
        "condition_mlp",
        "initial",
        "middle1",
        "middle_attention",
        "middle2",
        "norm1",
        "norm2",
        "activation",
        "attention",
        "layer_norm",
        "feed_forward",
        "operation",
    }

    assert set(expected_attributes).issubset(classes)
    for class_name, required_attributes in expected_attributes.items():
        actual = _assigned_self_attributes(classes[class_name])
        assert actual == required_attributes
        assert forbidden.isdisjoint(actual)
        assert _init_defaults(classes[class_name]) == expected_defaults[
            class_name
        ]
        assert {
            method_name: _method_structure_digest(
                source,
                classes[class_name],
                method_name,
            )
            for method_name in ("__init__", "forward")
        } == expected_method_structures[class_name]


@pytest.mark.parametrize(
    "summary",
    [
        {"missing_key_count": 1, "unexpected_key_count": 0},
        {"missing_key_count": 0, "unexpected_key_count": 1},
        {"missing_key_count": 2, "unexpected_key_count": 3},
    ],
    ids=("missing", "unexpected", "missing-and-unexpected"),
)
def test_any_state_dict_mismatch_fails_closed_and_closes_loaded_objects(
    tmp_path,
    summary,
):
    specs = _valid_bundle(tmp_path)
    object_loader = _FakeLoader(summaries={"densenet": summary})

    with pytest.raises(ModelBundleError) as raised:
        ModelBundleLoader(tmp_path, object_loader, specs).load()

    assert str(raised.value) == "Model bundle state is incompatible."
    assert object_loader.closed == [
        specs[1].relative_path,
        specs[0].relative_path,
    ]
    assert len(object_loader.closed) == len(set(object_loader.closed))
    assert str(tmp_path) not in str(raised.value)
    assert "private" not in str(raised.value)


def test_loader_exception_is_safely_summarized_and_partial_objects_close(
    tmp_path,
):
    specs = _valid_bundle(tmp_path)
    object_loader = _FakeLoader(fail_kind="densenet")

    with pytest.raises(ModelBundleError) as raised:
        ModelBundleLoader(tmp_path, object_loader, specs).load()

    error_text = str(raised.value)
    assert specs[1].relative_path in error_text
    assert object_loader.closed == [specs[0].relative_path]
    assert str(tmp_path) not in error_text
    assert "private.key.name" not in error_text
    assert "absolute" not in error_text


def test_default_ddpm_loader_collects_mismatch_with_non_strict_loading(
    monkeypatch,
    tmp_path,
):
    strict_arguments = []

    class _Model:
        def load_state_dict(self, _state, strict):
            strict_arguments.append(strict)
            return SimpleNamespace(
                missing_keys=("private.missing",),
                unexpected_keys=("private.unexpected",),
            )

        def eval(self):
            return self

    fake_torch = ModuleType("torch")
    fake_torch.load = lambda _path, map_location: {
        "model": {"module.layer": object()}
    }
    fake_architecture = ModuleType(
        "materialsagent_zta35g_runtime.model_architecture"
    )
    fake_architecture.build_conditional_unet = _Model
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(
        sys.modules,
        "materialsagent_zta35g_runtime.model_architecture",
        fake_architecture,
    )

    loaded = _DefaultObjectLoader._load_ddpm(
        tmp_path / "not-opened.pth"
    )

    assert strict_arguments == [False]
    assert loaded.summary == {
        "missing_key_count": 1,
        "unexpected_key_count": 1,
    }
    assert "private" not in repr(loaded.summary)


def test_default_densenet_loader_uses_strict_state_loading(
    monkeypatch,
    tmp_path,
):
    strict_arguments = []

    class _Features:
        def eval(self):
            return self

    class _Model:
        features = _Features()

        def load_state_dict(self, _state, strict):
            strict_arguments.append(strict)
            return SimpleNamespace(
                missing_keys=(),
                unexpected_keys=(),
            )

    fake_torch = ModuleType("torch")
    fake_torch.load = lambda _path, map_location: {}
    fake_models = ModuleType("torchvision.models")
    fake_models.densenet121 = lambda weights: _Model()
    fake_torchvision = ModuleType("torchvision")
    fake_torchvision.models = fake_models
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torchvision", fake_torchvision)
    monkeypatch.setitem(
        sys.modules,
        "torchvision.models",
        fake_models,
    )

    loaded = _DefaultObjectLoader._load_densenet(
        tmp_path / "not-opened.pth"
    )

    assert strict_arguments == [True]
    assert loaded.summary == {
        "missing_key_count": 0,
        "unexpected_key_count": 0,
    }


def test_ddpm_checkpoint_priority_and_prefix_cleanup():
    assert select_ddpm_state(
        {"ema": {"module.a": 1}, "model": {"b": 2}}
    ) == {"module.a": 1}
    assert select_ddpm_state({"model": {"b": 2}}) == {"b": 2}
    assert select_ddpm_state({"model.a": 1}) == {"model.a": 1}
    assert clean_ddpm_state_keys(
        {
            "module.model.layer.weight": 1,
            "model.layer.bias": 2,
            "normal": 3,
        }
    ) == {
        "layer.weight": 1,
        "layer.bias": 2,
        "normal": 3,
    }


def test_densenet_legacy_key_conversion_and_safe_summary():
    converted = convert_legacy_densenet_keys(
        {
            "features.denseblock1.denselayer1.norm.1.weight": 1,
            "features.denseblock1.denselayer1.conv.2.bias": 2,
            "features.normal": 3,
        }
    )
    assert converted == {
        "features.denseblock1.denselayer1.norm1.weight": 1,
        "features.denseblock1.denselayer1.conv2.bias": 2,
        "features.normal": 3,
    }

    summary = safe_incompatible_keys_summary(
        ["private.absolute.path.weight", "another"],
        ["unexpected.secret"],
    )
    assert summary == {
        "missing_key_count": 2,
        "unexpected_key_count": 1,
    }
    assert "private" not in repr(summary)


def test_bundle_source_contains_no_save_dump_or_overwrite_operations():
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "materialsagent_zta35g_runtime"
        / "model_bundle.py"
    ).read_text(encoding="utf-8")

    assert "torch.save" not in source
    assert "joblib.dump" not in source
    assert "pickle.load" not in source
    assert "'wb'" not in source
    assert '"wb"' not in source
