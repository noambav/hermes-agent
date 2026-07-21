"""Tests for the last30days optional skill (frontmatter + script logic).

No live network calls: urllib.request.urlopen is mocked everywhere.
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent.parent / "optional-skills" / "research" / "last30days"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "fetch_sources.py"

sys.path.insert(0, str(SCRIPT.parent))
import fetch_sources  # noqa: E402


def _fake_response(payload):
    body = json.dumps(payload).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    return _Resp(body)


class TestFrontmatter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = SKILL_MD.read_text(encoding="utf-8")
        assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
        cls.fm = yaml.safe_load(text.split("---", 2)[1])
        cls.body = text.split("---", 2)[2]

    def test_required_fields(self):
        for field in ("name", "description", "version", "author", "license", "platforms"):
            self.assertIn(field, self.fm, f"missing frontmatter field: {field}")

    def test_name(self):
        self.assertEqual(self.fm["name"], "last30days")

    def test_description_length_and_shape(self):
        desc = self.fm["description"]
        self.assertLessEqual(len(desc), 60, f"description is {len(desc)} chars (max 60)")
        self.assertTrue(desc.endswith("."), "description must end with a period")

    def test_version_and_license(self):
        self.assertEqual(str(self.fm["version"]), "0.1.0")
        self.assertEqual(self.fm["license"], "MIT")

    def test_platforms(self):
        self.assertEqual(set(self.fm["platforms"]), {"linux", "macos", "windows"})

    def test_hermes_metadata(self):
        hermes = self.fm["metadata"]["hermes"]
        self.assertIsInstance(hermes["tags"], list)
        self.assertTrue(hermes["tags"])
        self.assertIsInstance(hermes["related_skills"], list)

    def test_body_sections_present(self):
        for section in ("## When to Use", "## Prerequisites", "## How to Run",
                        "## Quick Reference", "## Procedure", "## Pitfalls",
                        "## Verification"):
            self.assertIn(section, self.body, f"missing section: {section}")


class TestEngagementScore(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(fetch_sources.engagement_score(0, 0), 0.0)

    def test_capped_at_one(self):
        self.assertEqual(fetch_sources.engagement_score(10_000, 10_000), 1.0)

    def test_weighting(self):
        # 250/500 * 0.6 + 100/200 * 0.4 = 0.3 + 0.2 = 0.5
        self.assertEqual(fetch_sources.engagement_score(250, 100), 0.5)

    def test_negative_clamped(self):
        self.assertEqual(fetch_sources.engagement_score(-50, -5), 0.0)


class TestWindow(unittest.TestCase):
    def test_date_to_unix_utc(self):
        self.assertEqual(fetch_sources.date_to_unix("1970-01-02"), 86400)

    def test_window_ordering(self):
        start, end = fetch_sources.window(30)
        self.assertLess(start, end)


class TestHackerNews(unittest.TestCase):
    def test_filters_low_points_and_sorts(self):
        payload = {"hits": [
            {"title": "Low", "objectID": "1", "points": 1, "num_comments": 0,
             "created_at": "2026-07-01T00:00:00Z"},
            {"title": "Mid", "objectID": "2", "points": 50, "num_comments": 10,
             "created_at": "2026-07-02T00:00:00Z", "url": "https://example.com/mid"},
            {"title": "High", "objectID": "3", "points": 400, "num_comments": 150,
             "created_at": "2026-07-03T00:00:00Z"},
        ]}
        with mock.patch.object(fetch_sources.urllib.request, "urlopen",
                               return_value=_fake_response(payload)):
            items = fetch_sources.search_hackernews("test topic", "2026-06-21", "2026-07-21")
        self.assertEqual([i["title"] for i in items], ["High", "Mid"])
        self.assertEqual(items[0]["discussion_url"],
                         "https://news.ycombinator.com/item?id=3")
        # story without a url falls back to the HN discussion link
        self.assertEqual(items[0]["url"], "https://news.ycombinator.com/item?id=3")
        self.assertEqual(items[1]["url"], "https://example.com/mid")

    def test_multiword_query_sets_optional_words(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            return _fake_response({"hits": []})

        with mock.patch.object(fetch_sources.urllib.request, "urlopen", fake_urlopen):
            fetch_sources.search_hackernews("alpha beta gamma", "2026-06-21", "2026-07-21")
        self.assertIn("optionalWords=beta+gamma", captured["url"])
        self.assertIn("numericFilters=created_at_i%3E", captured["url"])


class TestReddit(unittest.TestCase):
    PAYLOAD = {"data": {"children": [
        {"data": {"title": "Post A", "permalink": "/r/test/comments/a/", "score": 500,
                  "num_comments": 200, "subreddit": "test", "created_utc": 86400}},
        {"data": {"title": "Dup A", "permalink": "/r/test/comments/a/", "score": 1,
                  "num_comments": 0, "subreddit": "test", "created_utc": 86400}},
        {"data": {"title": "Post B", "permalink": "/r/test/comments/b/", "score": 10,
                  "num_comments": 2, "subreddit": "test", "created_utc": 86400}},
    ]}}

    def test_dedupes_by_url_and_scores(self):
        with mock.patch.object(fetch_sources.urllib.request, "urlopen",
                               return_value=_fake_response(self.PAYLOAD)):
            items = fetch_sources.search_reddit("test")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Post A")
        self.assertEqual(items[0]["relevance"], 1.0)
        self.assertEqual(items[0]["date"], "1970-01-02")

    def test_subreddit_scoping(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            return _fake_response({"data": {"children": []}})

        with mock.patch.object(fetch_sources.urllib.request, "urlopen", fake_urlopen):
            fetch_sources.search_reddit("topic", subreddit="r/LocalLLaMA")
        self.assertIn("/r/LocalLLaMA/search.json", captured["url"])
        self.assertIn("restrict_sr=on", captured["url"])


class TestPolymarket(unittest.TestCase):
    def test_dedupes_across_pages_and_sorts_by_volume(self):
        pages = [
            {"events": [{"id": "1", "slug": "small", "title": "Small", "volume1mo": 100},
                        {"id": "2", "slug": "big", "title": "Big", "volume1mo": 9000}]},
            {"events": [{"id": "1", "slug": "small", "title": "Small", "volume1mo": 100}]},
            {"events": []},
        ]
        responses = iter(pages)
        with mock.patch.object(fetch_sources.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _fake_response(next(responses))):
            items = fetch_sources.search_polymarket("election")
        self.assertEqual([i["title"] for i in items], ["Big", "Small"])
        self.assertEqual(items[0]["url"], "https://polymarket.com/event/big")


class TestMainDegradedMode(unittest.TestCase):
    def test_source_error_is_partial_not_fatal(self):
        """A failing source annotates status and exit stays 0 (upstream behavior)."""
        def fake_urlopen(req, timeout=0):
            raise fetch_sources.urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", None, None)

        stdout = io.StringIO()
        with mock.patch.object(fetch_sources.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(sys, "stdout", stdout), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            rc = fetch_sources.main(["topic", "--sources", "reddit", "--format", "json"])
        self.assertEqual(rc, 0)
        out = json.loads(stdout.getvalue())
        self.assertIn("error", out["results"]["reddit"]["status"])
        self.assertEqual(out["results"]["reddit"]["items"], [])

    def test_markdown_output_shape(self):
        payload = {"hits": [{"title": "T", "objectID": "9", "points": 10,
                             "num_comments": 3, "created_at": "2026-07-01T00:00:00Z"}]}
        stdout = io.StringIO()
        with mock.patch.object(fetch_sources.urllib.request, "urlopen",
                               return_value=_fake_response(payload)), \
             mock.patch.object(sys, "stdout", stdout):
            rc = fetch_sources.main(["topic", "--sources", "hackernews"])
        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        self.assertIn("# last30days: topic", text)
        self.assertIn("## hackernews (1 items, status: ok)", text)


if __name__ == "__main__":
    unittest.main()
