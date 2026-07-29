import ast
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


def _method_structure_digest(class_node, method_name):
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    structure = ast.dump(method, include_attributes=False)
    return sha256(structure.encode("utf-8")).hexdigest()


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
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
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
    # These formatting-independent AST digests were reviewed line by line
    # against the read-only original. They lock every layer constructor,
    # channel/kernel argument, loop, and forward mathematical operation/order.
    expected_method_structures = {
        "SinusoidalPositionEmbeddings": {
            "__init__": (
                "529b4e73d767f456ffe1793cd47298ae707c2bf781308293f"
                "c937404e6e078c3"
            ),
            "forward": (
                "a3149629ae44bb6569605eb452007c765909de3822c161e074"
                "7a06ac1797f75f"
            ),
        },
        "ConditionEmbeddings": {
            "__init__": (
                "d70e7572ef642bb8597f4a97596d662d7e17200acc3a73a8"
                "55f6fe4c0850d5a5"
            ),
            "forward": (
                "2900cef70b28158eb1335a9eaaae1dd3111823bf3e804ae2b"
                "58c8fd4f564ad61"
            ),
        },
        "ResidualBlock": {
            "__init__": (
                "d615123aaad519b368acbfa5447eeea661c2ae03fb4e873b3"
                "4ec4216e3ebbd3b"
            ),
            "forward": (
                "82330c77a2175207690c99f443e6225cb3a753e4f73bbb629"
                "722b14dbef53eb5"
            ),
        },
        "SelfAttention": {
            "__init__": (
                "2e4810bcc6c6f0f00c157420dbd120d888b923e978b6dbc5"
                "e3001c1270acb3d9"
            ),
            "forward": (
                "e0e050c7f6a8c1cdc151fcc454c036bb87a120829f3c96706"
                "20c1d2b93794168"
            ),
        },
        "DownSample": {
            "__init__": (
                "90f0e453b13793701765bf4d42aa65519c5509de9f0c193de"
                "7de270a4919e7ac"
            ),
            "forward": (
                "1b251b4ed25d21bc35a794bb252323d788ed43916fe524b95"
                "4fa33413193a14f"
            ),
        },
        "UpSample": {
            "__init__": (
                "252adfbc178c4615f2fd4414cc5e4d8d87cb95518ef69d3c"
                "78f2fd84af38a917"
            ),
            "forward": (
                "1b251b4ed25d21bc35a794bb252323d788ed43916fe524b95"
                "4fa33413193a14f"
            ),
        },
        "ConditionalUNet": {
            "__init__": (
                "0a14751fb68360782efaf155cde83b64945a7bfbddbd3325b4"
                "19a261ed1b2dd6"
            ),
            "forward": (
                "77c948afab948136a2f2ec2010e053f169251ec56b3e46cbc"
                "44c84e139599f95"
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
                classes[class_name], method_name
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
