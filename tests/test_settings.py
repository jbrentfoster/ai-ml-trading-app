"""
Unit tests for config/settings.py YAML loading.

Mostly covers the unknown-key/section warnings — without those, a typo in
settings.yaml silently drops the intended override and the symptom only
surfaces later (e.g. 2026-05-07 found `min_trades_for_realised_kellly` with
three L's had been ignored for days).
"""

from __future__ import annotations

import pytest

from config.settings import AppConfig, _apply_yaml_section, load_yaml_config


class TestApplyYamlSection:

    def test_known_field_overrides_default(self):
        cfg = AppConfig()
        _apply_yaml_section(cfg.risk, {"kelly_fraction": 0.5}, section_name="risk")
        assert cfg.risk.kelly_fraction == 0.5

    def test_unknown_key_warns_and_is_ignored(self):
        """Typo in a YAML key must produce a UserWarning and not raise."""
        cfg = AppConfig()
        original = cfg.risk.min_trades_for_realised_kelly
        with pytest.warns(UserWarning, match=r"Unknown YAML key 'risk\.min_trades_for_realised_kellly'"):
            _apply_yaml_section(
                cfg.risk,
                {"min_trades_for_realised_kellly": 5},   # three L's — the real 2026-05-07 typo
                section_name="risk",
            )
        # Real field is untouched
        assert cfg.risk.min_trades_for_realised_kelly == original

    def test_mix_of_known_and_unknown_keys(self):
        """Known keys still apply; unknown ones warn."""
        cfg = AppConfig()
        with pytest.warns(UserWarning, match=r"Unknown YAML key 'risk\.bogus_setting'"):
            _apply_yaml_section(
                cfg.risk,
                {"kelly_fraction": 0.33, "bogus_setting": True},
                section_name="risk",
            )
        assert cfg.risk.kelly_fraction == 0.33

    def test_no_warning_without_section_name(self):
        """Without section_name, the warning text just shows the bare key."""
        cfg = AppConfig()
        with pytest.warns(UserWarning, match=r"Unknown YAML key 'bogus'"):
            _apply_yaml_section(cfg.risk, {"bogus": 1})


class TestLoadYamlConfig:

    def test_unknown_section_warns(self, tmp_path, monkeypatch):
        """Top-level section typo (e.g. 'mlmodels:' instead of 'ml:') must warn."""
        yaml_path = tmp_path / "settings.yaml"
        yaml_path.write_text(
            "mlmodels:\n"
            "  signal_threshold: 0.99\n"
            "risk:\n"
            "  kelly_fraction: 0.42\n"
        )
        from config import settings as settings_mod
        monkeypatch.setattr(settings_mod, "_YAML_PATH", yaml_path)
        # Snapshot + restore so this test doesn't bleed into other tests that
        # share the singleton.
        original = settings_mod.config.risk.kelly_fraction
        try:
            with pytest.warns(UserWarning, match=r"Unknown YAML section 'mlmodels'"):
                load_yaml_config()
            # Valid section under same load still applied
            assert settings_mod.config.risk.kelly_fraction == pytest.approx(0.42)
        finally:
            settings_mod.config.risk.kelly_fraction = original

    def test_missing_file_is_silent(self, tmp_path, monkeypatch):
        """No YAML file → load_yaml_config is a silent no-op."""
        from config import settings as settings_mod
        monkeypatch.setattr(settings_mod, "_YAML_PATH", tmp_path / "does_not_exist.yaml")
        # No warning, no exception
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")     # any warning here would raise
            load_yaml_config()                  # must not raise
