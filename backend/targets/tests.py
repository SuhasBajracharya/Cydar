import json
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from agent.providers import provider_status, resolve
from targets.models import ProviderConfig

User = get_user_model()


class ProviderConfigTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="password123")
        self.client.login(username="testuser", password="password123")

    def test_provider_status_shows_providers(self):
        resp = self.client.get(reverse("providers-list"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        ids = [p["id"] for p in data]
        self.assertIn("deepseek", ids)
        self.assertIn("openrouter", ids)
        self.assertIn("ollama", ids)

    def test_provider_config_save_and_resolve(self):
        url = reverse("provider-config", kwargs={"provider_id": "openrouter"})
        resp = self.client.put(
            url,
            data=json.dumps({"api_key": "sk-or-v1-testkey123", "base_url": "https://custom.openrouter.ai/v1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["provider_id"], "openrouter")
        self.assertTrue(data["has_key"])
        self.assertEqual(data["source"], "database")
        self.assertNotIn("api_key", data)  # Never return secrets!

        # Check DB
        config = ProviderConfig.objects.get(provider_id="openrouter")
        self.assertEqual(config.api_key, "sk-or-v1-testkey123")
        self.assertEqual(config.base_url, "https://custom.openrouter.ai/v1")

        # Check resolve() picks up the DB key
        base_url, api_key, model_id, headers = resolve("openrouter")
        self.assertEqual(api_key, "sk-or-v1-testkey123")
        self.assertEqual(base_url, "https://custom.openrouter.ai/v1")

        # Check provider_status() reflects database source
        status_list = provider_status()
        or_status = next(p for p in status_list if p["id"] == "openrouter")
        self.assertTrue(or_status["configured"])
        self.assertEqual(or_status["source"], "database")
        self.assertEqual(or_status["base_url"], "https://custom.openrouter.ai/v1")

    def test_provider_config_delete(self):
        ProviderConfig.objects.create(
            provider_id="openrouter",
            api_key="secret",
            base_url="https://custom.url",
        )
        url = reverse("provider-config", kwargs={"provider_id": "openrouter"})
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])
        self.assertFalse(ProviderConfig.objects.filter(provider_id="openrouter").exists())

    def test_unauthenticated_access_denied(self):
        self.client.logout()
        url = reverse("provider-config", kwargs={"provider_id": "openrouter"})
        resp = self.client.put(url, data=json.dumps({"api_key": "123"}), content_type="application/json")
        self.assertEqual(resp.status_code, 401)
