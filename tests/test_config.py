"""
Tests for core.config — PacsNode and AppConfig.
"""

import json
import os
import platform
from unittest.mock import patch

import pytest

from core.config import (
    AppConfig, PacsNode, TRANSFER_SYNTAXES_NAMES, RETRIEVE_METHODS,
    DEFAULT_CONFIG_FILE, LOCAL_QUERY_LOOPBACK, get_local_ip, local_ip_for,
    default_priority_terms,
)


# ═══════════════════════════════════════════════════════════════════════════
# PacsNode
# ═══════════════════════════════════════════════════════════════════════════

class TestPacsNode:

    def test_default_values(self):
        node = PacsNode()
        assert node.name == ""
        assert node.ae_title == ""
        assert node.ip_address == ""
        assert node.port == 104
        assert node.transfer_syntax == "JPEG2000Lossless"
        assert node.retrieve_method == "C-MOVE"
        assert node.hours == 3
        assert node.max_images == 0
        assert node.sync_interval == 60
        assert node.local_ae_title == "LOCAL_AE"
        assert node.local_port == 11112
        assert node.local_syntax == "JPEG2000Lossless"
        assert node.fallback_folder == ""

    def test_custom_values(self, sample_pacs_node):
        node = sample_pacs_node
        assert node.name == "Test PACS"
        assert node.ae_title == "TEST_AE"
        assert node.ip_address == "10.0.0.1"
        assert node.port == 4242
        assert node.transfer_syntax == "JPEGLossless"
        assert node.retrieve_method == "C-GET"

    def test_custom_service_params(self):
        node = PacsNode(hours=12, max_images=500, sync_interval=120)
        assert node.hours == 12
        assert node.max_images == 500
        assert node.sync_interval == 120

    def test_custom_local_dest_params(self):
        node = PacsNode(
            local_ae_title="ARZT_4", local_port=11113,
            local_syntax="ExplicitVRLittleEndian",
            fallback_folder="/tmp/fallback",
        )
        assert node.local_ae_title == "ARZT_4"
        assert node.local_port == 11113
        assert node.local_syntax == "ExplicitVRLittleEndian"
        assert node.fallback_folder == "/tmp/fallback"

    def test_to_dict(self, sample_pacs_node):
        d = sample_pacs_node.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "Test PACS"
        assert d["ae_title"] == "TEST_AE"
        assert d["ip_address"] == "10.0.0.1"
        assert d["port"] == 4242
        assert d["transfer_syntax"] == "JPEGLossless"
        assert d["retrieve_method"] == "C-GET"
        assert d["hours"] == 3  # default
        assert d["max_images"] == 0  # default
        assert d["sync_interval"] == 60  # default
        assert d["local_ae_title"] == "LOCAL_AE"  # default
        assert d["local_port"] == 11112  # default
        assert d["local_syntax"] == "JPEG2000Lossless"  # default
        assert d["fallback_folder"] == ""  # default

    def test_to_dict_custom_service_params(self):
        node = PacsNode(name="X", hours=24, max_images=1000, sync_interval=300)
        d = node.to_dict()
        assert d["hours"] == 24
        assert d["max_images"] == 1000
        assert d["sync_interval"] == 300

    def test_to_dict_custom_local_dest(self):
        node = PacsNode(
            name="X", local_ae_title="ARZT_4", local_port=11113,
            local_syntax="JPEGLossless", fallback_folder="/data/incoming",
        )
        d = node.to_dict()
        assert d["local_ae_title"] == "ARZT_4"
        assert d["local_port"] == 11113
        assert d["local_syntax"] == "JPEGLossless"
        assert d["fallback_folder"] == "/data/incoming"

    def test_from_dict_full(self):
        data = {
            "name": "Remote", "ae_title": "REM_AE",
            "ip_address": "192.168.0.1", "port": 11113,
            "transfer_syntax": "ExplicitVRLittleEndian",
            "retrieve_method": "C-GET",
            "hours": 12, "max_images": 200, "sync_interval": 90,
            "local_ae_title": "ARZT_4", "local_port": 11114,
            "local_syntax": "JPEGLossless",
            "fallback_folder": "/fallback",
        }
        node = PacsNode.from_dict(data)
        assert node.name == "Remote"
        assert node.ae_title == "REM_AE"
        assert node.port == 11113
        assert node.retrieve_method == "C-GET"
        assert node.hours == 12
        assert node.max_images == 200
        assert node.sync_interval == 90
        assert node.local_ae_title == "ARZT_4"
        assert node.local_port == 11114
        assert node.local_syntax == "JPEGLossless"
        assert node.fallback_folder == "/fallback"

    def test_from_dict_defaults(self):
        node = PacsNode.from_dict({})
        assert node.name == ""
        assert node.port == 104
        assert node.transfer_syntax == "JPEG2000Lossless"
        assert node.retrieve_method == "C-MOVE"
        assert node.hours == 3
        assert node.max_images == 0
        assert node.sync_interval == 60
        assert node.local_ae_title == "LOCAL_AE"
        assert node.local_port == 11112
        assert node.local_syntax == "JPEG2000Lossless"
        assert node.fallback_folder == ""

    def test_roundtrip(self, sample_pacs_node):
        d = sample_pacs_node.to_dict()
        restored = PacsNode.from_dict(d)
        assert restored.name == sample_pacs_node.name
        assert restored.port == sample_pacs_node.port
        assert restored.retrieve_method == sample_pacs_node.retrieve_method
        assert restored.hours == sample_pacs_node.hours
        assert restored.max_images == sample_pacs_node.max_images
        assert restored.sync_interval == sample_pacs_node.sync_interval
        assert restored.local_ae_title == sample_pacs_node.local_ae_title
        assert restored.local_port == sample_pacs_node.local_port
        assert restored.local_syntax == sample_pacs_node.local_syntax
        assert restored.fallback_folder == sample_pacs_node.fallback_folder

    def test_roundtrip_custom_service_params(self):
        node = PacsNode(
            name="Test", ae_title="T_AE",
            hours=48, max_images=9999, sync_interval=10,
        )
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.hours == 48
        assert restored.max_images == 9999
        assert restored.sync_interval == 10

    def test_roundtrip_custom_local_dest(self):
        node = PacsNode(
            name="Test", ae_title="T_AE",
            local_ae_title="MY_ARZT", local_port=22222,
            local_syntax="JPEGLossless",
            fallback_folder="/my/fallback",
        )
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.local_ae_title == "MY_ARZT"
        assert restored.local_port == 22222
        assert restored.local_syntax == "JPEGLossless"
        assert restored.fallback_folder == "/my/fallback"


class TestPacsNodePrioritySeriesTerms:
    """Per-source priority series list: keyword / regex entries that
    bias the engine's per-cycle queue ordering toward studies whose
    series descriptions match.  List index 0 = highest priority."""

    DEFAULT_TERMS = (
        "cct", "cta", "ct-a", "angio", "nevas",
        "perf", "perfusion", "ctp", "ct-p",
    )

    def test_default_priority_terms_helper_returns_expected_entries(self):
        terms = default_priority_terms()
        assert [t["term"] for t in terms] == list(self.DEFAULT_TERMS)
        assert all(t["is_regex"] is False for t in terms)

    def test_default_priority_terms_returns_fresh_list_each_call(self):
        """Mutating the returned list must not poison subsequent
        callers — important since the helper is used as a default
        value seeded into every new PacsNode."""
        a = default_priority_terms()
        a.append({"term": "polluted", "is_regex": False})
        b = default_priority_terms()
        assert {t["term"] for t in b} == set(self.DEFAULT_TERMS)

    def test_fresh_pacsnode_has_default_priority_terms(self):
        node = PacsNode()
        assert [t["term"] for t in node.priority_series_terms] == \
            list(self.DEFAULT_TERMS)

    def test_priority_terms_round_trip_through_to_from_dict(self):
        node = PacsNode()
        node.priority_series_terms = [
            {"term": "stroke", "is_regex": False},
            {"term": r"^CTA\b", "is_regex": True},
        ]
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.priority_series_terms == [
            {"term": "stroke", "is_regex": False},
            {"term": r"^CTA\b", "is_regex": True},
        ]

    def test_priority_terms_empty_list_is_preserved_on_roundtrip(self):
        """If the user explicitly clears the list, the empty list
        must survive the to_dict / from_dict cycle (no
        re-defaulting)."""
        node = PacsNode()
        node.priority_series_terms = []
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.priority_series_terms == []

    def test_from_dict_without_priority_key_applies_defaults(self):
        """Legacy config files (missing the new key) must produce a
        node whose priority list is the bundled defaults."""
        # Take a fresh node's dict, drop the new key, reload.
        d = PacsNode().to_dict()
        d.pop("priority_series_terms", None)
        restored = PacsNode.from_dict(d)
        assert [t["term"] for t in restored.priority_series_terms] == \
            list(self.DEFAULT_TERMS)

    def test_two_pacsnodes_have_independent_priority_lists(self):
        """Mutating one node's list must not leak into another's."""
        a = PacsNode(name="A")
        b = PacsNode(name="B")
        a.priority_series_terms.append({"term": "extra", "is_regex": False})
        assert {t["term"] for t in b.priority_series_terms} == \
            set(self.DEFAULT_TERMS)


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — basic properties
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigDefaults:

    def test_default_local_node_legacy(self, default_config):
        """Legacy local_node property should return a sensible default."""
        assert default_config.local_node.name == "Local PACS"
        assert default_config.local_node.ae_title == "LOCAL_AE"
        assert default_config.local_node.port == 11112

    def test_default_remote_nodes_empty(self, default_config):
        assert default_config.remote_nodes == {}

    def test_default_prior_studies(self, default_config):
        assert default_config.prior_studies_count == 0
        assert default_config.prior_studies_same_modality is False

    def test_default_filter_groups(self, default_config):
        assert default_config.filter_group_names == []
        assert default_config.institution_assignments == {}
        assert default_config.active_filter_groups == []
        assert default_config.filter_groups_enabled is False

    def test_default_service_params(self, default_config):
        assert default_config.default_hours == 3
        assert default_config.max_images == 0
        assert default_config.sync_interval == 60

    def test_default_language_is_english(self, default_config):
        """A fresh config must default to English."""
        assert default_config.language == "en"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — language setting
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigLanguage:
    """The language is chosen in the settings dialog from a fixed list
    of supported locales (en, de, fr, es). It must round-trip through
    save/load so a restart preserves the choice."""

    def test_language_can_be_set_to_german(self, default_config):
        default_config.language = "de"
        assert default_config.language == "de"

    @pytest.mark.parametrize("lang", ["en", "de", "fr", "es"])
    def test_language_roundtrip(self, tmp_config_path, lang):
        cfg = AppConfig(config_path=tmp_config_path)
        cfg.language = lang
        cfg.save()

        loaded = AppConfig(config_path=tmp_config_path)
        loaded.load()
        assert loaded.language == lang

    def test_load_missing_language_defaults_to_english(
            self, tmp_config_path):
        """Older config files don't contain a language key — loading
        them must not crash and must fall back to English."""
        with open(tmp_config_path, "w") as f:
            json.dump({"remotes": {}}, f)

        loaded = AppConfig(config_path=tmp_config_path)
        loaded.load()
        assert loaded.language == "en"

    def test_save_writes_language_key(self, default_config):
        default_config.language = "fr"
        default_config.save()
        with open(default_config.config_path) as f:
            data = json.load(f)
        assert data.get("language") == "fr"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — save and load
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigPersistence:

    def test_save_creates_file(self, populated_config):
        populated_config.save()
        assert os.path.exists(populated_config.config_path)

    def test_save_produces_valid_json(self, populated_config):
        populated_config.save()
        with open(populated_config.config_path) as f:
            data = json.load(f)
        assert "remotes" in data
        assert "filter_group_names" in data

    def test_save_no_global_local_key(self, populated_config):
        """New config should not write a top-level 'local' key."""
        populated_config.save()
        with open(populated_config.config_path) as f:
            data = json.load(f)
        assert "local" not in data

    def test_load_nonexistent_returns_false(self, tmp_config_path):
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_save_then_load_roundtrip(self, populated_config):
        populated_config.save()

        loaded = AppConfig(config_path=populated_config.config_path)
        assert loaded.load() is True

        # Remote nodes
        assert "ct" in loaded.remote_nodes
        assert "mri" in loaded.remote_nodes
        assert loaded.remote_nodes["ct"].name == "CT Scanner"
        assert loaded.remote_nodes["mri"].retrieve_method == "C-GET"

        # Per-source service parameters on remote nodes
        assert loaded.remote_nodes["ct"].hours == 3
        assert loaded.remote_nodes["ct"].max_images == 0
        assert loaded.remote_nodes["ct"].sync_interval == 60
        assert loaded.remote_nodes["mri"].hours == 24
        assert loaded.remote_nodes["mri"].max_images == 1000
        assert loaded.remote_nodes["mri"].sync_interval == 300

        # Per-source local destination
        assert loaded.remote_nodes["ct"].local_ae_title == "LOCAL_AE"
        assert loaded.remote_nodes["ct"].local_port == 11112
        assert loaded.remote_nodes["ct"].fallback_folder == "/tmp/dicom_test"
        assert loaded.remote_nodes["mri"].local_ae_title == "ARZT_4"
        assert loaded.remote_nodes["mri"].local_port == 11113
        assert loaded.remote_nodes["mri"].fallback_folder == "/tmp/dicom_test_mri"

        # Prior studies
        assert loaded.prior_studies_count == 2
        assert loaded.prior_studies_same_modality is True

        # Filter groups
        assert loaded.filter_group_names == ["Group A", "Group B", "Group C"]
        assert loaded.institution_assignments["Hospital Alpha"] == "Group A"
        assert loaded.active_filter_groups == ["Group A"]
        assert loaded.filter_groups_enabled is True

        # Legacy global service defaults
        assert loaded.default_hours == 6
        assert loaded.max_images == 500
        assert loaded.sync_interval == 120

    def test_load_corrupt_json(self, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            f.write("{invalid json!!}")
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_load_oserror_returns_false(self, tmp_config_path, monkeypatch):
        """An unreadable file (permissions, stale mount) must not crash
        startup — load() logs and returns False, same as a parse error."""
        with open(tmp_config_path, "w") as f:
            json.dump({"remotes": {}}, f)

        real_open = open

        def deny_open(path, *args, **kwargs):
            if path == tmp_config_path:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", deny_open)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_load_utf8_non_ascii_config(self, tmp_config_path):
        """A UTF-8 config file with non-ASCII text (German / French
        institution names) must load with the strings intact."""
        with open(tmp_config_path, "w", encoding="utf-8") as f:
            json.dump({
                "remotes": {"ct": {"name": "Krankenhaus Süd",
                                   "ae_title": "SÜD_AE"}},
                "institution_assignments": {"Hôpital Béziers": "Gruppe Ä"},
            }, f, ensure_ascii=False)

        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert config.remote_nodes["ct"].name == "Krankenhaus Süd"
        assert config.institution_assignments["Hôpital Béziers"] == "Gruppe Ä"

    def test_load_utf8_when_platform_default_is_not_utf8(
            self, tmp_config_path, monkeypatch):
        """load() must pass encoding="utf-8" explicitly rather than rely
        on the platform default (cp1252 on a German Windows box).

        Without it, a hand-edited / externally written UTF-8 config
        raises UnicodeDecodeError there, load() returns False, the app
        re-runs the first-run wizard, and the next save() starts from an
        empty _raw_data — silently dropping unknown-key round-tripping.
        """
        with open(tmp_config_path, "w", encoding="utf-8") as f:
            json.dump({"remotes": {"ct": {"name": "Krankenhaus Süd"}},
                       "future_unknown_key": "keep me"},
                      f, ensure_ascii=False)

        real_open = open

        def cp1252_default_open(path, *args, **kwargs):
            # Simulate a non-UTF-8 locale: any text-mode open() that
            # does NOT name its encoding gets cp1252, which cannot
            # decode the UTF-8 "ü" byte pair.
            mode = kwargs.get("mode", args[0] if args else "r")
            if "b" not in mode and not kwargs.get("encoding"):
                kwargs["encoding"] = "cp1252"
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", cp1252_default_open)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert config.remote_nodes["ct"].name == "Krankenhaus Süd"

        # And the unknown key survives a save() under the same locale.
        assert config.save() is True
        with real_open(tmp_config_path, encoding="utf-8") as f:
            assert json.load(f)["future_unknown_key"] == "keep me"

    def test_load_invalid_utf8_returns_false(self, tmp_config_path):
        """A corrupt (non-UTF-8 binary) file must not crash load()."""
        with open(tmp_config_path, "wb") as f:
            f.write(b"\x80\x81\xff\xfe not utf-8")
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is False

    def test_save_returns_true_on_success(self, populated_config):
        assert populated_config.save() is True

    def test_save_returns_false_on_oserror(self, populated_config,
                                           monkeypatch):
        """save() must swallow OSError (one caller is a QTimer slot,
        where an exception would propagate into the Qt event loop)
        and report failure via its return value instead."""
        def fail_replace(src, dst):
            raise OSError(28, "No space left on device", dst)

        monkeypatch.setattr("core.config.os.replace", fail_replace)
        assert populated_config.save() is False

    def test_save_unwritable_location_returns_false(self):
        """A config path inside an uncreatable directory (e.g. under a
        read-only root) must yield False, not an exception."""
        config = AppConfig(
            config_path="/nonexistent_ro_root/sub/dir/config.json")
        assert config.save() is False

    def test_load_empty_json(self, tmp_config_path):
        with open(tmp_config_path, "w") as f:
            json.dump({}, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert config.remote_nodes == {}

    def test_migrate_old_single_remote_format(self, tmp_config_path):
        """Old configs had 'remote' key instead of 'remotes'."""
        old_data = {
            "local": PacsNode(name="L", ae_title="L_AE").to_dict(),
            "remote": PacsNode(name="R", ae_title="R_AE", port=105).to_dict(),
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_data, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        assert "default" in config.remote_nodes
        assert config.remote_nodes["default"].port == 105

    def test_migrate_old_remotes_without_per_source_params(self, tmp_config_path):
        """Old remotes without hours/max_images/sync_interval should inherit globals."""
        old_data = {
            "local": PacsNode(name="L", ae_title="L_AE").to_dict(),
            "remotes": {
                "ct": {
                    "name": "CT", "ae_title": "CT_AE",
                    "ip_address": "10.0.0.1", "port": 104,
                    "transfer_syntax": "JPEG2000Lossless",
                    "retrieve_method": "C-MOVE",
                    # No hours/max_images/sync_interval!
                },
            },
            "default_hours": 12,
            "max_images": 999,
            "sync_interval": 180,
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_data, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        # Should inherit the global values
        assert config.remote_nodes["ct"].hours == 12
        assert config.remote_nodes["ct"].max_images == 999
        assert config.remote_nodes["ct"].sync_interval == 180

    def test_migrate_old_local_to_per_source(self, tmp_config_path):
        """Old config with global 'local' should migrate AE/port into each remote."""
        old_data = {
            "local": {
                "name": "Local",
                "ae_title": "MY_LOCAL",
                "ip_address": "127.0.0.1",
                "port": 22222,
                "transfer_syntax": "JPEGLossless",
            },
            "remotes": {
                "ct": {
                    "name": "CT", "ae_title": "CT_AE",
                    "ip_address": "10.0.0.1", "port": 104,
                    "transfer_syntax": "JPEG2000Lossless",
                    "retrieve_method": "C-MOVE",
                    # No local_ae_title etc.
                },
            },
            "fallback_storage_enabled": True,
            "fallback_storage_path": "/old/fallback",
        }
        with open(tmp_config_path, "w") as f:
            json.dump(old_data, f)
        config = AppConfig(config_path=tmp_config_path)
        assert config.load() is True
        # Should have migrated local dest from old global local
        assert config.remote_nodes["ct"].local_ae_title == "MY_LOCAL"
        assert config.remote_nodes["ct"].local_port == 22222
        assert config.remote_nodes["ct"].local_syntax == "JPEGLossless"
        assert config.remote_nodes["ct"].fallback_folder == "/old/fallback"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — helper methods
# ═══════════════════════════════════════════════════════════════════════════

class TestAppConfigHelpers:

    def test_get_remote_names(self, populated_config):
        names = populated_config.get_remote_names()
        assert set(names) == {"ct", "mri"}

    def test_get_local_dict_for_existing(self, populated_config):
        d = populated_config.get_local_dict_for("ct")
        assert d["ae_title"] == "LOCAL_AE"
        assert d["port"] == 11112

    def test_get_local_dict_for_mri(self, populated_config):
        d = populated_config.get_local_dict_for("mri")
        assert d["ae_title"] == "ARZT_4"
        assert d["port"] == 11113

    def test_get_local_dict_for_missing_key(self, populated_config):
        d = populated_config.get_local_dict_for("nonexistent")
        assert d["ae_title"] == "LOCAL_AE"  # fallback default
        assert d["port"] == 11112

    def test_local_query_defaults_to_the_move_destination(
            self, populated_config):
        """Unset overrides keep the historical behaviour: the box that
        receives the images is the one asked what it has."""
        assert (populated_config.get_local_query_dict_for("ct")
                == populated_config.get_local_dict_for("ct"))

    def test_local_query_host_override(self, populated_config):
        """Aim the inventory query somewhere else — needed when the
        built-in Storage SCP holds the receiving address and cannot
        answer a C-FIND."""
        populated_config.remote_nodes["ct"].local_query_host = "127.0.0.1"
        d = populated_config.get_local_query_dict_for("ct")
        assert d["ip_address"] == "127.0.0.1"
        # Everything else still describes the same local PACS.
        assert d["ae_title"] == "LOCAL_AE"
        assert d["port"] == 11112
        # And the C-MOVE destination is untouched.
        assert (populated_config.get_local_dict_for("ct")["ip_address"]
                != "127.0.0.1")

    def test_local_query_port_override(self, populated_config):
        populated_config.remote_nodes["ct"].local_query_port = 11115
        assert populated_config.get_local_query_dict_for("ct")["port"] == 11115
        assert populated_config.get_local_dict_for("ct")["port"] == 11112

    def test_builtin_scp_defaults_the_query_to_loopback(
            self, populated_config):
        """The collision this default exists to prevent: with the
        built-in SCP receiving, the C-MOVE destination address and the
        inventory address can be the same interface, and the SCP's
        more-specific bind then swallows the query."""
        node = populated_config.remote_nodes["ct"]
        node.receive_with_builtin_scp = True
        d = populated_config.get_local_query_dict_for("ct")
        assert d["ip_address"] == LOCAL_QUERY_LOOPBACK
        # Only the address moves; the local PACS is still the same AE
        # on the same port, and the C-MOVE destination is untouched.
        assert d["ae_title"] == "LOCAL_AE"
        assert d["port"] == 11112
        assert (populated_config.get_local_dict_for("ct")["ip_address"]
                != LOCAL_QUERY_LOOPBACK)

    def test_explicit_host_still_beats_the_loopback_default(
            self, populated_config):
        node = populated_config.remote_nodes["ct"]
        node.receive_with_builtin_scp = True
        node.local_query_host = "10.0.0.9"
        assert (populated_config.get_local_query_dict_for("ct")["ip_address"]
                == "10.0.0.9")

    def test_without_the_builtin_scp_the_default_is_unchanged(
            self, populated_config):
        """No built-in SCP means no collision, so the historical
        behaviour must survive untouched."""
        node = populated_config.remote_nodes["ct"]
        assert node.receive_with_builtin_scp is False
        assert (populated_config.get_local_query_dict_for("ct")
                == populated_config.get_local_dict_for("ct"))

    def test_builtin_scp_does_not_move_the_query_port(
            self, populated_config):
        """Only the host is guessed.  The local PACS answers both roles
        on one port everywhere we have seen, so inventing a port would
        break the common case."""
        node = populated_config.remote_nodes["ct"]
        node.receive_with_builtin_scp = True
        assert (populated_config.get_local_query_dict_for("ct")["port"]
                == populated_config.get_local_dict_for("ct")["port"])

    def test_local_query_overrides_survive_a_roundtrip(self):
        node = PacsNode(name="x", local_query_host="127.0.0.1",
                        local_query_port=11115)
        restored = PacsNode.from_dict(node.to_dict())
        assert restored.local_query_host == "127.0.0.1"
        assert restored.local_query_port == 11115

    def test_local_query_overrides_absent_from_old_config(self):
        """A config file written before these fields existed must load
        with the overrides off, i.e. unchanged behaviour."""
        restored = PacsNode.from_dict({"name": "x"})
        assert restored.local_query_host == ""
        assert restored.local_query_port == 0

    def test_get_local_dict_legacy(self, populated_config):
        """Legacy get_local_dict() returns first source's local config."""
        d = populated_config.get_local_dict()
        assert d["ae_title"] == "LOCAL_AE"  # from "ct" (first)

    def test_get_remote_dict_existing(self, populated_config):
        d = populated_config.get_remote_dict("ct")
        assert d is not None
        assert d["name"] == "CT Scanner"

    def test_get_remote_dict_missing(self, populated_config):
        assert populated_config.get_remote_dict("nonexistent") is None

    def test_update_local_ip_caches(self, populated_config):
        """update_local_ip() caches the IP so get_local_dict_for uses it."""
        with patch("core.config.get_local_ip", return_value="10.0.0.99"):
            populated_config.update_local_ip()
        # After caching, get_local_dict_for should use the cached value
        # even without patching get_local_ip anymore
        d = populated_config.get_local_dict_for("ct")
        assert d["ip_address"] == "10.0.0.99"

    def test_update_local_ip_used_by_legacy(self, populated_config):
        """Legacy get_local_dict also uses cached IP."""
        with patch("core.config.get_local_ip", return_value="10.0.0.77"):
            populated_config.update_local_ip()
        d = populated_config.get_local_dict()
        assert d["ip_address"] == "10.0.0.77"


# ═══════════════════════════════════════════════════════════════════════════
# AppConfig — default config path
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaultConfigPath:

    @patch("core.config.platform.system", return_value="Darwin")
    def test_macos_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "Library/Application Support/DicomSyncGUI" in path
        assert path.endswith(DEFAULT_CONFIG_FILE)

    @patch("core.config.platform.system", return_value="Linux")
    def test_linux_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "DicomSyncGUI" in path
        assert path.endswith(DEFAULT_CONFIG_FILE)

    @patch("core.config.platform.system", return_value="Windows")
    def test_windows_path(self, _mock):
        path = AppConfig._default_config_path()
        assert "DicomSyncGUI" in path


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

class TestConstants:

    def test_transfer_syntaxes(self):
        assert "JPEG2000Lossless" in TRANSFER_SYNTAXES_NAMES
        assert "ExplicitVRLittleEndian" in TRANSFER_SYNTAXES_NAMES
        assert len(TRANSFER_SYNTAXES_NAMES) >= 5

    def test_retrieve_methods(self):
        # C-GET was removed from the offered list — it was never
        # implemented (the engine always issues C-MOVE).
        assert RETRIEVE_METHODS == ["C-MOVE"]


class TestGetLocalIP:

    def test_returns_string(self):
        ip = get_local_ip()
        assert isinstance(ip, str)

    def test_returns_valid_ip_format(self):
        ip = get_local_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        for p in parts:
            assert p.isdigit()

    @patch("socket.socket")
    def test_fallback_on_error(self, mock_sock):
        mock_sock.side_effect = OSError("Network unreachable")
        ip = get_local_ip()
        assert ip == "127.0.0.1"


class TestLocalIPFor:
    """``local_ip_for`` answers "which of MY addresses reaches that
    host", which is not the same question ``get_local_ip`` answers (that
    one always follows the default route).  A source PACS behind a VPN
    is reached on the tunnel address, and the built-in SCP must bind
    exactly that address to win over another process's wildcard bind."""

    def test_returns_valid_ip_format(self):
        parts = local_ip_for("8.8.8.8").split(".")
        assert len(parts) == 4
        assert all(p.isdigit() for p in parts)

    def test_asks_the_route_to_the_given_host(self):
        with patch("socket.socket") as mock_sock:
            sock = mock_sock.return_value.__enter__.return_value
            sock.getsockname.return_value = ("10.168.20.14", 51234)
            assert local_ip_for("10.1.15.30") == "10.168.20.14"
        sock.connect.assert_called_once()
        assert sock.connect.call_args.args[0][0] == "10.1.15.30"

    def test_falls_back_to_default_route_on_error(self):
        """VPN down or a bad address must not crash the SCP startup —
        fall back to the default-route address."""
        with patch("core.config.socket.socket", side_effect=OSError("no route")):
            with patch("core.config.get_local_ip", return_value="192.168.1.55"):
                assert local_ip_for("10.1.15.30") == "192.168.1.55"


class TestPacsNodeBuiltinReceiver:

    def test_defaults_to_off(self):
        assert PacsNode().receive_with_builtin_scp is False

    def test_round_trips_through_dict(self):
        node = PacsNode(name="tz", receive_with_builtin_scp=True)
        assert node.to_dict()["receive_with_builtin_scp"] is True
        assert PacsNode.from_dict(node.to_dict()).receive_with_builtin_scp is True

    def test_missing_key_in_old_config_loads_as_off(self):
        """Config files written before this field existed must still
        load, with the built-in receiver disabled."""
        assert PacsNode.from_dict({"name": "tz"}).receive_with_builtin_scp is False


class TestAppConfigAsyncSave:
    """``save()`` fsyncs, so calling it from a Qt slot stalls the event
    loop for the length of the disk write.  ``save_async`` snapshots on
    the calling thread and writes on a background one."""

    def _config(self, tmp_path):
        cfg = AppConfig(str(tmp_path / "cfg.json"))
        cfg.remote_nodes["ct"] = PacsNode(name="CT", ae_title="CT")
        return cfg

    def test_save_async_writes_the_file(self, tmp_path):
        cfg = self._config(tmp_path)
        cfg.save_async()
        cfg.flush()
        with open(cfg.config_path, encoding="utf-8") as f:
            assert json.load(f)["remotes"]["ct"]["ae_title"] == "CT"

    def test_payload_is_snapshotted_on_the_calling_thread(self, tmp_path):
        """The snapshot is what makes an off-thread write safe: state
        mutated after the call must not leak into that write."""
        cfg = self._config(tmp_path)
        cfg.language = "de"
        cfg.save_async()
        cfg.language = "fr"          # after the snapshot
        cfg.flush()
        with open(cfg.config_path, encoding="utf-8") as f:
            assert json.load(f)["language"] == "de"

    def test_bursts_coalesce_to_the_last_state(self, tmp_path):
        """Ten UI events must not produce ten disk writes — and the
        file must end up holding the FINAL state, not an arbitrary one."""
        cfg = self._config(tmp_path)
        writes = []
        real = cfg._write_payload

        def counting(data):
            writes.append(data["language"])
            return real(data)

        cfg._write_payload = counting
        for lang in ("en", "de", "fr", "es"):
            cfg.language = lang
            cfg.save_async()
        cfg.flush()
        assert writes, "no write happened at all"
        assert len(writes) < 4, f"expected coalescing, got {len(writes)} writes"
        with open(cfg.config_path, encoding="utf-8") as f:
            assert json.load(f)["language"] == "es"

    def test_flush_is_a_noop_without_a_pending_write(self, tmp_path):
        self._config(tmp_path).flush()   # must not raise or block

    def test_sync_save_after_flush_wins(self, tmp_path):
        """The ordering ``flush()`` then ``save()`` is what stops a
        queued older snapshot from rolling back newer state."""
        cfg = self._config(tmp_path)
        cfg.language = "de"
        cfg.save_async()
        cfg.flush()
        cfg.language = "fr"
        cfg.save()
        with open(cfg.config_path, encoding="utf-8") as f:
            assert json.load(f)["language"] == "fr"
