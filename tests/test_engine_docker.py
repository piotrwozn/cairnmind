"""The database engine in Docker (#272): every Docker call is replaced here, no daemon is needed."""
from __future__ import annotations

import json
import re

import pytest

from cairnmind import config
from cairnmind.config import Settings
from cairnmind.database_mode import MODE_INVALID, describe, engine_mode


def settings_with(tmp_path, **values) -> Settings:
    settings = Settings.load(tmp_path / "data", create=True)
    for name, value in values.items():
        setattr(settings, name, value)
    return settings


# --- The setting and the pinned image ----------------------------------------------------------------------

def test_the_engine_image_is_pinned_by_a_real_digest():
    # Fails until the release job has published the image and its digest replaces the placeholder.
    assert config.ARCADE_IMAGE == "ghcr.io/piotrwozn/cairnmind-arcadedb"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", config.ARCADE_IMAGE_DIGEST)
    assert config.ARCADE_IMAGE_DIGEST != "sha256:" + "0" * 64


def test_an_empty_engine_mode_is_native_and_a_wrong_one_is_refused_naming_it(tmp_path):
    settings = settings_with(tmp_path)
    assert settings.engine_mode == "" and settings.engine_docker_build is False
    assert engine_mode(settings) == "native" and describe(settings)["engine"] == "native"
    saved = json.loads((tmp_path / "data" / "config.json").read_text())
    assert saved["engine_mode"] == "" and saved["engine_docker_build"] is False
    settings.engine_mode = "docker"
    assert engine_mode(settings) == "docker" and describe(settings)["engine"] == "docker"
    settings.engine_mode = "dokker"
    with pytest.raises(ValueError, match="engine_mode in config.json is 'dokker'; it must be native or docker"):
        engine_mode(settings)
    shown = describe(settings)
    assert shown["engine"] == "unknown" and shown["engine_problem"] == MODE_INVALID.format(value="'dokker'")
