import unittest

import md2tg


def count(html, tag):
    if tag.startswith("</"):
        return html.count(tag)
    return html.count("<%s>" % tag) + html.count("<%s " % tag)


class TestConvert(unittest.TestCase):
    def test_escaping(self):
        out = md2tg.convert("<script>alert('x') & \"quotes\"</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)

    def test_bold_italic_strike_spoiler(self):
        out = md2tg.convert("**bold** and *em* and ~~gone~~ and ||secret||")
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<i>em</i>", out)
        self.assertIn("<s>gone</s>", out)
        self.assertIn("<tg-spoiler>secret</tg-spoiler>", out)

    def test_snake_case_not_italic(self):
        out = md2tg.convert("snake_case and file_name.txt stay")
        self.assertNotIn("<i>", out)

    def test_inline_code_protected(self):
        out = md2tg.convert("use `x = a**b` here")
        self.assertIn("<code>x = a**b</code>", out)
        self.assertNotIn("<b>", out)

    def test_code_fence_with_lang(self):
        out = md2tg.convert("```python\nprint('<hi & bye>')\n```")
        self.assertIn('<pre><code class="language-python">', out)
        self.assertIn("print('&lt;hi &amp; bye&gt;')", out)

    def test_link(self):
        out = md2tg.convert("see [docs](https://opencode.ai) now")
        self.assertIn('<a href="https://opencode.ai">docs</a>', out)

    def test_bare_url_with_query(self):
        out = md2tg.convert("go to https://example.com?a=1&b=2 ok")
        self.assertIn('<a href="https://example.com?a=1&amp;b=2">https://example.com?a=1&amp;b=2</a>', out)

    def test_header(self):
        self.assertEqual(md2tg.convert("# Title"), "<b>Title</b>")

    def test_blockquote(self):
        out = md2tg.convert("> line one\n> line two")
        self.assertIn("<blockquote>line one\nline two</blockquote>", out)

    def test_bold_in_link_text(self):
        out = md2tg.convert("[**big** deal](https://x.com)")
        self.assertIn('<a href="https://x.com"><b>big</b> deal</a>', out)


class TestSplit(unittest.TestCase):
    def test_short_untouched(self):
        parts = md2tg.split("hello **world**")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0], "hello <b>world</b>")

    def test_many_paragraphs_fit(self):
        md = "\n\n".join("para %d %s" % (i, "x" * 200) for i in range(60))
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)

    def test_huge_code_block_reopens_fence(self):
        body = "\n".join("line %d of code" % i for i in range(2000))
        md = "before\n```python\n%s\n```\nafter" % body
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)
            # every chunk containing code must be balanced (no cut-off tags)
            self.assertEqual(count(p, "pre"), count(p, "</pre>"))
        # first and last chunk contain the surrounding text
        self.assertIn("before", parts[0])
        self.assertIn("after", parts[-1])

    def test_monster_line(self):
        md = "word " * 3000  # one huge line
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)

    def test_empty(self):
        self.assertEqual(md2tg.split(""), [""])


class TestNesting(unittest.TestCase):
    """The HTTP 400 bug: interleaved tags from regex conversion."""

    def test_italic_bold_interleave(self):
        out = md2tg.convert("*see **this* now**")
        # must not contain </i> followed by </b> in a way that crosses: b opened inside i must close inside i
        self.assertEqual(count(out, "i"), count(out, "</i>"))
        self.assertEqual(count(out, "b"), count(out, "</b>"))

    def test_all_weird_orders_stay_balanced(self):
        cases = [
            "*a **b* c**",
            "**x *y** z*",
            "*a* **b** *c*",
            "**bold with ~~strike~~ and ||spoiler||**",
            "*outer **inner ~~deep~~* end**",
        ]
        for c in cases:
            out = md2tg.convert(c)
            for tag in ("b", "i", "s", "tg-spoiler"):
                self.assertEqual(count(out, tag), count(out, "</%s>" % tag), msg=c)

    def test_stray_closer_dropped(self):
        out = md2tg._balance("hello </b> world")
        self.assertNotIn("</b>", out)
        self.assertIn("hello", out)


class TestLists(unittest.TestCase):
    def test_dash_list_bullets(self):
        out = md2tg.convert("- one\n- two")
        self.assertEqual(out, "\u2022 one\n\u2022 two")

    def test_star_and_plus_lists(self):
        self.assertEqual(md2tg.convert("* one"), "\u2022 one")
        self.assertEqual(md2tg.convert("+ one"), "\u2022 one")

    def test_nested_lists(self):
        out = md2tg.convert("- top\n  - sub\n- top2")
        lines = out.split("\n")
        self.assertEqual(lines[0], "\u2022 top")
        self.assertEqual(lines[1], "  - sub")
        self.assertEqual(lines[2], "\u2022 top2")

    def test_nested_lists_with_bold(self):
        out = md2tg.convert("- **bold item**\n  - plain sub")
        lines = out.split("\n")
        self.assertEqual(lines[0], "\u2022 <b>bold item</b>")
        self.assertEqual(lines[1], "  - plain sub")

    def test_not_a_list(self):
        self.assertEqual(md2tg.convert("-"), "-")
        self.assertEqual(md2tg.convert("---"), "---")


class TestTables(unittest.TestCase):
    def test_table_as_aligned_pre(self):
        md = "| Name | Age |\n|---|---|\n| Alice | 30 |\n| Bob | 5 |"
        out = md2tg.convert(md)
        self.assertIn("<pre>", out)
        self.assertIn("Name   Age", out)
        self.assertIn("----------", out)
        self.assertIn("Alice  30", out)
        self.assertIn("Bob    5", out)

    def test_table_escapes_html(self):
        md = "| A | B |\n|---|---|\n| <x> | 1 |"
        out = md2tg.convert(md)
        self.assertIn("&lt;x&gt;", out)
        self.assertNotIn("<x>", out.replace("<pre>", "").replace("</pre>", ""))

    def test_non_table_pipes_untouched(self):
        out = md2tg.convert("a | b without table\nno separator below\nplain")
        self.assertNotIn("<pre>", out)
class TestThinking(unittest.TestCase):
    def test_send_parts_thinking_first(self):
        parts = md2tg.send_parts("The answer is **42**.", "Let me think about this...")
        self.assertTrue(parts[0].startswith("<blockquote expandable>"))
        self.assertIn("Let me think", parts[0])
        self.assertIn("The answer is <b>42</b>.", parts[-1])

    def test_send_parts_no_thinking(self):
        parts = md2tg.send_parts("answer only", None)
        self.assertEqual(len(parts), 1)
        self.assertNotIn("blockquote", parts[0])

    def test_send_parts_empty_thinking(self):
        parts = md2tg.send_parts("answer", "  ")
        self.assertEqual(len(parts), 1)

    def test_long_thinking_fits(self):
        thinking = "reasoning line %d\n" * 400 % tuple(range(400))
        answer = "ok"
        parts = md2tg.send_parts(answer, thinking)
        for p in parts:
            self.assertLessEqual(len(p), 4096)


class TestRichSplit(unittest.TestCase):
    def test_small_untouched(self):
        self.assertEqual(md2tg.split_rich("# hi\n\nbody"), ["# hi\n\nbody"])

    def test_large_markdown_chunked(self):
        md = "\n\n".join("para %d\n%s" % (i, "y" * 300) for i in range(300))
        parts = md2tg.split_rich(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 30000)

    def test_code_fence_intact(self):
        body = "\n".join("code line %d" % i for i in range(4000))
        md = "intro\n```python\n%s\n```" % body
        parts = md2tg.split_rich(md, limit=10000)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 10000)
            if "```python" in p or "```" in p:
                self.assertEqual(p.count("```") % 2, 0, msg=p[:80])


if __name__ == "__main__":
    unittest.main()


class TestRichDollars(unittest.TestCase):
    """Rich Markdown reads "$...$" as inline LaTeX, so the money range
    "$0.1 - $0.2" renders as italic math "0.1 - " and the second amount
    loses its sign. escape_rich_dollars sends GFM's backslash escape
    instead, leaving code, links and real display math alone."""

    def test_money_range_is_escaped(self):
        self.assertEqual(md2tg.escape_rich_dollars("cost $0.1 - $0.2"),
                         "cost \\$0.1 - \\$0.2")

    def test_text_without_dollars_is_returned_untouched(self):
        src = "plain **text** with `code` and _italics_"
        self.assertIs(md2tg.escape_rich_dollars(src), src)

    def test_second_pass_changes_nothing(self):
        once = md2tg.escape_rich_dollars("$5 between $7 and $9")
        self.assertEqual(md2tg.escape_rich_dollars(once), once)

    def test_code_spans_and_fences_stay_literal(self):
        for src in ("`echo $PATH`", "```\necho $HOME\n```", "~~~\n$a\n~~~",
                    "```bash\nawk '{print $1}'\n```"):
            self.assertEqual(md2tg.escape_rich_dollars(src), src)

    def test_display_math_is_left_as_math(self):
        self.assertEqual(md2tg.escape_rich_dollars("$$E=mc^2$$"),
                         "$$E=mc^2$$")

    def test_unclosed_display_math_becomes_literal(self):
        # no second "$$", so there is no math span to protect: escape both
        self.assertEqual(md2tg.escape_rich_dollars("$$x^2"), "\\$\\$x^2")

    def test_bare_urls_keep_their_dollar(self):
        src = "see https://example.dev/a$b now"
        self.assertEqual(md2tg.escape_rich_dollars(src), src)

    def test_split_rich_measures_the_escaped_source(self):
        md = "\n\n".join(["$1"] * 30)
        chunks = md2tg.split_rich(md, limit=40)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 40)
            self.assertNotIn("$1", chunk.replace("\\$1", ""))
