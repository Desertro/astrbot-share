"""Exercise fresh installs, secret escaping, identity pruning and safe reruns."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_install_and_safe_rerun(tmp_path):
    specs = json.loads((ROOT / "config/variables.json").read_text())
    values = {}
    for index, (name, spec) in enumerate(specs.items(), 1):
        if spec["required"]:
            values[name] = (
                str(800000000 + index)
                if name.startswith("AB_QQ_")
                else "InstallTestAa123"
            )
    values["AB_DASHBOARD_PASSWORD"] = 'InstallAa1"\\$value\nnext'
    cookie_name = next(name for name in specs if "DOUYIN_CK" in name)
    values[cookie_name] = '[{"name":"sessionid","value":"not-a-real-cookie\\\\$`"}]'
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(name + "=" + json.dumps(value) for name, value in values.items())
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("AB_")}
    command = [
        sys.executable,
        str(ROOT / "tools/setup.py"),
        "--non-interactive",
        "--output",
        str(tmp_path),
        "--env-file",
        str(env_file),
    ]
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert values["AB_DASHBOARD_PASSWORD"] not in completed.stdout
    config = json.loads((tmp_path / "data/cmd_config.json").read_text())
    assert config["dashboard"]["pbkdf2_password"].startswith("pbkdf2_sha256$")
    assert config["platform"][0]["ws_reverse_token"] == values["AB_ONEBOT_TOKEN"]
    assert values["AB_QQ_1"] in config["admins_id"]
    mem = json.loads(
        (tmp_path / "data/config/astrbot_plugin_mem_chat_config.json").read_text()
    )
    assert mem["group_whitelist"] == [values["AB_QQ_2"]]
    assert mem["group_persona_overrides"] == {}
    parser = json.loads(
        (tmp_path / "data/config/astrbot_plugin_parser_config.json").read_text()
    )
    assert parser["douyin_ck"] == values[cookie_name]
    for path in (tmp_path / "data/config").glob("*.json"):
        assert "{{AB_" not in path.read_text()
    with sqlite3.connect(tmp_path / "data/data_v4.db") as database:
        tables = {
            row[0]
            for row in database.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        assert tables == {"personas", "persona_folders", "command_configs"}
        assert database.execute("select count(*) from personas").fetchone()[0] == 1
        database.execute(
            "UPDATE personas SET system_prompt = ?", ("Preserve local edit",)
        )
    before = (tmp_path / "data/cmd_config.json").read_bytes()
    again = subprocess.run(command, env=env, capture_output=True, text=True)
    assert again.returncode == 2
    assert (tmp_path / "data/cmd_config.json").read_bytes() == before
    overwrite = subprocess.run(
        command + ["--overwrite"], env=env, capture_output=True, text=True
    )
    assert overwrite.returncode == 0, overwrite.stderr
    assert list((tmp_path / "data/setup-backups").rglob("cmd_config.json"))
    with sqlite3.connect(tmp_path / "data/data_v4.db") as database:
        assert (
            database.execute("select system_prompt from personas").fetchone()[0]
            == "Preserve local edit"
        )
    check = subprocess.run(
        command + ["--check"], env=env, capture_output=True, text=True
    )
    assert check.returncode == 0, check.stderr
    auth_spec = importlib.util.spec_from_file_location(
        "verify_share_auth", ROOT / "astrbot/core/utils/auth_password.py"
    )
    auth = importlib.util.module_from_spec(auth_spec)
    auth_spec.loader.exec_module(auth)
    assert auth.verify_dashboard_password(
        config["dashboard"]["pbkdf2_password"], values["AB_DASHBOARD_PASSWORD"]
    )


def test_missing_values_write_no_runtime_files(tmp_path):
    command = [
        sys.executable,
        str(ROOT / "tools/setup.py"),
        "--non-interactive",
        "--output",
        str(tmp_path),
        "--env-file",
        str(tmp_path / "missing.env"),
    ]
    env = {key: value for key, value in os.environ.items() if not key.startswith("AB_")}
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not (tmp_path / "data").exists()


def test_dotenv_is_literal_and_preserves_special_characters(tmp_path):
    sys.path.insert(0, str(ROOT))
    from share_runtime import read_env

    path = tmp_path / ".env"
    expected = 'quote" newline\n $() `command` \\ and unicode 你好'
    path.write_text("AB_TEST=" + json.dumps(expected, ensure_ascii=False))
    assert read_env(path)["AB_TEST"] == expected
