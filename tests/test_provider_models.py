import tempfile
import unittest

from repvblicvs_engine import executor
from repvblicvs_engine.providers import Router
from repvblicvs_engine.store import Store


class ProviderModelTests(unittest.TestCase):
    def test_routes_configured_without_a_model_use_the_executor_model(self):
        with tempfile.TemporaryDirectory() as directory:
            router = Router(Store(directory))
            router.configure_account("acct", evidence={}, verified_until=0, status="unavailable")
            for name in executor.SUPPORTED:
                router.configure_provider(name, "acct")
            listed = {item["name"]: item["model"] for item in router.list()}
            for name, model in executor.SUPPORTED.items():
                self.assertEqual(listed[name], model)


if __name__ == "__main__":
    unittest.main()
