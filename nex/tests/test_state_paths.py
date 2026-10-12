"""The operator-state override has one canonical resolution across layers."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

from agent import providers as provider_module  # noqa: E402
from agent.providers import default_settings_path, env_file_paths  # noqa: E402
from mcp.capability import capability_registry_path  # noqa: E402
from mcp.manager import servers_config_path  # noqa: E402
from mcp.paths import nex_home  # noqa: E402
from store import _db_path  # noqa: E402


class StatePathTests(unittest.TestCase):
    def test_all_default_state_files_follow_nex_home(self):
        with tempfile.TemporaryDirectory(prefix="nex-state-paths-") as root:
            custom = os.path.join(root, "operator-state")
            env = {"NEX_HOME": custom, "NEX_CAPABILITY_FILE": ""}
            with patch.dict(os.environ, env):
                self.assertEqual(nex_home(create=False), custom)
                self.assertEqual(servers_config_path(),
                                 os.path.join(custom, "servers.json"))
                self.assertEqual(_db_path(), os.path.join(custom, "nex.db"))
                settings_path = os.path.join(custom, "providers.json")
                self.assertEqual(default_settings_path(), settings_path)
                self.assertEqual(provider_module.SettingsStore().path,
                                 settings_path)
                self.assertEqual(capability_registry_path(),
                                 os.path.join(custom, "capabilities.json"))
                self.assertEqual(env_file_paths()[-1],
                                 os.path.join(custom, ".env"))

    def test_reading_the_configured_path_does_not_create_the_directory(self):
        with tempfile.TemporaryDirectory(prefix="nex-state-paths-") as root:
            custom = os.path.join(root, "not-created-yet")
            with patch.dict(os.environ, {"NEX_HOME": custom}):
                self.assertEqual(nex_home(create=False), custom)
                self.assertFalse(os.path.exists(custom))

    def test_env_chain_resolves_nex_home_before_loading_its_env_file(self):
        with tempfile.TemporaryDirectory(prefix="nex-env-chain-") as root:
            nex_dir = os.path.join(root, "nex")
            custom_home = os.path.join(root, "operator-state")
            os.makedirs(nex_dir)
            os.makedirs(custom_home)
            with open(os.path.join(root, ".env"), "w",
                      encoding="utf-8") as env_file:
                env_file.write("NEX_HOME=" + custom_home + chr(10))
            with open(os.path.join(custom_home, ".env"), "w",
                      encoding="utf-8") as env_file:
                env_file.write("STATE_PATH_TEST=loaded" + chr(10))

            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(provider_module, "NEX_DIR", nex_dir):
                provider_module.load_env()
                self.assertEqual(os.environ["NEX_HOME"], custom_home)
                self.assertEqual(os.environ["STATE_PATH_TEST"], "loaded")
                self.assertEqual(provider_module.DEFAULT_SETTINGS_PATH,
                                 os.path.join(custom_home, "providers.json"))

    def test_explicit_capability_file_still_overrides_the_shared_default(self):
        with patch.dict(os.environ, {
                "NEX_HOME": "/ignored/state", "NEX_CAPABILITY_FILE": "pins.json"}):
            self.assertEqual(capability_registry_path(), "pins.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
