import importlib
import os

from django.test import SimpleTestCase


class SettingsTestCase(SimpleTestCase):
    def test_allowed_hosts_include_render_defaults(self):
        from config import settings

        self.assertIn("myraid-saas-backend.onrender.com", settings.ALLOWED_HOSTS)
        self.assertIn("localhost", settings.ALLOWED_HOSTS)
        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)

    def test_allowed_hosts_include_render_external_hostname(self):
        from config import settings

        original = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        os.environ["RENDER_EXTERNAL_HOSTNAME"] = "dynamic-render-host.onrender.com"
        try:
            reloaded = importlib.reload(settings)
            self.assertIn("dynamic-render-host.onrender.com", reloaded.ALLOWED_HOSTS)
        finally:
            if original is None:
                os.environ.pop("RENDER_EXTERNAL_HOSTNAME", None)
            else:
                os.environ["RENDER_EXTERNAL_HOSTNAME"] = original
            importlib.reload(settings)
