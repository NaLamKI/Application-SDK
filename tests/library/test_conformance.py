"""The shared manifest cases (`sdk/conformance/manifests/`): the store and the SDK must reach the same verdict."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from appext.manifest import ManifestError, load_manifest

ROOT = Path(__file__).resolve().parents[2] / "conformance" / "manifests"
EXPECTED = json.loads((ROOT / "expected.json").read_text()) if (ROOT / "expected.json").exists() else {}

pytestmark = pytest.mark.skipif(not EXPECTED, reason="sdk/conformance/manifests/expected.json is not there")


def test_every_case_file_is_listed_and_every_listing_has_a_file():
    on_disk = {f"{d.name}/{f.name}" for d in (ROOT / "valid", ROOT / "invalid") for f in d.glob("*.toml")}
    assert on_disk == set(EXPECTED), sorted(on_disk ^ set(EXPECTED))


@pytest.mark.parametrize("case", sorted(k for k, v in EXPECTED.items() if not v))
def test_valid_manifests_are_accepted(case):
    manifest = load_manifest(ROOT / case)
    assert manifest.id and manifest.client_id == f"ext-{manifest.id}"


@pytest.mark.parametrize("case", sorted(k for k, v in EXPECTED.items() if v))
def test_invalid_manifests_fail_with_exactly_the_listed_paths(case):
    with pytest.raises(ManifestError) as err:
        load_manifest(ROOT / case)
    assert sorted(err.value.paths) == sorted(EXPECTED[case]), err.value.errors
    for error in err.value.errors:
        assert error["message"]
