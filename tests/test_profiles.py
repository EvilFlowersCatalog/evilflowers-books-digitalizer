"""PdfProfile recode-arg generation + config-driven profile selection."""

from __future__ import annotations

import pytest

from evilflowers_books_digitalizer.pipeline.profiles import (
    ARCHIVAL,
    DISTRIBUTION,
    PdfProfile,
    profiles_from_config,
)


def test_distribution_uses_jpeg_and_downsamples_background():
    args = DISTRIBUTION.recode_args()
    assert "--mrc-image-format" in args and args[args.index("--mrc-image-format") + 1] == "jpeg"
    assert "--bg-downsample" in args
    assert "-J" not in args  # JPEG path doesn't pass a JPEG2000 implementation


def test_archival_uses_jpeg2000_and_requests_pdfa():
    args = ARCHIVAL.recode_args()
    assert args[args.index("--mrc-image-format") + 1] == "jpeg2000"
    assert "-J" in args
    assert ARCHIVAL.pdfa == "2b"


def test_with_overrides_only_applies_known_fields():
    p = DISTRIBUTION.with_overrides({"bg_downsample": 5, "bogus": 1, "fg_downsample": None})
    assert p.bg_downsample == 5
    assert p.fg_downsample == DISTRIBUTION.fg_downsample  # None override ignored
    assert not hasattr(p, "bogus")


def test_profiles_from_config_defaults_to_both():
    profiles = profiles_from_config({})
    assert [p.name for p in profiles] == ["distribution", "archival"]


def test_profiles_from_config_selects_and_overrides():
    config = {"render": {"outputs": ["distribution"], "distribution": {"bg_downsample": 4}}}
    profiles = profiles_from_config(config)
    assert len(profiles) == 1
    assert profiles[0].bg_downsample == 4


def test_profiles_from_config_rejects_unknown():
    with pytest.raises(ValueError, match="unknown render profile"):
        profiles_from_config({"render": {"outputs": ["nope"]}})


def test_profile_is_frozen():
    with pytest.raises(Exception):
        PdfProfile(name="x").bg_downsample = 3  # type: ignore[misc]
