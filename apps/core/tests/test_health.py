from django.test import TestCase
from rest_framework.test import APIClient


class HealthCheckTestCase(TestCase):
    def test_health_check_is_public_and_touches_database(self):
        response = APIClient().get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["database"], "ok")
        self.assertEqual(response["Cache-Control"], "no-store")
