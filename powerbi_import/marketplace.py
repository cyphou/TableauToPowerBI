"""
Migration Marketplace — versioned pattern registry for community DAX recipes,
visual mappings, and M query templates.

Patterns are JSON files stored in a catalogue directory, each with metadata
(name, version, author, tags, category) and a payload (DAX, M, visual override).

Usage:
    registry = PatternRegistry("examples/marketplace")
    registry.load()
    patterns = registry.search(tags=["finance"], category="dax_recipe")
    recipe = registry.get("revenue_ytd", version="1.2.0")
    registry.apply_dax_recipes(measures, tags=["healthcare"])
"""

import json
import logging
import os
import re

logger = logging.getLogger('tableau_to_powerbi.marketplace')


class PatternMetadata:
    """Descriptor for a marketplace pattern."""

    __slots__ = ('name', 'version', 'author', 'description', 'tags',
                 'category', 'tableau_function', 'created', 'updated',
                 'dependencies')

    VALID_CATEGORIES = frozenset({
        'dax_recipe', 'visual_mapping', 'm_template',
        'naming_convention', 'model_template',
    })

    def __init__(self, data):
        self.name = data.get('name', '')
        self.version = data.get('version', '1.0.0')
        self.author = data.get('author', '')
        self.description = data.get('description', '')
        self.tags = list(data.get('tags', []))
        self.category = data.get('category', 'dax_recipe')
        self.tableau_function = data.get('tableau_function', '')
        self.created = data.get('created', '')
        self.updated = data.get('updated', '')
        # Marketplace v2: dependency specs like "base_pack>=1.0.0"
        self.dependencies = list(data.get('dependencies', []))

    def matches(self, tags=None, category=None, name_pattern=None):
        """Check whether this pattern matches the given filter criteria."""
        if category and self.category != category:
            return False
        if tags:
            tag_set = set(t.lower() for t in self.tags)
            if not any(t.lower() in tag_set for t in tags):
                return False
        if name_pattern and not re.search(name_pattern, self.name, re.IGNORECASE):
            return False
        return True

    def to_dict(self):
        return {
            'name': self.name,
            'version': self.version,
            'author': self.author,
            'description': self.description,
            'tags': self.tags,
            'category': self.category,
            'tableau_function': self.tableau_function,
            'created': self.created,
            'updated': self.updated,
            'dependencies': self.dependencies,
        }


class Pattern:
    """A single marketplace pattern with metadata and payload."""

    def __init__(self, metadata, payload, source_path=None):
        self.metadata = metadata
        self.payload = payload  # dict: depends on category
        self.source_path = source_path

    @property
    def name(self):
        return self.metadata.name

    @property
    def version(self):
        return self.metadata.version

    @property
    def category(self):
        return self.metadata.category

    def to_dict(self):
        return {
            'metadata': self.metadata.to_dict(),
            'payload': self.payload,
        }


def _parse_version(ver_str):
    """Parse a semver string into a comparable tuple."""
    parts = ver_str.split('.')
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


class PatternRegistry:
    """Catalogue of versioned migration patterns.

    Loads pattern JSON files from a directory, supports search/filter,
    and can apply DAX recipes or visual overrides to migration output.
    """

    def __init__(self, catalogue_dir=None):
        self._catalogue_dir = catalogue_dir
        self._patterns = {}  # name → {version → Pattern}
        self._loaded = False

    # ── Loading ──

    def load(self, catalogue_dir=None):
        """Load all pattern files from the catalogue directory."""
        cat_dir = catalogue_dir or self._catalogue_dir
        if not cat_dir:
            logger.warning("No catalogue directory specified")
            return 0
        if not os.path.isdir(cat_dir):
            logger.warning(f"Catalogue directory not found: {cat_dir}")
            return 0

        count = 0
        for fname in sorted(os.listdir(cat_dir)):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(cat_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                pattern = self._parse_pattern(data, fpath)
                if pattern:
                    self._register(pattern)
                    count += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load pattern {fname}: {e}")
        self._loaded = True
        logger.info(f"Loaded {count} patterns from {cat_dir}")
        return count

    def _parse_pattern(self, data, source_path):
        """Parse a pattern dict into a Pattern object."""
        meta_data = data.get('metadata', data)
        payload = data.get('payload', {})
        try:
            metadata = PatternMetadata(meta_data)
        except Exception as e:
            logger.error(f"Invalid pattern metadata in {source_path}: {e}")
            return None
        if not metadata.name:
            logger.warning(f"Pattern in {source_path} has no name — skipped")
            return None
        return Pattern(metadata, payload, source_path)

    def _register(self, pattern):
        """Add a pattern to the internal index."""
        name = pattern.name
        if name not in self._patterns:
            self._patterns[name] = {}
        self._patterns[name][pattern.version] = pattern

    # ── Registration ──

    def register(self, pattern_dict):
        """Register a pattern from a dict (programmatic API).

        Args:
            pattern_dict: Dict with 'metadata' and 'payload' keys.

        Returns:
            Pattern or None
        """
        pattern = self._parse_pattern(pattern_dict, '<programmatic>')
        if pattern:
            self._register(pattern)
        return pattern

    # ── Querying ──

    def get(self, name, version=None):
        """Get a specific pattern by name, optionally pinned to a version.

        Without *version*, returns the latest version.
        """
        versions = self._patterns.get(name)
        if not versions:
            return None
        if version:
            return versions.get(version)
        # Return latest version
        latest_ver = max(versions.keys(), key=_parse_version)
        return versions[latest_ver]

    def search(self, tags=None, category=None, name_pattern=None):
        """Search patterns matching the given criteria.

        Returns a list of Pattern objects (latest version per name).
        """
        results = []
        for name, versions in self._patterns.items():
            latest_ver = max(versions.keys(), key=_parse_version)
            pattern = versions[latest_ver]
            if pattern.metadata.matches(tags=tags, category=category,
                                        name_pattern=name_pattern):
                results.append(pattern)
        return results

    def list_all(self):
        """Return all patterns (latest version per name)."""
        return self.search()

    @property
    def count(self):
        return len(self._patterns)

    # ── Application ──

    def apply_dax_recipes(self, measures, tags=None, category='dax_recipe'):
        """Apply matching DAX recipes to a dict of measures.

        Each recipe payload should have:
            - ``match``: regex to match measure DAX formulas
            - ``replacement``: DAX replacement string
          OR:
            - ``inject``: new measure name + DAX to add alongside existing measures

        Args:
            measures: dict {measure_name: dax_formula}
            tags: optional tag filter
            category: pattern category (default 'dax_recipe')

        Returns:
            dict of changes: {measure_name: {'action': 'replaced'|'injected', ...}}
        """
        recipes = self.search(tags=tags, category=category)
        changes = {}
        injected = {}
        for recipe in recipes:
            payload = recipe.payload
            match_re = payload.get('match')
            replacement = payload.get('replacement')
            inject = payload.get('inject')

            if match_re and replacement:
                for mname, formula in list(measures.items()):
                    if re.search(match_re, formula, re.IGNORECASE):
                        new_formula = re.sub(match_re, replacement, formula,
                                             flags=re.IGNORECASE)
                        measures[mname] = new_formula
                        changes[mname] = {
                            'action': 'replaced',
                            'recipe': recipe.name,
                            'version': recipe.version,
                        }

            if inject:
                inject_name = inject.get('name', '')
                inject_dax = inject.get('dax', '')
                if inject_name and inject_dax and inject_name not in measures:
                    injected[inject_name] = inject_dax
                    changes[inject_name] = {
                        'action': 'injected',
                        'recipe': recipe.name,
                        'version': recipe.version,
                    }

        measures.update(injected)
        return changes

    def apply_visual_overrides(self, visual_type_map):
        """Apply visual mapping overrides from marketplace patterns.

        Each pattern payload should have:
            - ``overrides``: dict {tableau_mark: pbi_visual_type}

        Args:
            visual_type_map: dict to update in-place

        Returns:
            int: number of overrides applied
        """
        patterns = self.search(category='visual_mapping')
        count = 0
        for pattern in patterns:
            overrides = pattern.payload.get('overrides', {})
            for mark, pbi_type in overrides.items():
                visual_type_map[mark.lower()] = pbi_type
                count += 1
        return count

    def export(self, output_path):
        """Export all patterns to a single JSON catalogue file."""
        catalogue = []
        for name in sorted(self._patterns.keys()):
            for ver in sorted(self._patterns[name].keys(), key=_parse_version):
                catalogue.append(self._patterns[name][ver].to_dict())
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(catalogue, f, indent=2, ensure_ascii=False)
        return len(catalogue)

    def to_dict(self):
        """Summary dict for reporting."""
        return {
            'pattern_count': self.count,
            'categories': list(set(
                p.category for versions in self._patterns.values()
                for p in versions.values()
            )),
            'patterns': [
                {'name': n, 'versions': list(v.keys())}
                for n, v in self._patterns.items()
            ],
        }

    # ── Marketplace v2: dependency tracking ──

    def get_dependencies(self, name, version=None):
        """Return the declared dependency specs for a pattern."""
        pattern = self.get(name, version)
        if not pattern:
            return []
        return list(pattern.metadata.dependencies)

    def resolve_dependencies(self, name, version=None, _seen=None):
        """Resolve the full transitive dependency closure for a pattern.

        Returns:
            dict with keys:
                ``order``: list of pattern names in install order (deps first),
                ``missing``: list of unmet dependency specs,
                ``conflicts``: list of version-conflict messages.
        """
        order = []
        missing = []
        conflicts = []
        seen = _seen if _seen is not None else set()

        def _visit(pname, pver):
            key = (pname, pver)
            if key in seen:
                return
            seen.add(key)
            pat = self.get(pname, pver)
            if not pat:
                missing.append(f"{pname}{('==' + pver) if pver else ''}")
                return
            for dep in pat.metadata.dependencies:
                dname, op, dver = _split_dep_spec(dep)
                target = self.get(dname)
                if not target:
                    missing.append(dep)
                    continue
                if dver and not _version_satisfies(
                        _parse_version(target.version), op, _parse_version(dver)):
                    conflicts.append(
                        f"{pname} requires {dname}{op}{dver}, "
                        f"have {target.version}")
                _visit(dname, None)
            if pname not in order:
                order.append(pname)

        _visit(name, version)
        return {'order': order, 'missing': missing, 'conflicts': conflicts}

    # ── Marketplace v2: remote catalogue sync ──

    def sync_remote(self, catalogue_url, dest_dir=None, timeout=30, opener=None):
        """Fetch a remote pattern catalogue and load it into the registry.

        The remote endpoint must return a JSON array of pattern objects (same
        shape as local pattern files: ``{metadata, payload}`` or a flat dict).
        Designed for GitHub-release asset URLs but works with any HTTP(S) JSON.

        Args:
            catalogue_url: HTTP(S) URL returning a JSON array of patterns.
            dest_dir: optional directory to cache fetched patterns as files.
            timeout: socket timeout in seconds.
            opener: optional callable(url, timeout) -> bytes (for testing /
                offline injection). Defaults to urllib.

        Returns:
            int: number of patterns fetched and registered.
        """
        if opener is None:
            opener = _default_url_opener
        if not re.match(r'^https?://', catalogue_url):
            raise ValueError(f"Unsupported catalogue URL scheme: {catalogue_url}")

        raw = opener(catalogue_url, timeout)
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get('patterns', [data])
        if not isinstance(data, list):
            raise ValueError("Remote catalogue must be a JSON array of patterns")

        count = 0
        for entry in data:
            pattern = self._parse_pattern(entry, catalogue_url)
            if pattern:
                self._register(pattern)
                count += 1
                if dest_dir:
                    self._cache_pattern(pattern, dest_dir)
        self._loaded = True
        logger.info("Synced %d patterns from %s", count, catalogue_url)
        return count

    def _cache_pattern(self, pattern, dest_dir):
        """Write a fetched pattern to the local cache directory."""
        os.makedirs(dest_dir, exist_ok=True)
        safe = re.sub(r'[^\w.\-]+', '_', f"{pattern.name}-{pattern.version}")
        fpath = os.path.join(dest_dir, f"{safe}.json")
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(pattern.to_dict(), f, indent=2, ensure_ascii=False)
        return fpath


# ── Marketplace v2 helpers ──

def _split_dep_spec(spec):
    """Split 'name>=1.2.0' → ('name', '>=', '1.2.0'). op/ver may be None."""
    m = re.match(r'^([\w.\-]+)\s*([<>=!]=?)?\s*([\d.]+)?$', str(spec).strip())
    if not m:
        return str(spec).strip(), None, None
    return m.group(1), m.group(2), m.group(3)


def _version_satisfies(have, op, want):
    """Compare two version tuples under a comparison operator."""
    if op in (None, '=='):
        return have == want
    if op == '>=':
        return have >= want
    if op == '<=':
        return have <= want
    if op == '>':
        return have > want
    if op == '<':
        return have < want
    if op == '!=':
        return have != want
    return True


def _default_url_opener(url, timeout):
    """Fetch *url* with urllib (stdlib). Returns response bytes."""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'tableau-to-powerbi-marketplace'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()
