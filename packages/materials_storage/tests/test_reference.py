from hashlib import sha256
import pytest
from materials_storage import ObjectStorageRef


def reference(**values):
    return ObjectStorageRef(**{"object_id": "object", "store_id": "primary", "bucket": "ml",
        "storage_key": "ml/v1/object", "sha256": sha256(b"content").hexdigest(),
        "size_bytes": 7, "media_type": "text/csv", **values})


def test_round_trip_and_integrity():
    original = reference(version_id="opaque-version")
    restored = ObjectStorageRef(**original.to_dict())
    assert restored == original
    restored.verify(b"content")
    with pytest.raises(ValueError, match="integrity"):
        restored.verify(b"changed")


@pytest.mark.parametrize("values", [{"storage_key": "../other"}, {"storage_key": "/absolute"},
    {"storage_key": "a\\b"}, {"sha256": None}, {"size_bytes": True}, {"size_bytes": -1},
    {"media_type": "bad"}, {"version_id": 3}, {"contract_version": "unknown"}])
def test_invalid_reference(values):
    with pytest.raises(ValueError):
        reference(**values)
