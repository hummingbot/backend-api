"""
Pins the .env template written by setup.sh against config.py (READ-117).

setup.sh's heredoc is the only template this repo has, and the generated .env is the only
place an operator ever sees a setting. Nothing derives the heredoc from config.py, so the two
drifted: five whole settings groups had no line in the template at all. These tests make that
drift fail loudly instead of silently:

- every prefixed settings group in config.py is represented in the template,
- every setting the template documents actually exists in config.py, and
- every default the template shows is the default config.py applies.

Run with: pytest test/test_env_template_matches_config.py -v
"""
import json
import re
from pathlib import Path
from typing import Dict, Tuple

import pytest
from pydantic_settings import BaseSettings

import config

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "setup.sh"

# Groups added by READ-117: documented, but every line commented out, so a freshly generated
# .env keeps producing exactly the settings config.py already applies.
COMMENTED_ONLY_PREFIXES = ("PERFORMANCE_", "BACKTESTING_", "MARKET_DATA_", "CORS_", "AWS_")

# A variable assignment in the template: "NAME=value" (active) or "#NAME=value" (documented).
# Prose comments start with "# " and are therefore not matched.
_ASSIGNMENT = re.compile(r"^(#?)([A-Z][A-Z0-9_]*)=(.*)$")


def _env_template() -> str:
    """The body of setup.sh's `cat > .env << EOF` heredoc."""
    source = SETUP_SH.read_text()
    start = source.index("cat > .env << EOF\n") + len("cat > .env << EOF\n")
    end = source.index("\nEOF\n", start)
    return source[start:end]


def _assignments() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (active, documented) variable assignments found in the template."""
    active, documented = {}, {}
    for line in _env_template().splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        commented, name, value = match.groups()
        (documented if commented else active)[name] = value
    return active, documented


def _settings_groups() -> Dict[str, type]:
    """Every BaseSettings group in config.py that has a non-empty env_prefix, keyed by prefix."""
    groups = {}
    for attribute in vars(config).values():
        if not (isinstance(attribute, type) and issubclass(attribute, BaseSettings)):
            continue
        prefix = attribute.model_config.get("env_prefix", "")
        if prefix:
            groups[prefix] = attribute
    return groups


def _defaults_by_env_name() -> Dict[str, str]:
    """Map each prefixed setting's env var name to the default config.py applies, rendered as .env text."""
    defaults = {}
    for prefix, group in _settings_groups().items():
        for field_name, field in group.model_fields.items():
            defaults[f"{prefix}{field_name.upper()}"] = _render(field.default)
    return defaults


def _render(default) -> str:
    """Render a field default the way it would be written in .env."""
    if isinstance(default, bool):
        return "true" if default else "false"
    if isinstance(default, (list, dict)):
        return json.dumps(default)
    return str(default)


class TestEnvTemplateMatchesConfig:
    def test_every_settings_group_is_represented(self):
        """A new group in config.py must show up in the template — that is exactly what drifted."""
        active, documented = _assignments()
        names = set(active) | set(documented)
        for prefix in _settings_groups():
            assert any(name.startswith(prefix) for name in names), (
                f"config.py defines a settings group with env_prefix {prefix!r} that setup.sh's "
                f".env template never mentions; operators cannot discover it"
            )

    def test_documented_settings_exist_in_config(self):
        """The template must not document a setting config.py does not have (renamed or removed)."""
        _, documented = _assignments()
        known = _defaults_by_env_name()
        for name in documented:
            assert name in known, f"setup.sh documents {name}, which no config.py settings group defines"

    def test_documented_defaults_match_config(self):
        """A default shown in the template must be the default config.py applies."""
        _, documented = _assignments()
        known = _defaults_by_env_name()
        for name, value in documented.items():
            assert value == known[name], (
                f"setup.sh shows {name}={value} but config.py defaults to {known[name]}"
            )

    @pytest.mark.parametrize("prefix", COMMENTED_ONLY_PREFIXES)
    def test_optional_groups_are_documented_but_not_set(self, prefix):
        """These groups are documented only: a fresh .env must not pin a value config.py owns."""
        active, documented = _assignments()
        assert any(name.startswith(prefix) for name in documented), f"no {prefix} setting is documented"
        assert not [name for name in active if name.startswith(prefix)], (
            f"{prefix} settings must stay commented out so a fresh .env changes no runtime behavior"
        )

    def test_performance_retention_explains_that_zero_keeps_everything(self):
        """PERFORMANCE_RETENTION_DAYS=0 grows the database forever; the template must say so."""
        template = _env_template()
        assert "#PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL=60" in template
        assert "#PERFORMANCE_RETENTION_DAYS=0" in template
        retention_comment = template[: template.index("#PERFORMANCE_RETENTION_DAYS=")]
        assert "0 keeps everything forever" in retention_comment


class TestReadmePointsAtConfig:
    def test_configuration_section_names_config_py_as_authoritative(self):
        """The README excerpt is curated, so it has to say where the full list lives."""
        readme = (REPO_ROOT / "README.md").read_text()
        section = readme[readme.index("## Configuration"):]
        section = section[: section.index("\n## ")]
        assert "`config.py`" in section
        assert "PERFORMANCE_RETENTION_DAYS" in section
