"""Regression tests for the stdlib GitHub Pages documentation builder."""

import errno
import importlib.util
import inspect
import os
import stat
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUILDER_PATH = _REPO_ROOT / ".github" / "scripts" / "build_docs.py"
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "_tableau_to_powerbi_build_docs", _BUILDER_PATH
)
if _BUILDER_SPEC is None or _BUILDER_SPEC.loader is None:
    raise ImportError(f"Cannot load documentation builder from {_BUILDER_PATH}")
build_docs = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(build_docs)


class _TagCollector(HTMLParser):
    """Collect actual HTML elements (escaped source tags remain text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class TestBuildDocsPublisher(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.src_dir = self.root / "docs"
        self.out_dir = self.root / "site"
        self.src_dir.mkdir()

    def _write_doc(self, name, content):
        path = self.src_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _build(self):
        build_docs.build(str(self.src_dir), str(self.out_dir))

    def _page(self, name="README.html"):
        return (self.out_dir / name).read_text(encoding="utf-8")

    def _assert_asset_rejected(self, case_name, relative_path, payload):
        case_root = self.root / case_name
        source_dir = case_root / "docs"
        output_dir = case_root / "site"
        source_dir.mkdir(parents=True)
        (source_dir / "README.md").write_text("# Synthetic docs\n", encoding="utf-8")
        asset = source_dir / "images" / relative_path
        asset.parent.mkdir(parents=True)
        if isinstance(payload, bytes):
            asset.write_bytes(payload)
        else:
            asset.write_text(payload, encoding="utf-8")

        with self.assertRaises(ValueError) as raised:
            build_docs.build(str(source_dir), str(output_dir))
        message = str(raised.exception)
        self.assertIn("Unsafe documentation image asset:", message)
        self.assertIn(str(asset), message)

    def _symlink_or_skip(self, link, target, *, is_directory=False):
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=is_directory)
        except NotImplementedError as exc:
            self.skipTest(f"Symlinks are not supported by this platform: {exc}")
        except OSError as exc:
            unsupported_permission = (
                exc.errno in (errno.EACCES, errno.EPERM)
                or getattr(exc, "winerror", None) in (5, 1314)
            )
            if unsupported_permission:
                self.skipTest(f"Symlink creation is not permitted here: {exc}")
            raise

    @staticmethod
    def _elements(page):
        parser = _TagCollector()
        parser.feed(page)
        parser.close()
        return parser.elements

    def test_build_keeps_the_two_argument_signature(self):
        self.assertEqual(
            list(inspect.signature(build_docs.build).parameters),
            ["src_dir", "out_dir"],
        )

    def test_markdown_image_attributes_are_rendered_and_escaped(self):
        self._write_doc(
            "README.md",
            '![badge" onerror="boom]'
            '(images/badge.svg?name="wide"&size=2 "title & <preview>")\n',
        )
        self._build()
        page = self._page()

        images = [
            attrs for tag, attrs in self._elements(page)
            if tag == "img"
        ]
        self.assertEqual(len(images), 1, page)
        attrs = images[0]
        self.assertEqual(attrs.get("alt"), 'badge" onerror="boom')
        self.assertEqual(
            attrs.get("src"), 'images/badge.svg?name="wide"&size=2'
        )
        self.assertEqual(attrs.get("title"), "title & <preview>")
        self.assertNotIn("onerror", attrs)
        self.assertIn('alt="badge&quot; onerror=&quot;boom"', page)
        self.assertIn(
            'src="images/badge.svg?name=&quot;wide&quot;&amp;size=2"', page
        )
        self.assertIn('title="title &amp; &lt;preview&gt;"', page)

    def test_regular_markdown_links_bold_and_inline_code_still_render(self):
        self._write_doc(
            "README.md",
            "[Project](https://example.com) **bold text** `x < y`\n",
        )
        self._build()
        page = self._page()

        hrefs = {
            attrs.get("href") for tag, attrs in self._elements(page)
            if tag == "a"
        }
        self.assertIn("https://example.com", hrefs)
        self.assertIn("<strong>bold text</strong>", page)
        self.assertIn("<code>x &lt; y</code>", page)

    def test_inline_code_keeps_markdown_and_html_looking_content_literal(self):
        self._write_doc(
            "README.md",
            "`![badge](images/badge.svg) <script> **bold** "
            "[guide](guide.md)`\n",
        )
        self._build()
        page = self._page()
        tags = {tag for tag, _ in self._elements(page)}

        self.assertIn(
            "![badge](images/badge.svg) &lt;script&gt; **bold** "
            "[guide](guide.md)",
            page,
        )
        self.assertNotIn("img", tags)
        self.assertNotIn("script", tags)
        self.assertNotIn("<strong>bold</strong>", page)

    def test_fenced_code_keeps_markdown_and_html_looking_content_literal(self):
        self._write_doc(
            "README.md",
            "```html\n"
            "![badge](images/badge.svg) <script> **bold** "
            "[guide](guide.md)\n"
            "```\n",
        )
        self._build()
        page = self._page()
        tags = {tag for tag, _ in self._elements(page)}

        self.assertIn(
            "![badge](images/badge.svg) &lt;script&gt; **bold** "
            "[guide](guide.md)",
            page,
        )
        self.assertNotIn("img", tags)
        self.assertNotIn("script", tags)
        self.assertNotIn("<strong>bold</strong>", page)

    def test_safe_authored_presentation_html_is_preserved(self):
        self._write_doc(
            "README.md",
            '<p align="center"><a href="https://example.com/project">'
            '<img src="images/badge.svg" alt="Build badge"></a></p>\n'
            "\n"
            "<details>\n"
            "<summary>More information</summary>\n"
            "<table>\n"
            "<tr><td>Migration guide</td><td>Placeholder</td></tr>\n"
            "</table>\n"
            "</details>\n",
        )
        self._build()
        elements = self._elements(self._page())
        tags = {tag for tag, _ in elements}

        for tag in ("p", "img", "a", "details", "summary", "table", "tr", "td"):
            self.assertIn(tag, tags)
        self.assertTrue(
            any(tag == "p" and attrs.get("align") == "center"
                for tag, attrs in elements)
        )
        self.assertTrue(
            any(tag == "img" and attrs.get("src") == "images/badge.svg"
                and attrs.get("alt") == "Build badge"
                for tag, attrs in elements)
        )
        self.assertTrue(
            any(tag == "a" and attrs.get("href") == "https://example.com/project"
                for tag, attrs in elements)
        )

    def test_script_and_unknown_authored_tags_are_not_activated(self):
        self._write_doc(
            "README.md",
            "<script>alert(1)</script>\n"
            '<iframe src="https://example.com">embedded</iframe>\n'
            '<custom-widget data-mode="demo">placeholder</custom-widget>\n',
        )
        self._build()
        tags = {tag for tag, _ in self._elements(self._page())}

        self.assertNotIn("script", tags)
        self.assertNotIn("iframe", tags)
        self.assertNotIn("custom-widget", tags)

    def test_unsafe_authored_attributes_and_protocols_are_not_activated(self):
        self._write_doc(
            "README.md",
            '<a href="javascript:alert(1)" onclick="alert(1)">unsafe</a>\n'
            '<img src="javascript:alert(1)" onerror="alert(1)" alt="unsafe">\n',
        )
        self._build()
        elements = self._elements(self._page())

        for tag, attrs in elements:
            self.assertFalse(
                any(name.lower().startswith("on") for name in attrs),
                f"event handler survived on <{tag}>: {attrs}",
            )
            for name in ("href", "src"):
                value = attrs.get(name, "").strip().lower()
                self.assertFalse(
                    value.startswith(("javascript:", "data:")),
                    f"unsafe {name} survived on <{tag}>: {value}",
                )

    def test_build_copies_nested_svg_and_png_image_assets(self):
        images_dir = self.src_dir / "images"
        nested_dir = images_dir / "nested"
        nested_dir.mkdir(parents=True)
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            "<text>public placeholder</text></svg>\n"
        )
        png = b"public PNG placeholder\x00\xff"
        (images_dir / "badge.svg").write_text(svg, encoding="utf-8")
        (nested_dir / "preview.png").write_bytes(png)
        self._write_doc(
            "README.md",
            "![Badge](images/badge.svg) ![Preview](images/nested/preview.png)\n",
        )
        self._build()

        copied_svg = self.out_dir / "images" / "badge.svg"
        copied_png = self.out_dir / "images" / "nested" / "preview.png"
        self.assertTrue(copied_svg.is_file(), "SVG image asset was not copied")
        self.assertTrue(copied_png.is_file(), "PNG image asset was not copied")
        self.assertEqual(copied_svg.read_text(encoding="utf-8"), svg)
        self.assertEqual(copied_png.read_bytes(), png)

    def test_only_allowlisted_rasters_are_copied_byte_for_byte_from_nested_paths(self):
        assets = {
            "images/pixel.png": b"\x89PNG\r\n\x1a\nsynthetic-png",
            "images/photo.jpg": b"\xff\xd8synthetic-jpeg\xff\xd9",
            "images/photo.jpeg": b"\xff\xd8synthetic-jpeg-long-extension\xff\xd9",
            "images/animated.gif": b"GIF89a\x01\x00\x01\x00synthetic-gif",
            "images/nested/deep/preview.webp": b"RIFFsynthetic-WEBPWEBP",
        }
        for relative_path, payload in assets.items():
            path = self.src_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self._write_doc("README.md", "# Synthetic raster assets\n")

        self._build()

        for relative_path, payload in assets.items():
            with self.subTest(asset=relative_path):
                self.assertEqual(
                    (self.out_dir / relative_path).read_bytes(), payload,
                )

    def test_static_nested_gradient_svg_and_internal_paint_filters_are_preserved(self):
        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 24" '
            'role="img" aria-labelledby="title description">\n'
            '  <title id="title">Synthetic gradient</title>\n'
            '  <desc id="description">Static geometry with local references</desc>\n'
            '  <defs>\n'
            '    <linearGradient id="paint" x1="0%" y1="0%" x2="100%" y2="0%">\n'
            '      <stop offset="0%" stop-color="#ffffff"/>\n'
            '      <stop offset="100%" stop-color="#204060"/>\n'
            '    </linearGradient>\n'
            '    <filter id="soft-shadow">\n'
            '      <feGaussianBlur in="SourceAlpha" stdDeviation="1"/>\n'
            '      <feDropShadow dx="0" dy="1" stdDeviation="1" flood-color="#000000"/>\n'
            '    </filter>\n'
            '  </defs>\n'
            '  <g fill="url(#paint)" filter="url(#soft-shadow)">\n'
            '    <rect x="1" y="1" width="46" height="22" rx="3"/>\n'
            '    <text x="24" y="15" text-anchor="middle" font-family="sans-serif" '
            'font-size="8" fill="#ffffff">safe</text>\n'
            '  </g>\n'
            '</svg>\n'
        ).encode("utf-8")
        asset = self.src_dir / "images" / "nested" / "gradient.svg"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(svg)
        self._write_doc("README.md", "![Synthetic gradient](images/nested/gradient.svg)\n")

        self._build()

        self.assertEqual(
            (self.out_dir / "images" / "nested" / "gradient.svg").read_bytes(),
            svg,
        )

    def test_existing_nine_documentation_svgs_are_preserved_exactly(self):
        names = (
            "architecture.svg",
            "badge-pbip.svg",
            "badge-license.svg",
            "badge-python.svg",
            "banner.svg",
            "pipeline.svg",
            "features.svg",
            "logo.svg",
            "conversions.svg",
        )
        source_assets = _REPO_ROOT / "docs" / "images"
        fixture_assets = self.src_dir / "images"
        fixture_assets.mkdir()
        expected = {}
        for name in names:
            payload = (source_assets / name).read_bytes()
            (fixture_assets / name).write_bytes(payload)
            expected[name] = payload
        self._write_doc("README.md", "# Existing SVG assets\n")

        self._build()

        for name, payload in expected.items():
            with self.subTest(asset=name):
                self.assertEqual(
                    (self.out_dir / "images" / name).read_bytes(), payload,
                )

    def test_arbitrary_html_asset_is_rejected_instead_of_published(self):
        self._assert_asset_rejected(
            "arbitrary-html",
            "payload.html",
            b"<script>document.body.innerHTML = 'owned'</script>",
        )

    def test_unknown_asset_extensions_are_rejected(self):
        for index, (name, payload) in enumerate((
            ("payload.js", b"alert('not an image')"),
            ("styles.css", b"body { background: url(https://example.invalid/x); }"),
            ("notes.txt", b"not an image"),
            ("vector.svgz", b"compressed SVG is not strict UTF-8 SVG"),
        )):
            with self.subTest(asset=name):
                self._assert_asset_rejected(
                    f"unsupported-{index}", name, payload,
                )

    def test_unsafe_or_malformed_svg_is_rejected_with_the_asset_path(self):
        svg_ns = "http://www.w3.org/2000/svg"

        def svg(body, attributes=""):
            return (
                f'<svg xmlns="{svg_ns}"{attributes}>{body}</svg>'
            ).encode("utf-8")

        unsafe = (
            ("script.svg", svg("<script>alert(1)</script>")),
            ("event-handler.svg", svg('<rect width="1" height="1" onload="alert(1)"/>')),
            ("foreign-object.svg", svg("<foreignObject><div>active</div></foreignObject>")),
            ("style-element.svg", svg("<style>rect { fill: red }</style>")),
            ("style-attribute.svg", svg('<rect width="1" height="1" style="fill:red"/>')),
            ("external-href.svg", svg('<a href="https://example.invalid/x"><text>x</text></a>')),
            ("internal-href.svg", svg(
                '<defs><linearGradient id="copy" href="#paint"/></defs>')),
            ("external-paint.svg", svg(
                '<rect width="1" height="1" fill="url(https://example.invalid/p.svg#paint)"/>')),
            ("animation.svg", svg('<animate attributeName="opacity" from="0" to="1"/>')),
            ("unknown-element.svg", svg("<metadata>not in the static profile</metadata>")),
            ("unknown-attribute.svg", svg(
                '<rect width="1" height="1" data-extra="not allowed"/>')),
            ("foreign-namespace.svg", (
                '<evil:svg xmlns:evil="urn:example:evil"><evil:rect/></evil:svg>'
            ).encode("utf-8")),
            ("doctype.svg", (
                f'<!DOCTYPE svg [<!ENTITY probe "expanded">]>'
                f'<svg xmlns="{svg_ns}"><text>&probe;</text></svg>'
            ).encode("utf-8")),
            ("xml-stylesheet.svg", (
                '<?xml-stylesheet type="text/css" href="external.css"?>'
                f'<svg xmlns="{svg_ns}"/>'
            ).encode("utf-8")),
            ("malformed.svg", f'<svg xmlns="{svg_ns}"><g></svg>'.encode("utf-8")),
            ("invalid-utf8.svg", f'<svg xmlns="{svg_ns}">\xff</svg>'.encode("latin-1")),
        )
        for index, (name, payload) in enumerate(unsafe):
            with self.subTest(asset=name):
                self._assert_asset_rejected(f"unsafe-svg-{index}", name, payload)

    def test_image_root_symlink_is_rejected(self):
        target = self.root / "outside-images"
        target.mkdir()
        (target / "payload.html").write_text("<script>unsafe</script>", encoding="utf-8")
        image_root = self.src_dir / "images"
        self._symlink_or_skip(image_root, target, is_directory=True)
        self._write_doc("README.md", "# Symlink root\n")

        with self.assertRaisesRegex(ValueError, "Unsafe documentation image asset:") as raised:
            self._build()
        self.assertIn(str(image_root), str(raised.exception))

    def test_nested_directory_symlink_is_rejected(self):
        images = self.src_dir / "images"
        images.mkdir()
        target = self.root / "outside-nested"
        target.mkdir()
        (target / "preview.png").write_bytes(b"outside target")
        link = images / "nested"
        self._symlink_or_skip(link, target, is_directory=True)
        self._write_doc("README.md", "# Nested symlink\n")

        with self.assertRaisesRegex(ValueError, "Unsafe documentation image asset:") as raised:
            self._build()
        self.assertIn(str(link), str(raised.exception))

    def test_file_and_dangling_symlinks_are_rejected(self):
        images = self.src_dir / "images"
        images.mkdir()
        target = self.root / "outside.png"
        target.write_bytes(b"outside target")
        file_link = images / "file.png"
        self._symlink_or_skip(file_link, target)
        self._write_doc("README.md", "# File symlink\n")
        with self.assertRaisesRegex(ValueError, "Unsafe documentation image asset:") as raised:
            self._build()
        self.assertIn(str(file_link), str(raised.exception))

        # Use a fresh builder tree so a prior link cannot satisfy the check.
        dangling_root = self.root / "dangling"
        source_dir = dangling_root / "docs"
        output_dir = dangling_root / "site"
        image_dir = source_dir / "images"
        image_dir.mkdir(parents=True)
        (source_dir / "README.md").write_text("# Dangling symlink\n", encoding="utf-8")
        dangling = image_dir / "missing.png"
        self._symlink_or_skip(dangling, dangling_root / "missing-target.png")
        with self.assertRaisesRegex(ValueError, "Unsafe documentation image asset:") as raised:
            build_docs.build(str(source_dir), str(output_dir))
        self.assertIn(str(dangling), str(raised.exception))

    def test_windows_junction_reparse_point_is_rejected(self):
        images = self.src_dir / "images"
        images.mkdir()
        (images / "safe.png").write_bytes(b"synthetic image")
        self._write_doc("README.md", "# Junction guard\n")
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        junction_stat = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_file_attributes=reparse_flag,
        )
        real_lstat = build_docs.os.lstat
        root_path = os.path.normcase(os.path.abspath(images))

        def lstat_with_junction(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(os.fspath(path))) == root_path:
                return junction_stat
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(build_docs.os, "lstat", side_effect=lstat_with_junction):
            with self.assertRaisesRegex(
                    ValueError, "Unsafe documentation image asset:") as raised:
                self._build()
        self.assertIn(str(images), str(raised.exception))

    def test_sibling_markdown_links_become_html_with_query_and_fragment(self):
        self._write_doc(
            "README.md",
            "[Guide](guide.md) "
            "[Setup](setup.md?tab=install#requirements) "
            "[Top](#overview)\n",
        )
        self._write_doc("guide.md", "# Guide\n")
        self._write_doc("setup.md", "# Setup\n")
        self._build()
        hrefs = {
            attrs.get("href") for tag, attrs in self._elements(self._page())
            if tag == "a"
        }

        self.assertIn("guide.html", hrefs)
        self.assertIn("setup.html?tab=install#requirements", hrefs)
        self.assertIn("#overview", hrefs)

    def test_absolute_and_mailto_links_remain_as_authored(self):
        self._write_doc(
            "README.md",
            "[Web](https://example.com/docs?a=1&b=2#faq) "
            "[Email](mailto:docs@example.com?subject=help) "
            "[Section](#overview)\n",
        )
        self._build()
        hrefs = {
            attrs.get("href") for tag, attrs in self._elements(self._page())
            if tag == "a"
        }

        self.assertIn("https://example.com/docs?a=1&b=2#faq", hrefs)
        self.assertIn("mailto:docs@example.com?subject=help", hrefs)
        self.assertIn("#overview", hrefs)

    def test_links_outside_docs_point_to_public_github_main_sources(self):
        self._write_doc(
            "README.md",
            "[Repository README](../README.md#overview) "
            "[Test README](../tests/README.md)\n",
        )
        self._build()
        hrefs = {
            attrs.get("href") for tag, attrs in self._elements(self._page())
            if tag == "a"
        }

        self.assertIn(
            "https://github.com/cyphou/TableauToPowerBI/blob/main/README.md"
            "#overview",
            hrefs,
        )
        self.assertIn(
            "https://github.com/cyphou/TableauToPowerBI/blob/main/tests/README.md",
            hrefs,
        )

    def test_generated_pages_include_responsive_image_sizing(self):
        self._write_doc("README.md", "# Responsive images\n")
        self._build()
        page = self._page()

        self.assertRegex(
            page,
            r"(?is)\bimg\s*\{"
            r"(?=[^}]*\bmax-width\s*:\s*100%)"
            r"(?=[^}]*\bheight\s*:\s*auto)"
            r"[^}]*\}",
        )

    def test_published_heading_ids_match_github_and_fragment_links(self):
        headings = (
            (
                "Local browser slice — bounded delivery",
                "local-browser-slice--bounded-delivery",
            ),
            (
                "Paired Tableau ↔ Power BI Desktop screenshots",
                "paired-tableau--power-bi-desktop-screenshots",
            ),
            (
                "Parser scope and workbook binding contract (reports #81–#83)",
                "parser-scope-and-workbook-binding-contract-reports-8183",
            ),
        )
        self._write_doc(
            "README.md",
            "\n\n".join(
                f"## {heading}\n\n[Section](#{slug})"
                for heading, slug in headings
            ) + "\n",
        )
        self._build()
        elements = self._elements(self._page())
        heading_ids = [
            attrs.get("id") for tag, attrs in elements if tag == "h2"
        ]
        fragment_links = {
            attrs["href"] for tag, attrs in elements
            if tag == "a" and attrs.get("href", "").startswith("#")
        }

        self.assertEqual(heading_ids, [slug for _, slug in headings])
        self.assertEqual(
            fragment_links, {f"#{heading_id}" for heading_id in heading_ids}
        )

    def test_repeated_headings_have_unique_github_ids(self):
        self._write_doc("README.md", "## Repeat\n\n## Repeat\n\n## Repeat\n")
        self._build()
        heading_ids = [
            attrs.get("id") for tag, attrs in self._elements(self._page())
            if tag == "h2"
        ]

        self.assertEqual(heading_ids, ["repeat", "repeat-1", "repeat-2"])

    def test_heading_ids_preserve_unicode_letters_and_underscores(self):
        self._write_doc(
            "README.md",
            "## Déploiement & vérification\n\n## `field_name`\n",
        )
        self._build()
        heading_ids = [
            attrs.get("id") for tag, attrs in self._elements(self._page())
            if tag == "h2"
        ]

        self.assertEqual(
            heading_ids, ["déploiement--vérification", "field_name"]
        )


if __name__ == "__main__":
    unittest.main()
