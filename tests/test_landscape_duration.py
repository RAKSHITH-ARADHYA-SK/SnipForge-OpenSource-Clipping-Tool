import unittest
from studio import render, jobs, creators


class TestLandscapeAndDuration(unittest.TestCase):
    def test_duration_limits(self):
        # 10 minutes = 600 seconds
        self.assertEqual(jobs.LIMITS["max_len"], (10, 600))
        self.assertEqual(creators.NUMERIC["max_len"], (10, 600))

        # Test clean_options accepts max_len up to 600
        opts = jobs.clean_options({"max_len": 600, "min_len": 120, "aspect": "16:9"})
        self.assertEqual(opts["max_len"], 600)
        self.assertEqual(opts["min_len"], 120)
        self.assertEqual(opts["aspect"], "16:9")

        # Test clean_options accepts auto
        opts_auto = jobs.clean_options({"aspect": "auto"})
        self.assertEqual(opts_auto["aspect"], "auto")

        # Test invalid aspect falls back to default 9:16
        opts_invalid = jobs.clean_options({"aspect": "4:3"})
        self.assertEqual(opts_invalid["aspect"], "9:16")

    def test_geometry_16_9(self):
        # 16:9 landscape video
        layout, fg_h, hook_y, cap_y = render.geometry(1920, 1080, "blur", aspect="16:9")
        self.assertEqual(layout, "crop")
        self.assertEqual(fg_h, 1080)
        self.assertEqual(cap_y, 920)

        # 9:16 vertical video
        layout_v, fg_h_v, hook_y_v, cap_y_v = render.geometry(1080, 1920, "crop", aspect="9:16")
        self.assertEqual(layout_v, "crop")
        self.assertEqual(fg_h_v, 1920)
        self.assertEqual(hook_y_v, 300)
        self.assertEqual(cap_y_v, 1380)

    def test_build_ass_big_text_omitted_in_16_9(self):
        words = [{"w": "Hello", "s": 0.5, "e": 1.0}, {"w": "World", "s": 1.1, "e": 1.5}]
        hook = "VIRAL HOOK HEADER"

        # In 16:9: big text (hook) MUST NOT be in the ASS events!
        ass_16_9 = render.build_ass(words, 0, 2.0, hook, "bounce", [], 180, 920, aspect="16:9")
        self.assertIn("PlayResX: 1920", ass_16_9)
        self.assertIn("PlayResY: 1080", ass_16_9)
        self.assertNotIn("VIRAL HOOK HEADER", ass_16_9)
        self.assertNotIn("Style: Hook", [line for line in ass_16_9.splitlines() if line.startswith("Dialogue:") and ",Hook," in line])
        self.assertIn("HELLO", ass_16_9)
        self.assertIn("WORLD", ass_16_9)

        # In 9:16: big text (hook) MUST be present!
        ass_9_16 = render.build_ass(words, 0, 2.0, hook, "bounce", [], 300, 1380, aspect="9:16")
        self.assertIn("PlayResX: 1080", ass_9_16)
        self.assertIn("PlayResY: 1920", ass_9_16)
        self.assertIn("VIRAL HOOK HEADER", ass_9_16)

    def test_filter_generation(self):
        fc_16_9 = render._filter("blur", 1080, aspect="16:9")
        self.assertIn("scale=1920:1080", fc_16_9)

        fc_9_16 = render._filter("blur", 1080, aspect="9:16")
        self.assertIn("scale=1080:1920", fc_9_16)

    def test_request_render_duration_validation(self):
        # Fake job setup
        jobs._jobs["dummy_job"] = {
            "id": "dummy_job", "user": "test_user", "clips": [
                {"id": "clip01", "start": 0.0, "end": 60.0, "status": "done", "aspect": "16:9"}
            ],
            "dir": ".",
            "width": 1920,
            "height": 1080,
            "options": {"aspect": "16:9"}
        }
        try:
            # 600s duration is accepted
            jobs.request_render("dummy_job", "clip01", {"start": 0, "end": 600, "aspect": "16:9"})
            clip = jobs._jobs["dummy_job"]["clips"][0]
            self.assertEqual(clip["end"] - clip["start"], 600)
            self.assertEqual(clip["aspect"], "16:9")

            # Re-rendering without passing aspect in changes keeps aspect: 16:9
            jobs.request_render("dummy_job", "clip01", {"start": 10, "end": 70})
            self.assertEqual(clip["aspect"], "16:9")

            # If clip had no aspect set, it falls back to 16:9 from landscape video geometry
            del clip["aspect"]
            jobs.request_render("dummy_job", "clip01", {"start": 10, "end": 80})
            self.assertEqual(clip["aspect"], "16:9")

            # >600s duration raises ValueError
            with self.assertRaises(ValueError):
                jobs.request_render("dummy_job", "clip01", {"start": 0, "end": 601})
        finally:
            jobs._jobs.pop("dummy_job", None)


if __name__ == "__main__":
    unittest.main()
