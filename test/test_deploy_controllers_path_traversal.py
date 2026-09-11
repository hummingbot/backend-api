"""SEC-058: the controllers list re-read from the deployed script config is attacker-controlled.

`DockerService.create_hummingbot_instance` reads `controllers_config` back off the script config
YAML (which any authenticated caller can write through `POST /scripts/configs/{name}`) and used to
join every entry into a source and a destination path without validating it. These tests pin that
traversal entries are skipped and that only safe single-component names are ever copied.
"""
import os

import pytest
import yaml

from config import settings
from models import V2ControllerDeployment
from services.docker_service import DockerService


def _build_bots_tree(root, controllers_config):
    """Lay out the minimal bots/ tree create_hummingbot_instance expects under `root`."""
    credentials_dir = root / "bots" / "credentials" / "master_account"
    credentials_dir.mkdir(parents=True)
    (credentials_dir / "conf_client.yml").write_text("instance_id: placeholder\n")

    scripts_dir = root / "bots" / "conf" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "evil.yml").write_text(yaml.dump({"controllers_config": controllers_config}))

    controllers_dir = root / "bots" / "conf" / "controllers"
    controllers_dir.mkdir(parents=True)
    (controllers_dir / "good.yml").write_text("controller_name: good\n")

    # A file outside bots/ that a traversal entry would exfiltrate into the instance dir.
    (root / "secret.txt").write_text("DB_PASSWORD=hunter2\n")


def _deploy(root, monkeypatch, controllers_config):
    _build_bots_tree(root, controllers_config)
    monkeypatch.chdir(root)
    # No config password => the deployment bails out right after the config copy, so the test
    # never needs a live Docker daemon.
    monkeypatch.setattr(settings.security, "config_password", "")

    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = str(root)
    deployment = V2ControllerDeployment(
        instance_name="testbot",
        credentials_profile="master_account",
        controllers_config=["good.yml"],
        script_config="evil.yml",
    )
    service.create_hummingbot_instance(deployment)
    return root / "bots" / "instances" / "testbot" / "conf" / "controllers"


@pytest.mark.parametrize(
    "entry",
    [
        "../../../secret.txt",          # lands on bots/instances/secret.txt with the old code
        "../../../../etc/hosts",        # escapes the repo entirely
        "/etc/hosts",                   # absolute path
        "subdir/good.yml",              # separator inside the name
        "..",
    ],
)
def test_a_traversal_controller_entry_is_never_copied(tmp_path, monkeypatch, caplog, entry):
    with caplog.at_level("WARNING"):
        destination = _deploy(tmp_path, monkeypatch, [entry])

    copied = sorted(p.name for p in destination.iterdir()) if destination.exists() else []
    assert copied == []
    # Nothing was written anywhere outside the instance's own conf/controllers directory.
    assert not (tmp_path / "bots" / "instances" / "secret.txt").exists()
    assert (tmp_path / "secret.txt").read_text() == "DB_PASSWORD=hunter2\n"
    assert any(entry in record.message for record in caplog.records if record.levelname == "WARNING")


def test_a_non_string_controller_entry_is_skipped(tmp_path, monkeypatch):
    destination = _deploy(tmp_path, monkeypatch, [{"controller": "good.yml"}])

    copied = sorted(p.name for p in destination.iterdir()) if destination.exists() else []
    assert copied == []


def test_well_formed_controller_entries_are_still_copied(tmp_path, monkeypatch):
    destination = _deploy(tmp_path, monkeypatch, ["good.yml", "../../../secret.txt"])

    assert sorted(p.name for p in destination.iterdir()) == ["good.yml"]
    assert (destination / "good.yml").read_text() == "controller_name: good\n"
    assert not (tmp_path / "bots" / "instances" / "secret.txt").exists()
    assert os.path.exists(tmp_path / "secret.txt")
