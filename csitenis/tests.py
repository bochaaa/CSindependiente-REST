from django.test import SimpleTestCase
from django.urls import Resolver404, resolve


class AdminUrlRoutingTests(SimpleTestCase):
    def test_django_admin_uses_internal_route(self):
        match = resolve("/django-admin/")

        self.assertEqual(match.namespace, "admin")
        self.assertEqual(match.url_name, "index")

    def test_legacy_admin_route_is_not_handled_by_backend(self):
        with self.assertRaises(Resolver404):
            resolve("/admin/")
