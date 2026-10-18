"""V3 region tests (PC-01): URL parsing, identity keying, scoped benches.

The load-bearing test is ``test_429_in_region_a_does_not_bench_region_b``:
it drives the real Carousel, benches a key through one regional identity,
and proves the same key stays healthy under the other region.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))

from core.carousel import Carousel, fingerprint  # noqa: E402
from core.region import parse_region, region_from_overrides, identity_segment  # noqa: E402


class TestParseRegion(unittest.TestCase):
    """The URL table from the design, parametrized."""

    CASES = [
        # AI Studio / flat Gemini: no region, ever.
        ("https://generativelanguage.googleapis.com/v1beta", "", ("global", "default")),
        # Vertex regional: the path is authoritative...
        ("https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/publishers/google/models/gemini-2.5-pro",
         "", ("us-central1", "path")),
        # ...even when the subdomain names a different region.
        ("https://europe-west1-aiplatform.googleapis.com/v1/projects/p/locations/europe-west4/x",
         "", ("europe-west4", "path")),
        # Subdomain alone (custom base without a locations segment).
        ("https://us-central1-aiplatform.googleapis.com/v1", "", ("us-central1", "subdomain")),
        # locations/global is still global.
        ("https://aiplatform.googleapis.com/v1/projects/p/locations/global/foo", "", ("global", "path")),
        # No locations segment, no subdomain region.
        ("https://aiplatform.googleapis.com/v1", "", ("global", "default")),
        # Bedrock: host region; cross-region model prefix is its own scope.
        ("https://bedrock-runtime.us-east-1.amazonaws.com", "anthropic.claude-sonnet", ("us-east-1", "bedrock")),
        ("https://bedrock-runtime.us-east-1.amazonaws.com", "us.anthropic.claude-sonnet", ("xr-us", "bedrock")),
        ("https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com", "m", ("xr-global", "bedrock")) if False else
        ("https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com", "m", ("us-gov-west-1", "bedrock")),
        # Azure: the resource is the quota scope.
        ("https://myres.openai.azure.com/openai/deployments/gpt", "", ("az-myres", "azure")),
        # Flat providers, localhost, proxies: global.
        ("https://api.openai.com/v1", "", ("global", "default")),
        ("https://api.groq.com/openai/v1", "", ("global", "default")),
        ("http://localhost:1234/v1", "", ("global", "default")),
        ("https://proxy.local/vertex/v1/projects/p/locations/europe-west4", "", ("global", "default")),
        # Hygiene: ports, case, trailing dot, encoding, query strings.
        ("HTTPS://US-CENTRAL1-AIPLATFORM.GOOGLEAPIS.COM/v1/projects/p/locations/us-central1/x?q=1",
         "", ("us-central1", "path")),
        ("https://us-central1-aiplatform.googleapis.com:443/v1", "", ("us-central1", "subdomain")),
        # A model named like a path segment must not be read as a region.
        ("https://generativelanguage.googleapis.com/v1beta/locations/gemini-2.5", "locations/x", ("global", "default")),
        # No URL at all.
        (None, "", ("global", "default")),
    ]

    def test_table(self):
        for url, model, expected in self.CASES:
            self.assertEqual(parse_region(url, model), expected, f"url={url!r} model={model!r}")

    def test_override_wins_and_validated(self):
        self.assertEqual(
            parse_region("https://api.openai.com/v1", "m", "US-Central1"),
            ("us-central1", "override"),
        )
        region, source = parse_region("https://api.openai.com/v1", "m", "US Central")
        self.assertEqual((region, source), ("global", "invalid"))

    def test_region_from_overrides(self):
        raw = "a1b2=us-central1, c3d4=europe-west4"
        self.assertEqual(region_from_overrides(raw, "a1b2"), "us-central1")
        self.assertEqual(region_from_overrides(raw, "C3D4"), "europe-west4")
        self.assertIsNone(region_from_overrides(raw, "zzzz"))
        self.assertIsNone(region_from_overrides("", "a1b2"))
        # A malformed entry does not disable the rest.
        self.assertEqual(region_from_overrides("nolabel, a1b2=us-east4", "a1b2"), "us-east4")


class TestIdentityKeying(unittest.TestCase):
    def setUp(self):
        self.engine = Carousel()

    def _add(self, key, provider="vertex", model="gemini-2.5-pro"):
        self.engine.mark(provider + ":" + model, key, True)

    def test_identity_two_part_when_global(self):
        self.assertEqual(
            Carousel.identity("Vertex", "Gemini", ""), "vertex:gemini",
        )
        self.assertEqual(Carousel.identity("v", "m", "global"), "v:m:global")

    def test_identity_three_part_when_regional(self):
        ident = Carousel.identity("Vertex", "Gemini-2.5-Pro", "US-Central1")
        self.assertEqual(ident, "vertex:gemini-2.5-pro:us-central1")

    def test_429_in_region_a_does_not_bench_region_b(self):
        """THE scoped-bench test: drive the real Carousel."""
        key = "k-region-test-1"
        ident_a = Carousel.identity("vertex", "gemini-2.5-pro", "us-central1")
        ident_b = Carousel.identity("vertex", "gemini-2.5-pro", "europe-west4")

        # Seed the key healthy in both regions.
        self.engine.mark(ident_a, key, True)
        self.engine.mark(ident_b, key, True)

        # A rate-limit refusal in region A benches the key there. The delay
        # is stated in seconds-from-now by the real transport path; 9999 is
        # "a long rest", which is what a 429 ladder stores.
        self.engine.mark(ident_a, key, False, delay=9999.0, kind="rate_limit")

        # ...and selection for region A reports EXHAUSTED...
        chosen_a, status_a = self.engine.select(ident_a, [key])
        self.assertEqual(status_a, "EXHAUSTED")

        # ...while the same key is still selectable for region B.
        chosen_b, status_b = self.engine.select(ident_b, [key])
        self.assertEqual(status_b, "SUCCESS")
        self.assertEqual(chosen_b, key)

    def test_auth_error_is_key_scoped(self):
        """An auth refusal is the key, not the region: both identities must see it.

        Auth/billing refusals are account-scoped in the real classifier —
        ``mark(..., scope=QuotaScope.ACCOUNT)`` populates the provider-wide
        account hold, which every identity of that provider reads. Drive
        exactly that and prove the hold crosses regions.
        """
        from core.quota import QuotaScope

        key = "k-auth-test-1"
        ident_a = Carousel.identity("vertex", "m", "us-central1")
        ident_b = Carousel.identity("vertex", "m", "europe-west4")
        self.engine.mark(ident_a, key, True)
        self.engine.mark(ident_b, key, True)
        self.engine.mark(ident_a, key, False, delay=9999.0, kind="auth",
                         scope=QuotaScope.ACCOUNT)
        chosen_a, status_a = self.engine.select(ident_a, [key])
        chosen_b, status_b = self.engine.select(ident_b, [key])
        self.assertEqual(status_a, "EXHAUSTED")
        self.assertEqual(status_b, "EXHAUSTED")

    def test_identity_segment_compatibility(self):
        # global keeps the historical two-part identity
        self.assertEqual(identity_segment("global", "default"), "")
        # anything else appends
        self.assertEqual(identity_segment("us-central1", "path"), "us-central1")
        # an invalid override contributes nothing (falls back to global)
        self.assertEqual(identity_segment("global", "invalid"), "")


if __name__ == "__main__":
    unittest.main()
