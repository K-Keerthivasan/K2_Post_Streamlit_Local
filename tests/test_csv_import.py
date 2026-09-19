from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import csv_import


WIDE = (
    "Title,Subtitle,Slide 1 Heading,slide1_body,slide2_heading,slide2_body,"
    "CTA,caption,tags,image\n"
    "Five myths,What matters,FIRST,alpha,SECOND,beta,Save this,A caption,"
    '"seo, marketing",laptop desk\n'
)

LONG = (
    "post_id,order,heading,body,image_query\n"
    "myths,2,SECOND,beta,office\n"
    "myths,1,FIRST,alpha,laptop\n"
    "speed,1,SLOW,gamma,server\n"
)


class ReadCsvTests(unittest.TestCase):
    def test_strips_bom_and_normalises_newlines(self):
        raw = "﻿title,body\nHello,\"one\r\ntwo\"\n".encode("utf-8")
        headers, rows = csv_import.read_csv(raw)

        self.assertEqual(headers, ["title", "body"])
        self.assertEqual(rows[0]["body"], "one\ntwo")

    def test_detects_semicolon_delimiter(self):
        headers, rows = csv_import.read_csv("title;caption\nHi;There\n")

        self.assertEqual(headers, ["title", "caption"])
        self.assertEqual(rows[0]["caption"], "There")

    def test_blank_rows_are_dropped(self):
        _, rows = csv_import.read_csv("title\nA\n\n \nB\n")

        self.assertEqual([r["title"] for r in rows], ["A", "B"])

    def test_empty_file_is_an_error(self):
        with self.assertRaises(csv_import.CsvImportError):
            csv_import.read_csv("   ")

    def test_header_without_rows_is_an_error(self):
        with self.assertRaises(csv_import.CsvImportError):
            csv_import.read_csv("title,caption\n")


class ShapeDetectionTests(unittest.TestCase):
    def test_wide_sheet(self):
        headers, _ = csv_import.read_csv(WIDE)
        self.assertEqual(csv_import.detect_shape(headers), "wide")

    def test_long_sheet(self):
        headers, _ = csv_import.read_csv(LONG)
        self.assertEqual(csv_import.detect_shape(headers), "long")

    def test_column_named_for_a_field_beats_an_alias(self):
        # "body" is an alias of the slide body AND could read as a brief; a column
        # named exactly "body" must map to the field of the same name.
        self.assertEqual(csv_import._field_for("body"), "body")
        self.assertEqual(csv_import._field_for("notes"), "notes")

    def test_slide_columns_are_matched_loosely(self):
        self.assertEqual(csv_import._slide_col("Slide 1 Heading"), (1, "heading"))
        self.assertEqual(csv_import._slide_col("slide2"), (2, "body"))
        self.assertEqual(csv_import._slide_col("s3_image"), (3, "image_query"))
        self.assertIsNone(csv_import._slide_col("caption"))


class ToPostsTests(unittest.TestCase):
    def test_wide_row_becomes_one_post_with_ordered_slides(self):
        headers, rows = csv_import.read_csv(WIDE)
        posts = csv_import.to_posts(headers, rows)

        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["title"], "Five myths")
        self.assertEqual(post["subtitle"], "What matters")
        self.assertEqual(post["cta"], "Save this")
        self.assertEqual(post["hashtags"], ["seo", "marketing"])
        self.assertEqual(post["image_query"], "laptop desk")
        self.assertEqual([s["heading"] for s in post["slides"]], ["FIRST", "SECOND"])
        self.assertEqual([s["body"] for s in post["slides"]], ["alpha", "beta"])

    def test_long_rows_group_by_post_id_and_sort_by_order(self):
        headers, rows = csv_import.read_csv(LONG)
        posts = csv_import.to_posts(headers, rows)

        self.assertEqual(len(posts), 2)
        self.assertEqual([s["heading"] for s in posts[0]["slides"]], ["FIRST", "SECOND"])
        self.assertEqual(posts[1]["title"], "speed")

    def test_wide_sheet_without_slide_columns_keeps_body_as_a_brief(self):
        headers, rows = csv_import.read_csv("title,body\nIdea,Some notes here\n")
        post = csv_import.to_posts(headers, rows)[0]

        self.assertEqual(post["notes"], "Some notes here")
        self.assertEqual(post["slides"], [])

    def test_hashtags_accept_hashes_and_mixed_separators(self):
        headers, rows = csv_import.read_csv('title,hashtags\nA,"#one #two,three"\n')
        post = csv_import.to_posts(headers, rows)[0]

        self.assertEqual(post["hashtags"], ["one", "two", "three"])


class ToPlanTests(unittest.TestCase):
    brand = {"handle": "@demo", "hashtags": ["fallback"]}

    def _plan(self, csv_text=WIDE, fmt="carousel"):
        headers, rows = csv_import.read_csv(csv_text)
        post = csv_import.to_posts(headers, rows)[0]
        return csv_import.to_plan(post, self.brand, fmt=fmt)

    def test_carousel_plan_uses_the_sheet_copy_verbatim(self):
        plan = self._plan()

        self.assertEqual(plan["title_card"]["headline"], "Five myths")
        self.assertEqual(plan["content_slides"][0]["body"], "alpha")
        self.assertEqual(plan["caption"], "A caption")
        self.assertEqual(plan["outro_card"]["handle"], "@demo")
        # title + 2 content + outro
        self.assertEqual(plan["slide_count"], 4)

    def test_headings_are_upper_cased_for_the_template(self):
        headers, rows = csv_import.read_csv(
            "title,slide1_heading,slide1_body\nA,lower case,text\n")
        post = csv_import.to_posts(headers, rows)[0]
        plan = csv_import.to_plan(post, self.brand)

        self.assertEqual(plan["content_slides"][0]["heading"], "LOWER CASE")

    def test_missing_hashtags_fall_back_to_the_brand(self):
        headers, rows = csv_import.read_csv("title,slide1_body\nA,text\n")
        post = csv_import.to_posts(headers, rows)[0]
        plan = csv_import.to_plan(post, self.brand)

        self.assertEqual(plan["hashtags"], ["fallback"])

    def test_single_card_format_gets_a_flat_plan(self):
        plan = self._plan(fmt="square")

        self.assertEqual(plan["headline"], "Five myths")
        self.assertEqual(plan["body"], "alpha")
        self.assertNotIn("content_slides", plan)

    def test_plans_are_marked_as_csv_origin(self):
        self.assertEqual(self._plan()["origin"], "csv")


class ToStoryTests(unittest.TestCase):
    def test_slides_become_the_brief_when_there_are_no_notes(self):
        headers, rows = csv_import.read_csv(WIDE)
        post = csv_import.to_posts(headers, rows)[0]
        story = csv_import.to_story(post)

        self.assertEqual(story["title"], "Five myths")
        self.assertIn("FIRST: alpha", story["summary"])
        self.assertIn("What matters", story["summary"])

    def test_notes_win_when_present(self):
        headers, rows = csv_import.read_csv("title,notes\nA,The real brief\n")
        post = csv_import.to_posts(headers, rows)[0]

        self.assertEqual(csv_import.to_story(post)["summary"], "The real brief")


class TemplateTests(unittest.TestCase):
    def test_the_shipped_template_parses_into_one_post(self):
        headers, rows = csv_import.read_csv(csv_import.TEMPLATE_CSV)
        info = csv_import.inspect(headers, rows)

        self.assertEqual(info["shape"], "wide")
        self.assertEqual(info["post_count"], 1)
        self.assertEqual(info["unmapped"], [])
        self.assertEqual(len(info["posts"][0]["slides"]), 3)


if __name__ == "__main__":
    unittest.main()
