"""Build static HTML documentation from Markdown files.

Usage::

    python .github/scripts/build_docs.py docs/ _site/

Each ``.md`` file in the source directory is converted to a minimal
HTML page and written to the output directory.  A sidebar index is
generated automatically.  No external dependencies are required.
"""

import html
import os
import posixpath
import re
import shutil
import stat
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       max-width: 960px; margin: 0 auto; padding: 2rem 1rem; line-height: 1.6; color: #24292e; }
nav { position: fixed; left: 0; top: 0; width: 220px; height: 100vh; overflow-y: auto;
      background: #f6f8fa; border-right: 1px solid #e1e4e8; padding: 1rem; }
nav a { display: block; padding: 4px 0; text-decoration: none; color: #0366d6; font-size: 0.9rem; }
nav a:hover { text-decoration: underline; }
main { margin-left: 240px; min-width: 0; overflow-wrap: break-word; }
img { max-width: 100%; height: auto; vertical-align: middle; }
details { border: 1px solid #d1d9e0; border-radius: 8px; padding: 0.75rem; margin: 1rem 0; }
summary { cursor: pointer; }
code, pre { background: #f6f8fa; border-radius: 3px; font-size: 0.9rem; }
pre { padding: 1rem; overflow-x: auto; }
code { padding: 2px 6px; }
table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #e1e4e8; padding: 6px 13px; text-align: left; }
th { background: #f6f8fa; }
h1 { border-bottom: 1px solid #e1e4e8; padding-bottom: 0.3rem; }
h2 { border-bottom: 1px solid #eaecef; padding-bottom: 0.3rem; margin-top: 1.5rem; }
@media (max-width: 900px) {
    body { padding: 1rem; }
    nav { box-sizing: border-box; position: static; width: 100%; height: auto;
          border: 1px solid #e1e4e8; border-radius: 8px; margin-bottom: 1rem; }
    nav a { display: inline-block; margin-right: 0.75rem; }
    main { margin-left: 0; }
}
"""

_HTML_ATTRIBUTES = {
    'a': {'href', 'id', 'name', 'title'},
    'p': {'align'},
    'img': {'src', 'alt', 'title', 'width', 'height'},
    'details': {'open'},
    'summary': set(),
    'table': set(),
    'thead': set(),
    'tbody': set(),
    'tr': set(),
    'th': {'align', 'colspan', 'rowspan', 'width'},
    'td': {'align', 'colspan', 'rowspan', 'width'},
    'b': set(),
    'strong': set(),
    'em': set(),
    'br': set(),
    'kbd': set(),
    'sub': set(),
    'sup': set(),
}
_HTML_TAG = re.compile(r"""</?[A-Za-z](?:"[^"]*"|'[^']*'|[^'"<>])*>""")
_MARKDOWN_LINK = re.compile(r'(!?)\[([^\]\n]*)\]\((\S+?)(?:\s+"([^"]*)")?\)')
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
_SVG_ELEMENTS = {
    f'{{http://www.w3.org/2000/svg}}{name}'
    for name in {
        'svg', 'g', 'defs', 'title', 'desc', 'rect', 'circle', 'ellipse',
        'line', 'polyline', 'polygon', 'path', 'text', 'tspan',
        'linearGradient', 'radialGradient', 'stop', 'filter',
        'feGaussianBlur', 'feDropShadow',
    }
}
_SVG_ATTRIBUTES = {
    'aria-labelledby', 'cx', 'cy', 'd', 'dx', 'dy', 'fill', 'filter',
    'flood-color', 'flood-opacity', 'font-family', 'font-size', 'font-weight',
    'height', 'id', 'in', 'letter-spacing', 'offset', 'opacity', 'points',
    'r', 'role', 'rx', 'ry', 'stdDeviation', 'stop-color', 'stop-opacity',
    'stroke', 'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin',
    'stroke-width', 'text-anchor', 'transform', 'viewBox', 'width', 'x',
    'x1', 'x2', 'y', 'y1', 'y2',
}


def _is_asset_link(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or (
        bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _validate_svg_asset(path):
    error = f'Unsafe documentation image asset: {path}'
    try:
        with open(path, encoding='utf-8-sig') as stream:
            markup = stream.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f'{error} (SVG must use UTF-8)') from exc

    if '\x00' in markup or re.search(
        r'<!\s*(?:DOCTYPE|ENTITY)\b|<\?(?!xml\s)', markup, re.IGNORECASE,
    ):
        raise ValueError(f'{error} (SVG declarations or processing instructions)')
    declaration = re.match(r'\s*<\?xml\s+.*?\?>', markup, re.DOTALL)
    if declaration:
        encoding = re.search(r'\bencoding\s*=\s*(["\'])(.*?)\1', declaration[0])
        if encoding and encoding[2].lower() not in {'utf-8', 'utf8'}:
            raise ValueError(f'{error} (SVG must use UTF-8)')
    try:
        root = ET.fromstring(markup)
    except ET.ParseError as exc:
        raise ValueError(f'{error} (malformed SVG XML)') from exc
    if root.tag != '{http://www.w3.org/2000/svg}svg':
        raise ValueError(f'{error} (expected an SVG root element)')
    for element in root.iter():
        if element.tag not in _SVG_ELEMENTS:
            raise ValueError(f'{error} (unsupported SVG element: {element.tag})')
        for name, value in element.attrib.items():
            if name not in _SVG_ATTRIBUTES or '\\' in value or '/*' in value:
                raise ValueError(f'{error} (unsupported SVG attribute: {name})')
            if re.search(r'url\s*\(', value, re.IGNORECASE) and (
                name not in {'fill', 'stroke', 'filter'}
                or not re.fullmatch(r'url\(\s*#[A-Za-z_][\w.-]*\s*\)', value)
            ):
                raise ValueError(f'{error} (external or unsupported SVG reference)')


def _copy_image_assets(src_dir, out_dir):
    images_dir = os.path.join(src_dir, 'images')
    if not os.path.lexists(images_dir):
        return
    if _is_asset_link(images_dir) or not os.path.isdir(images_dir):
        raise ValueError(f'Unsafe documentation image asset: {images_dir} (expected a real directory)')
    pending = [images_dir]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if _is_asset_link(entry.path):
                    raise ValueError(f'Unsafe documentation image asset: {entry.path} (links are not allowed)')
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                extension = os.path.splitext(entry.name)[1].lower()
                if not entry.is_file(follow_symlinks=False) or extension not in _IMAGE_EXTENSIONS:
                    raise ValueError(f'Unsafe documentation image asset: {entry.path} (unsupported image format)')
                if extension == '.svg':
                    _validate_svg_asset(entry.path)
                destination = os.path.join(
                    out_dir, 'images', os.path.relpath(entry.path, images_dir),
                )
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copyfile(entry.path, destination)


def _safe_url(value, image=False):
    normalized = re.sub(r'[\x00-\x20]', '', html.unescape(value))
    allowed = {'', 'http', 'https'} if image else {'', 'http', 'https', 'mailto'}
    return urlsplit(normalized).scheme.lower() in allowed


class _PresentationTag(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = ''

    def handle_starttag(self, tag, attrs):
        allowed = _HTML_ATTRIBUTES.get(tag)
        if allowed is None or any(
            name not in allowed
            or (name in {'href', 'src'} and not _safe_url(value or '', name == 'src'))
            for name, value in attrs
        ):
            self.output = html.escape(self.get_starttag_text())
            return
        attributes = ''.join(
            f' {name}="{html.escape(value, quote=True)}"' if value is not None else f' {name}'
            for name, value in attrs
        )
        self.output = f'<{tag}{attributes}>'

    def handle_endtag(self, tag):
        self.output = f'</{tag}>' if tag in _HTML_ATTRIBUTES else html.escape(f'</{tag}>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _md_to_html(md_text):
    """Minimal Markdown to HTML converter (stdlib only, no dependencies).

    Handles headings, code, links/images, lists, tables and bounded presentation HTML.
    """
    lines = md_text.split('\n')
    out = []
    in_code = False
    in_table = False
    in_list = False
    list_tag = ''
    heading_ids = set()

    for line in lines:
        # Fenced code blocks
        if line.strip().startswith('```'):
            if in_code:
                out.append('</code></pre>')
                in_code = False
            else:
                lang = line.strip()[3:]
                out.append(f'<pre><code class="language-{html.escape(lang)}">')
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue

        # Tables
        if '|' in line and line.strip().startswith('|'):
            cells = [c.strip() for c in line.strip().split('|')[1:-1]]
            if all(re.match(r'^[-:]+$', c) for c in cells):
                continue  # separator row
            if not in_table:
                out.append('<table>')
                tag = 'th'
                in_table = True
            else:
                tag = 'td'
            out.append('<tr>' + ''.join(f'<{tag}>{_inline(c)}</{tag}>' for c in cells) + '</tr>')
            continue
        elif in_table:
            out.append('</table>')
            in_table = False

        stripped = line.strip()

        # Headings
        m = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if m:
            if in_list:
                out.append(f'</{list_tag}>')
                in_list = False
            level = len(m.group(1))
            text = _inline(m.group(2))
            heading = html.unescape(re.sub(r'<[^>]*>', '', m.group(2))).strip()
            base_slug = re.sub(r'[^\w -]', '', heading.lower()).replace(' ', '-')
            slug = base_slug
            duplicate = 0
            while slug in heading_ids:
                duplicate += 1
                slug = f'{base_slug}-{duplicate}'
            heading_ids.add(slug)
            out.append(f'<h{level} id="{slug}">{text}</h{level}>')
            continue

        # Unordered list
        m = re.match(r'^[-*+]\s+(.*)', stripped)
        if m:
            if not in_list or list_tag != 'ul':
                if in_list:
                    out.append(f'</{list_tag}>')
                out.append('<ul>')
                in_list = True
                list_tag = 'ul'
            out.append(f'<li>{_inline(m.group(1))}</li>')
            continue

        # Ordered list
        m = re.match(r'^\d+\.\s+(.*)', stripped)
        if m:
            if not in_list or list_tag != 'ol':
                if in_list:
                    out.append(f'</{list_tag}>')
                out.append('<ol>')
                in_list = True
                list_tag = 'ol'
            out.append(f'<li>{_inline(m.group(1))}</li>')
            continue

        # Close list if we hit a non-list line
        if in_list and stripped == '':
            out.append(f'</{list_tag}>')
            in_list = False

        # Blank line
        if stripped == '':
            continue

        if _HTML_TAG.match(stripped):
            out.append(_inline(stripped))
            continue

        # Paragraph
        out.append(f'<p>{_inline(stripped)}</p>')

    if in_code:
        out.append('</code></pre>')
    if in_table:
        out.append('</table>')
    if in_list:
        out.append(f'</{list_tag}>')

    return '\n'.join(out)


def _inline(text):
    """Convert inline Markdown to HTML."""
    tokens = []

    def protect(markup):
        token = f'\x00DOCS{len(tokens)}\x00'
        tokens.append(markup)
        return token

    text = re.sub(r'(`+)(.+?)\1', lambda m: protect(f'<code>{html.escape(m[2])}</code>'), text)

    def link(match):
        image, label, target, title = match.groups()
        if not _safe_url(target, image=bool(image)):
            return protect(html.escape(match[0]))
        attribute = f' title="{html.escape(title, quote=True)}"' if title is not None else ''
        target = html.escape(target, quote=True)
        if image:
            return protect(f'<img src="{target}" alt="{html.escape(label, quote=True)}"{attribute}>')
        return protect(f'<a href="{target}"{attribute}>{_inline(label)}</a>')

    text = _MARKDOWN_LINK.sub(link, text)

    def presentation(match):
        parser = _PresentationTag()
        parser.feed(match[0])
        parser.close()
        return protect(parser.output)

    text = _HTML_TAG.sub(presentation, text)
    text = html.escape(text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    for index in reversed(range(len(tokens))):
        text = text.replace(f'\x00DOCS{index}\x00', tokens[index])
    return text


def _rewrite_doc_links(body, md_files):
    def rewrite(match):
        value = html.unescape(match[1])
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return match[0]
        path = posixpath.normpath(unquote(parsed.path))
        if path in md_files:
            target = parsed._replace(path=path[:-3] + '.html')
        elif path.startswith('../'):
            source = posixpath.normpath(posixpath.join('docs', path))
            if source.startswith('../'):
                raise ValueError(f'Documentation link leaves the repository: {value}')
            kind = 'tree' if parsed.path.endswith('/') else 'blob'
            target = urlsplit(f'https://github.com/cyphou/TableauToPowerBI/{kind}/main/{quote(source)}')
            target = target._replace(query=parsed.query, fragment=parsed.fragment)
        else:
            return match[0]
        return f'href="{html.escape(urlunsplit(target), quote=True)}"'

    return re.sub(r'href="([^"]*)"', rewrite, body)


def build(src_dir, out_dir):
    """Build HTML docs from Markdown files."""
    os.makedirs(out_dir, exist_ok=True)

    md_files = sorted(f for f in os.listdir(src_dir) if f.endswith('.md'))
    if not md_files:
        print(f"No .md files found in {src_dir}")
        return

    # Build sidebar
    nav_links = []
    for f in md_files:
        name = f.replace('.md', '')
        title = name.replace('_', ' ').replace('-', ' ').title()
        html_name = name + '.html'
        nav_links.append(f'<a href="{html_name}">{title}</a>')
    nav_html = '<nav>\n<h3>Documentation</h3>\n' + '\n'.join(nav_links) + '\n</nav>'

    for f in md_files:
        name = f.replace('.md', '')
        title = name.replace('_', ' ').replace('-', ' ').title()
        with open(os.path.join(src_dir, f), 'r', encoding='utf-8') as fh:
            md = fh.read()
        body = _rewrite_doc_links(_md_to_html(md), md_files)
        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} – Tableau to Power BI</title>
<style>{_CSS}</style>
</head>
<body>
{nav_html}
<main>
{body}
</main>
</body>
</html>"""
        out_path = os.path.join(out_dir, name + '.html')
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write(page)
        print(f"  ✓ {f} → {name}.html")

    _copy_image_assets(src_dir, out_dir)

    # Create index.html redirect to README
    if 'README.md' in md_files:
        idx = '<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url=README.html"></head></html>'
        with open(os.path.join(out_dir, 'index.html'), 'w') as fh:
            fh.write(idx)
        print("  ✓ index.html → README.html")

    print(f"\nBuilt {len(md_files)} pages in {out_dir}/")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f"Usage: python {sys.argv[0]} <src_dir> <out_dir>")
        sys.exit(1)
    build(sys.argv[1], sys.argv[2])
