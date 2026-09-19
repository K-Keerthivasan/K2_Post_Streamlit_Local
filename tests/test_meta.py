from pathlib import Path
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import meta


CONFIG = {"meta": {"accounts": {
    "brand_a": {"ig_user_id": "111", "fb_page_id": "222", "targets": ["instagram"]},
    "brand_b": {"ig_user_id": "333", "targets": ["instagram", "facebook"]},
}}}


class LoadConfigTests(unittest.TestCase):
    def test_defaults_fill_in_around_the_user_block(self):
        cfg = meta.load_config(CONFIG)

        self.assertEqual(cfg["api_version"], "v21.0")
        self.assertEqual(cfg["publish"]["on_approve"], "hold")
        self.assertEqual(cfg["rate_limit"]["max_posts_per_day"], meta.IG_DAILY_POSTS)

    def test_user_values_win_over_defaults(self):
        cfg = meta.load_config({"meta": {"api_version": "v19.0",
                                         "publish": {"on_approve": "now"}}})

        self.assertEqual(cfg["api_version"], "v19.0")
        self.assertEqual(cfg["publish"]["on_approve"], "now")
        # Untouched sibling keys survive the merge.
        self.assertIn("default_targets", cfg["publish"])


class AccountResolutionTests(unittest.TestCase):
    def test_known_brand_resolves(self):
        acct = meta.account_for("brand_a", meta.load_config(CONFIG))

        self.assertEqual(acct["ig_user_id"], "111")

    def test_unmapped_brand_never_falls_back_to_another_account(self):
        # Publishing brand B's post to brand A's Instagram is worse than an error.
        single = {"meta": {"accounts": {"brand_a": {"ig_user_id": "111"}}}}
        with self.assertRaises(meta.MetaError) as ctx:
            meta.account_for("brand_b", meta.load_config(single))

        self.assertIn("brand_b", str(ctx.exception))

    def test_missing_token_is_a_clear_error(self):
        cfg = meta.load_config(CONFIG)
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": ""}, clear=False):
            with self.assertRaises(meta.MetaError) as ctx:
                meta.token_for({}, cfg)

        self.assertIn("META_ACCESS_TOKEN", str(ctx.exception))

    def test_per_account_token_env_wins(self):
        cfg = meta.load_config(CONFIG)
        with mock.patch.dict(os.environ, {"BRAND_B_TOKEN": "xyz"}, clear=False):
            self.assertEqual(meta.token_for({"token_env": "BRAND_B_TOKEN"}, cfg), "xyz")

    def test_configured_reflects_whether_any_token_is_present(self):
        cfg = meta.load_config(CONFIG)
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": ""}, clear=False):
            self.assertFalse(meta.configured(cfg))
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t"}, clear=False):
            self.assertTrue(meta.configured(cfg))


class CaptionTests(unittest.TestCase):
    def test_hashtags_are_appended_with_hashes(self):
        text = meta._clean_caption("Hello", ["one", "#two"])

        self.assertEqual(text, "Hello\n\n#one #two")

    def test_caption_is_trimmed_to_instagrams_limit(self):
        text = meta._clean_caption("x" * (meta.IG_CAPTION_MAX + 500))

        self.assertEqual(len(text), meta.IG_CAPTION_MAX)
        self.assertTrue(text.endswith("…"))


class PublicUrlTests(unittest.TestCase):
    def test_no_urls_explains_public_base_url(self):
        with self.assertRaises(meta.MetaError) as ctx:
            meta._require_public_urls([])

        self.assertIn("PUBLIC_BASE_URL", str(ctx.exception))

    def test_localhost_is_rejected_before_calling_meta(self):
        with self.assertRaises(meta.MetaError) as ctx:
            meta._require_public_urls(["http://localhost:8000/outputs/a.png"])

        self.assertIn("localhost", str(ctx.exception))

    def test_public_urls_pass_through(self):
        urls = ["https://example.test/outputs/a.png"]

        self.assertEqual(meta._require_public_urls(urls), urls)


class AssetValidationTests(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(meta.MetaError):
            meta._validate_assets(["definitely-not-here.png"])

    def test_video_assets_are_refused(self):
        with mock.patch.object(Path, "exists", return_value=True):
            with self.assertRaises(meta.MetaError) as ctx:
                meta._validate_assets(["clip.mp4"])

        self.assertIn("images only", str(ctx.exception))


class PublishGuardTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "tok"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_nothing_to_publish(self):
        with self.assertRaises(meta.MetaError):
            meta.publish(brand="brand_a", ptype="post", config=CONFIG)

    def test_unknown_post_type(self):
        with self.assertRaises(meta.MetaError):
            meta.publish(brand="brand_a", ptype="reel",
                         asset_urls=["https://x.test/a.png"], config=CONFIG)

    def test_carousel_image_count_is_bounded(self):
        res = meta.publish(brand="brand_a", ptype="carousel",
                           asset_urls=["https://x.test/a.png"], config=CONFIG)

        self.assertFalse(res["ok"])
        self.assertIn("2-10 images", res["errors"][0]["error"])

    def test_one_target_failing_does_not_hide_the_other(self):
        # brand_b has no fb_page_id, so Facebook must fail on its own.
        with mock.patch.object(meta, "publish_instagram",
                               return_value={"platform": "instagram", "media_id": "1"}):
            res = meta.publish(brand="brand_b", ptype="post",
                               asset_urls=["https://x.test/a.png"], config=CONFIG)

        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["errors"][0]["platform"], "facebook")
        self.assertFalse(res["ok"])

    def test_dry_run_reports_without_calling_meta(self):
        with mock.patch.object(meta, "_graph",
                               side_effect=AssertionError("must not call the API")):
            res = meta.publish(brand="brand_a", ptype="carousel",
                               asset_urls=["https://x.test/a.png", "https://x.test/b.png"],
                               caption="hi", config=CONFIG, dry_run=True)

        self.assertTrue(res["dry_run"])
        self.assertEqual(res["targets"], ["instagram"])
        self.assertEqual(res["caption_chars"], 2)

    def test_explicit_targets_override_the_account_default(self):
        with mock.patch.object(meta, "publish_facebook",
                               return_value={"platform": "facebook", "media_id": "9"}) as fb:
            res = meta.publish(brand="brand_a", ptype="post",
                               asset_urls=["https://x.test/a.png"],
                               targets=["facebook"], config=CONFIG)

        self.assertTrue(fb.called)
        self.assertTrue(res["ok"])


class RateGuardTests(unittest.TestCase):
    def test_ceiling_blocks_further_publishes(self):
        cfg = meta.load_config({"meta": {"rate_limit": {"max_posts_per_day": 3,
                                                        "safety_margin": 1}}})
        import time
        stamps = {"ig:111": [time.time()] * 2}       # ceiling = 3 - 1 = 2
        with mock.patch.object(meta, "_rate_load", return_value=stamps):
            with self.assertRaises(meta.MetaError) as ctx:
                meta._rate_check("ig:111", cfg)

        self.assertIn("last 24h", str(ctx.exception))

    def test_stamps_older_than_a_day_do_not_count(self):
        cfg = meta.load_config({"meta": {"rate_limit": {"max_posts_per_day": 3,
                                                        "safety_margin": 1}}})
        import time
        stamps = {"ig:111": [time.time() - 90000] * 5}
        with mock.patch.object(meta, "_rate_load", return_value=stamps):
            meta._rate_check("ig:111", cfg)          # must not raise


class GraphErrorTests(unittest.TestCase):
    def test_meta_error_text_is_surfaced(self):
        response = mock.Mock(status_code=400)
        response.json.return_value = {"error": {"code": 190, "error_subcode": 463,
                                                "message": "Session has expired"}}
        with mock.patch.object(meta.requests, "get", return_value=response):
            with self.assertRaises(meta.MetaError) as ctx:
                meta._graph("GET", "me", meta.load_config({}), token="t")

        msg = str(ctx.exception)
        self.assertIn("190/463", msg)
        self.assertIn("Session has expired", msg)

    def test_non_json_response(self):
        response = mock.Mock(status_code=502)
        response.json.side_effect = ValueError
        response.text = "<html>bad gateway</html>"
        with mock.patch.object(meta.requests, "get", return_value=response):
            with self.assertRaises(meta.MetaError) as ctx:
                meta._graph("GET", "me", meta.load_config({}), token="t")

        self.assertIn("non-JSON", str(ctx.exception))


class LoadConfigIsIdempotentTests(unittest.TestCase):
    """A resolved meta block passed back in must not unwrap to empty defaults -
    that turned a configured machine into "no accounts configured"."""

    def test_resolved_block_survives_a_second_pass(self):
        once = meta.load_config(CONFIG)
        twice = meta.load_config(once)
        self.assertEqual(twice["accounts"], once["accounts"])

    def test_whole_config_still_unwraps(self):
        self.assertIn("brand_a", meta.load_config(CONFIG)["accounts"])


class PreflightTests(unittest.TestCase):
    """The constraints that decide whether a post can reach Instagram."""

    def _run(self, **kw):
        kw.setdefault("config", CONFIG)
        kw.setdefault("network", False)
        return meta.preflight(kw.pop("brand", "brand_a"), **kw)

    def _names(self, report):
        return {c["name"] for c in report["checks"] if not c["ok"]}

    def test_missing_token_blocks_and_says_so(self):
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run()
        self.assertFalse(report["ok"])
        self.assertIn("token.present", report["blocking"])

    def test_missing_ig_user_id_blocks(self):
        cfg = {"meta": {"accounts": {"brand_a": {"targets": ["instagram"]}}}}
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run(config=cfg)
        self.assertIn("account.ig_user_id", report["blocking"])

    def test_localhost_public_url_blocks_before_meta_sees_it(self):
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "http://localhost:8000"}, clear=False):
            report = self._run()
        self.assertIn("public_url.https", report["blocking"])
        self.assertIn("public_url.public", report["blocking"])

    def test_carousel_image_count_is_checked(self):
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run(ptype="carousel", asset_urls=["https://x.example/a.png"])
        self.assertIn("assets.count", report["blocking"])

    def test_too_many_hashtags_blocks(self):
        caption = "post " + " ".join(f"#tag{i}" for i in range(meta.IG_MAX_HASHTAGS + 5))
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run(caption=caption, asset_urls=["https://x.example/a.png"])
        self.assertIn("caption.hashtags", report["blocking"])

    def test_a_fully_configured_post_passes(self):
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run(caption="hello", asset_urls=["https://x.example/a.png"])
        self.assertTrue(report["ok"], self._names(report))

    def test_warnings_do_not_block(self):
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t",
                                          "PUBLIC_BASE_URL": "https://x.example"}, clear=False):
            report = self._run(caption="", asset_urls=["https://x.example/a.png"])
        self.assertTrue(report["ok"])
        self.assertIn("caption.present", self._names(report))


class AccountHostTests(unittest.TestCase):
    """Facebook-login and Instagram-login tokens live on different hosts and each
    host rejects the other's tokens, so the account decides the base URL."""

    def test_default_is_the_facebook_graph(self):
        base = meta.account_base({}, meta.load_config(CONFIG))
        self.assertTrue(base.startswith("https://graph.facebook.com"))

    def test_instagram_login_accounts_use_the_instagram_host(self):
        base = meta.account_base({"login": "instagram"}, meta.load_config(CONFIG))
        self.assertTrue(base.startswith(meta.IG_LOGIN_BASE))


class AppCredentialTests(unittest.TestCase):
    def test_facebook_pair_wins_when_both_are_present(self):
        env = {"META_APP_ID": "1", "META_APP_SECRET": "2",
               "INSTAGRAM_APP_ID": "3", "INSTAGRAM_APP_SECRET": "4"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(meta.app_credentials(), ("1", "2", "facebook"))

    def test_instagram_pair_is_used_when_it_is_the_only_one(self):
        env = {"META_APP_ID": "", "META_APP_SECRET": "",
               "INSTAGRAM_APP_ID": "3", "INSTAGRAM_APP_SECRET": "4"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(meta.app_credentials(), ("3", "4", "instagram"))

    def test_no_pair_is_a_clear_error(self):
        env = dict.fromkeys(["META_APP_ID", "META_APP_SECRET",
                             "INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"], "")
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(meta.MetaError) as ctx:
                meta.app_credentials()
        self.assertIn("INSTAGRAM_APP_ID", str(ctx.exception))

    def test_login_url_needs_https(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_APP_ID": "3"}, clear=False):
            with self.assertRaises(meta.MetaError):
                meta.ig_login_url("http://localhost:8000/cb")

    def test_login_url_carries_the_publish_scope(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_APP_ID": "3"}, clear=False):
            url = meta.ig_login_url("https://example.com/cb")
        self.assertIn("instagram_business_content_publish", url)
        self.assertIn("client_id=3", url)


if __name__ == "__main__":
    unittest.main()
