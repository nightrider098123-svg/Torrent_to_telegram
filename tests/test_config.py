import os
import tempfile
from configparser import ConfigParser
from utils import load_config, rotate_token

def test_load_config():
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("[Bot]\nadmin_ids = 12345,67890\ntoken = dummy_token")
        config_path = f.name

    config = load_config(config_path)

    assert config.get("Bot", "admin_ids") == "12345,67890"
    assert config.get("Bot", "token") == "dummy_token"

    os.remove(config_path)

def test_load_config_env_fallback():
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("[Bot]\nadmin_ids = 12345,67890")
        config_path = f.name

    os.environ["BOT_TOKEN"] = "env_dummy_token"
    config = load_config(config_path)

    assert config.get("Bot", "admin_ids") == "12345,67890"
    assert config.get("Bot", "token") == "env_dummy_token"

    del os.environ["BOT_TOKEN"]
    os.remove(config_path)

def test_rotate_token():
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("[Bot]\nadmin_ids = 12345\ntoken = dummy_token")
        config_path = f.name

    rotate_token(config_path, "Bot", "token", "new_super_secret")

    config = load_config(config_path)
    assert config.get("Bot", "token") == "new_super_secret"

    os.remove(config_path)
