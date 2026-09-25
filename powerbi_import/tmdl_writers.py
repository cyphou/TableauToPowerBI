"""TMDL serialization.

Every function that turns the in-memory semantic model into TMDL text: the
file writers, the TMDL syntax helpers (name quoting, data type and summarize
mapping, safe filenames) and the description generators that feed Copilot/Q&A
annotations. Extracted from tmdl_generator so the serialization surface has a
single owner (@semantic); tmdl_generator re-exports these names for backward
compatibility.
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid

from powerbi_import.tmdl_m_conversion import (
    _fix_m_if_else_balance,
    _strip_m_inline_comments,
)

logger = logging.getLogger(__name__)


def _quote_name(name):
    """Quote a TMDL name if needed (spaces, special characters).
    Internal apostrophes are escaped by doubling them ('')."""
    # Trailing/leading spaces are part of the identity: Tableau allows
    # 'Montant Articles ' and 'Montant Articles' to coexist, and trimming
    # merged them into one name, which made calculated columns reference
    # themselves. Only characters TMDL cannot carry are normalised.
    name = (name or '').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    # Keep bare identifiers only when they follow standard identifier rules.
    # This avoids emitting invalid names like numeric-only identifiers (e.g. 0).
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return name
    escaped = name.replace("'", "''")
    return f"'{escaped}'"


def _tmdl_datatype(bim_type):
    """Convert a type to TMDL type."""
    mapping = {
        'int64': 'int64', 'string': 'string', 'double': 'double',
        'decimal': 'decimal', 'boolean': 'boolean', 'datetime': 'dateTime',
        'binary': 'binary',
    }
    return mapping.get(bim_type.lower() if bim_type else '', 'string')


def _tmdl_summarize(summarize_by):
    """Convert summarizeBy to TMDL."""
    mapping = {
        'sum': 'sum',
        'none': 'none',
        'count': 'count',
        'average': 'average',
        'min': 'min',
        'max': 'max',
    }
    return mapping.get(str(summarize_by).lower(), 'none')


# Power BI shreds a .pbip through PBIProjectUtils.EnsureNotLong and refuses to
# open it when a path reaches these limits, whatever the OS supports.
PBI_MAX_PATH = 260


def _safe_filename(name, base_dir=None, suffix=''):
    """Create a safe filename stem for a table.

    With *base_dir*, the stem is shortened until the full path fits Power BI's
    limit. A Tableau table name carries its whole source description, so under
    a deep output folder the path alone can exceed 260 characters and Desktop
    reports "The specified path, file name, or both are too long". The table's
    real name is declared inside the file, so shortening the stem costs
    nothing; a hash keeps distinct tables distinct.
    """
    safe = re.sub(r'[<>:"/\\|?*]', '_', name)
    if base_dir is None:
        return safe
    budget = PBI_MAX_PATH - len(os.path.join(os.path.abspath(base_dir), '')) \
        - len(suffix) - 1
    if len(safe) <= budget:
        return safe
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]
    keep = budget - len(digest) - 1
    if keep < 1:
        # Nothing of the name survives; the hash alone still identifies it.
        return digest
    return safe[:keep].rstrip() + '_' + digest


def _write_tmdl_files(model_data, output_dir):
    """
    Write the complete TMDL file structure from a semantic model.

    Args:
        model_data: dict -- the full model (with 'model' key)
        output_dir: str -- path to the SemanticModel folder

    Returns:
        str -- path to the created definition/ folder
    """
    model = model_data.get('model', model_data)

    def_dir = os.path.join(output_dir, 'definition')
    os.makedirs(def_dir, exist_ok=True)

    tables = model.get('tables', [])
    relationships = model.get('relationships', [])
    roles = model.get('roles', [])
    culture = model.get('culture', 'en-US')

    # Pre-assign stable UUIDs to relationships for consistency between
    # model.tmdl (ref relationship <id>) and relationships.tmdl (relationship <id>)
    for rel in relationships:
        rel_name = rel.get('name', '')
        try:
            uuid.UUID(rel_name)
        except (ValueError, AttributeError):
            rel['name'] = str(uuid.uuid4())

    # 1. database.tmdl
    _write_database_tmdl(def_dir, model)

    # 2. model.tmdl
    _write_model_tmdl(def_dir, model, tables, roles, relationships)

    # 3. relationships.tmdl
    _write_relationships_tmdl(def_dir, relationships)

    # 4. expressions.tmdl (with datasource parameters)
    if model_data.get('_model_mode', '').lower() == 'directlake':
        _write_direct_lake_expression(
            def_dir,
            model_data.get('_direct_lake', {}),
        )
    else:
        _write_expressions_tmdl(def_dir, tables, datasources=model_data.get('_datasources'),
                                incremental_params=model_data.get('_incremental_params'))

    # 5. roles.tmdl
    if roles:
        _write_roles_tmdl(def_dir, roles)

    # 6. tables/*.tmdl
    tables_dir = os.path.join(def_dir, 'tables')
    os.makedirs(tables_dir, exist_ok=True)

    # Clean stale table files from previous runs
    expected_files = set()
    for table in tables:
        tname = table.get('name', 'Table')
        expected_files.add(_safe_filename(tname, tables_dir, '.tmdl') + '.tmdl')
    for existing in os.listdir(tables_dir):
        if existing.endswith('.tmdl') and existing not in expected_files:
            stale_path = os.path.join(tables_dir, existing)
            for _attempt in range(3):
                try:
                    os.remove(stale_path)
                    break
                except PermissionError:
                    time.sleep(0.3 * (2 ** _attempt))
                    logger.debug("Retry removing stale TMDL: %s", stale_path)
                except OSError as exc:
                    logger.debug("Cannot remove stale TMDL %s: %s", stale_path, exc)
                    break
            else:
                logger.warning("Cannot remove stale TMDL %s after retries (locked)", stale_path)

    failed_table_names = []
    for table in tables:
        try:
            _write_table_tmdl(tables_dir, table)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            table_name = table.get('name', 'Table')
            failed_table_names.append(table_name)
            logger.exception("TMDL table write failed for %s; continuing", table_name)

    if failed_table_names:
        failed = set(failed_table_names)
        tables = [table for table in tables if table.get('name', 'Table') not in failed]
        relationships = [rel for rel in relationships
                         if rel.get('fromTable') not in failed
                         and rel.get('toTable') not in failed]
        model['tables'] = tables
        model['relationships'] = relationships
        # Rewrite aggregate files so they do not reference omitted tables.
        _write_model_tmdl(def_dir, model, tables, roles, relationships)
        _write_relationships_tmdl(def_dir, relationships)
        logger.warning("Omitted %d failed TMDL table(s): %s",
                       len(failed_table_names), ', '.join(failed_table_names))

    # 7. diagramLayout.json (empty — Power BI Desktop fills it on first open)
    diagram_path = os.path.join(def_dir, 'diagramLayout.json')
    with open(diagram_path, 'w', encoding='utf-8') as f:
        json.dump({}, f)

    # 8. perspectives.tmdl (auto-generated from table groupings)
    perspectives = model.get('perspectives', [])
    if not perspectives and len(tables) > 2:
        # Auto-generate a "Full Model" perspective referencing all tables
        perspectives = [{
            "name": "Full Model",
            "tables": [t.get('name', '') for t in tables]
        }]
    if perspectives:
        _write_perspectives_tmdl(def_dir, perspectives)

    # 9. cultures/*.tmdl (model culture)
    linguistic_synonyms = model.get('_linguistic_synonyms', {})
    if culture and culture != 'en-US':
        cultures_dir = os.path.join(def_dir, 'cultures')
        os.makedirs(cultures_dir, exist_ok=True)
        _write_culture_tmdl(cultures_dir, culture, tables, linguistic_synonyms=linguistic_synonyms)
    elif linguistic_synonyms:
        # Even for en-US, write culture with synonyms for Q&A
        cultures_dir = os.path.join(def_dir, 'cultures')
        os.makedirs(cultures_dir, exist_ok=True)
        _write_culture_tmdl(cultures_dir, 'en-US', tables, linguistic_synonyms=linguistic_synonyms)

    # 9b. Additional language cultures (--languages flag)
    extra_languages = model.get('_languages', '')
    if extra_languages:
        _write_multi_language_cultures(def_dir, extra_languages, tables)

    # Release heavy table data after all writing steps (culture/perspectives)
    # are done. Post-write callers only need names and counts.
    for t in tables:
        t['_n_columns'] = len(t.get('columns', []))
        t['_n_measures'] = len(t.get('measures', []))
        t.pop('columns', None)
        t.pop('measures', None)
        t.pop('partitions', None)

    return def_dir


def _write_perspectives_tmdl(def_dir, perspectives):
    """
    Write perspectives.tmdl for multi-audience model views.

    Each perspective lists the tables visible from that viewpoint,
    allowing different user groups to see relevant subsets.

    Args:
        def_dir: Path to the definition/ folder
        perspectives: List of dicts with 'name' and 'tables' keys
    """
    lines = []
    for persp in perspectives:
        p_name = persp.get('name', 'Default')
        lines.append(f"perspective {_quote_name(p_name)}")
        for table_ref in persp.get('tables', []):
            tbl_name = table_ref if isinstance(table_ref, str) else table_ref.get('name', '')
            if tbl_name:
                lines.append(f"\tperspectiveTable {_quote_name(tbl_name)}")
        lines.append("")

    filepath = os.path.join(def_dir, 'perspectives.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _write_culture_tmdl(cultures_dir, culture_name, tables, linguistic_synonyms=None):
    """
    Write a culture TMDL file with linguistic metadata and translations.

    Generates translation entries for all table and column names
    in the model for the specified culture/locale.  When the culture
    differs from en-US, also writes ``translatedDisplayFolders`` and
    ``translatedDescriptions`` for measures and columns.

    Args:
        cultures_dir: Path to the cultures/ folder
        culture_name: Locale string (e.g. 'fr-FR')
        tables: List of table definitions (for generating metadata entries)
        linguistic_synonyms: Optional dict of field_name -> list of synonyms
            from Tableau captions/aliases for Q&A support
    """
    lines = [f"culture {_quote_name(culture_name)}"]

    # Linguistic metadata with synonyms
    lines.append("\tlinguisticMetadata =")
    lines.append('\t\t```')
    metadata = {
        "Version": "1.0.0",
        "Language": culture_name,
        "DynamicImprovement": "HighConfidence"
    }
    # Inject synonyms from Tableau field captions
    if linguistic_synonyms:
        entities = {}
        for field_name, syns in linguistic_synonyms.items():
            if syns:
                entities[field_name] = {
                    "State": "Generated",
                    "Terms": [{
                        "Value": s,
                        "State": "Suggested",
                        "Weight": 0.9
                    } for s in syns[:5]]  # Limit to 5 synonyms per field
                }
        if entities:
            metadata["Entities"] = entities
    lines.append(f'\t\t\t{json.dumps(metadata, ensure_ascii=False)}')
    lines.append('\t\t\t```')
    lines.append("")

    # Translation section — translatedDisplayFolders + translatedDescriptions
    folder_translations = _get_display_folder_translations(culture_name)
    if folder_translations and tables:
        for table in tables:
            tbl_name = table.get('name', '')
            if not tbl_name:
                continue
            # Translate display folders for measures
            for measure in table.get('measures', []):
                orig = measure.get('displayFolder', '')
                if not orig:
                    for ann in measure.get('annotations', []):
                        if ann.get('name') == 'displayFolder':
                            orig = ann.get('value', '')
                            break
                translated = folder_translations.get(orig, '')
                if translated and translated != orig:
                    lines.append(
                        f"\ttranslatedDisplayFolder {_quote_name(tbl_name)}"
                        f".{_quote_name(measure.get('name', ''))}"
                        f" = {_quote_name(translated)}"
                    )
            # Translate display folders for columns
            for col in table.get('columns', []):
                orig = col.get('displayFolder', '')
                if not orig:
                    for ann in col.get('annotations', []):
                        if ann.get('name') == 'displayFolder':
                            orig = ann.get('value', '')
                            break
                translated = folder_translations.get(orig, '')
                if translated and translated != orig:
                    lines.append(
                        f"\ttranslatedDisplayFolder {_quote_name(tbl_name)}"
                        f".{_quote_name(col.get('name', ''))}"
                        f" = {_quote_name(translated)}"
                    )
        lines.append("")

    filepath = os.path.join(cultures_dir, f'{culture_name}.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _write_multi_language_cultures(def_dir, languages, tables):
    """Write culture TMDL files for multiple languages.

    Args:
        def_dir: Path to the definition/ folder
        languages: Comma-separated locale string (e.g. 'fr-FR,de-DE,es-ES')
        tables: List of table definitions
    """
    if not languages:
        return

    locales = [loc.strip() for loc in languages.split(',') if loc.strip()]
    if not locales:
        return

    cultures_dir = os.path.join(def_dir, 'cultures')
    os.makedirs(cultures_dir, exist_ok=True)

    for locale in locales:
        if locale == 'en-US':
            continue  # Default culture, no need for translation file
        _write_culture_tmdl(cultures_dir, locale, tables)


_DISPLAY_FOLDER_TRANSLATIONS = {
    'fr-FR': {
        'Dimensions': 'Dimensions',
        'Measures': 'Mesures',
        'Time Intelligence': 'Intelligence Temporelle',
        'Flags': 'Indicateurs',
        'Calculations': 'Calculs',
        'Groups': 'Groupes',
        'Sets': 'Ensembles',
        'Bins': 'Intervalles',
        'Parameters': 'Paramètres',
        'Field Parameters': 'Paramètres de Champ',
        'Calculation Groups': 'Groupes de Calcul',
    },
    'de-DE': {
        'Dimensions': 'Dimensionen',
        'Measures': 'Kennzahlen',
        'Time Intelligence': 'Zeitintelligenz',
        'Flags': 'Kennzeichen',
        'Calculations': 'Berechnungen',
        'Groups': 'Gruppen',
        'Sets': 'Mengen',
        'Bins': 'Intervalle',
        'Parameters': 'Parameter',
        'Field Parameters': 'Feldparameter',
        'Calculation Groups': 'Berechnungsgruppen',
    },
    'es-ES': {
        'Dimensions': 'Dimensiones',
        'Measures': 'Medidas',
        'Time Intelligence': 'Inteligencia Temporal',
        'Flags': 'Indicadores',
        'Calculations': 'Cálculos',
        'Groups': 'Grupos',
        'Sets': 'Conjuntos',
        'Bins': 'Intervalos',
        'Parameters': 'Parámetros',
        'Field Parameters': 'Parámetros de Campo',
        'Calculation Groups': 'Grupos de Cálculo',
    },
    'pt-BR': {
        'Dimensions': 'Dimensões',
        'Measures': 'Medidas',
        'Time Intelligence': 'Inteligência Temporal',
        'Flags': 'Indicadores',
        'Calculations': 'Cálculos',
        'Groups': 'Grupos',
        'Sets': 'Conjuntos',
        'Bins': 'Intervalos',
        'Parameters': 'Parâmetros',
        'Field Parameters': 'Parâmetros de Campo',
        'Calculation Groups': 'Grupos de Cálculo',
    },
    'ja-JP': {
        'Dimensions': 'ディメンション',
        'Measures': 'メジャー',
        'Time Intelligence': 'タイムインテリジェンス',
        'Flags': 'フラグ',
        'Calculations': '計算',
        'Groups': 'グループ',
        'Sets': 'セット',
        'Bins': 'ビン',
        'Parameters': 'パラメーター',
        'Field Parameters': 'フィールドパラメーター',
        'Calculation Groups': '計算グループ',
    },
    'zh-CN': {
        'Dimensions': '维度',
        'Measures': '度量',
        'Time Intelligence': '时间智能',
        'Flags': '标志',
        'Calculations': '计算',
        'Groups': '组',
        'Sets': '集',
        'Bins': '区间',
        'Parameters': '参数',
        'Field Parameters': '字段参数',
        'Calculation Groups': '计算组',
    },
    'ko-KR': {
        'Dimensions': '차원',
        'Measures': '측정값',
        'Time Intelligence': '시간 인텔리전스',
        'Flags': '플래그',
        'Calculations': '계산',
        'Groups': '그룹',
        'Sets': '집합',
        'Bins': '구간',
        'Parameters': '매개변수',
        'Field Parameters': '필드 매개변수',
        'Calculation Groups': '계산 그룹',
    },
    'it-IT': {
        'Dimensions': 'Dimensioni',
        'Measures': 'Misure',
        'Time Intelligence': 'Time Intelligence',
        'Flags': 'Indicatori',
        'Calculations': 'Calcoli',
        'Groups': 'Gruppi',
        'Sets': 'Insiemi',
        'Bins': 'Intervalli',
        'Parameters': 'Parametri',
        'Field Parameters': 'Parametri di Campo',
        'Calculation Groups': 'Gruppi di Calcolo',
    },
    'nl-NL': {
        'Dimensions': 'Dimensies',
        'Measures': 'Metingen',
        'Time Intelligence': 'Tijdintelligentie',
        'Flags': 'Vlaggen',
        'Calculations': 'Berekeningen',
        'Groups': 'Groepen',
        'Sets': 'Sets',
        'Bins': 'Intervallen',
        'Parameters': 'Parameters',
        'Field Parameters': 'Veldparameters',
        'Calculation Groups': 'Berekeningsgroepen',
    },
    # ── v28: Additional culture translations ──
    'sv-SE': {
        'Dimensions': 'Dimensioner',
        'Measures': 'Mått',
        'Time Intelligence': 'Tidsintelligens',
        'Flags': 'Flaggor',
        'Calculations': 'Beräkningar',
        'Groups': 'Grupper',
        'Sets': 'Uppsättningar',
        'Bins': 'Intervall',
        'Parameters': 'Parametrar',
        'Field Parameters': 'Fältparametrar',
        'Calculation Groups': 'Beräkningsgrupper',
    },
    'da-DK': {
        'Dimensions': 'Dimensioner',
        'Measures': 'Målinger',
        'Time Intelligence': 'Tidsintelligens',
        'Flags': 'Flag',
        'Calculations': 'Beregninger',
        'Groups': 'Grupper',
        'Sets': 'Sæt',
        'Bins': 'Intervaller',
        'Parameters': 'Parametre',
        'Field Parameters': 'Feltparametre',
        'Calculation Groups': 'Beregningsgrupper',
    },
    'nb-NO': {
        'Dimensions': 'Dimensjoner',
        'Measures': 'Målinger',
        'Time Intelligence': 'Tidsintelligens',
        'Flags': 'Flagg',
        'Calculations': 'Beregninger',
        'Groups': 'Grupper',
        'Sets': 'Sett',
        'Bins': 'Intervaller',
        'Parameters': 'Parametere',
        'Field Parameters': 'Feltparametere',
        'Calculation Groups': 'Beregningsgrupper',
    },
    'fi-FI': {
        'Dimensions': 'Dimensiot',
        'Measures': 'Mittarit',
        'Time Intelligence': 'Aikaäly',
        'Flags': 'Liput',
        'Calculations': 'Laskelmat',
        'Groups': 'Ryhmät',
        'Sets': 'Joukot',
        'Bins': 'Intervallit',
        'Parameters': 'Parametrit',
        'Field Parameters': 'Kenttäparametrit',
        'Calculation Groups': 'Laskelmaryhmät',
    },
    'pl-PL': {
        'Dimensions': 'Wymiary',
        'Measures': 'Miary',
        'Time Intelligence': 'Analiza Czasowa',
        'Flags': 'Flagi',
        'Calculations': 'Obliczenia',
        'Groups': 'Grupy',
        'Sets': 'Zbiory',
        'Bins': 'Przedziały',
        'Parameters': 'Parametry',
        'Field Parameters': 'Parametry Pola',
        'Calculation Groups': 'Grupy Obliczeń',
    },
    'tr-TR': {
        'Dimensions': 'Boyutlar',
        'Measures': 'Ölçüler',
        'Time Intelligence': 'Zaman Zekası',
        'Flags': 'Bayraklar',
        'Calculations': 'Hesaplamalar',
        'Groups': 'Gruplar',
        'Sets': 'Kümeler',
        'Bins': 'Aralıklar',
        'Parameters': 'Parametreler',
        'Field Parameters': 'Alan Parametreleri',
        'Calculation Groups': 'Hesaplama Grupları',
    },
    'ru-RU': {
        'Dimensions': 'Измерения',
        'Measures': 'Метрики',
        'Time Intelligence': 'Временной анализ',
        'Flags': 'Флаги',
        'Calculations': 'Вычисления',
        'Groups': 'Группы',
        'Sets': 'Наборы',
        'Bins': 'Интервалы',
        'Parameters': 'Параметры',
        'Field Parameters': 'Параметры полей',
        'Calculation Groups': 'Группы вычислений',
    },
    'ar-SA': {
        'Dimensions': 'الأبعاد',
        'Measures': 'المقاييس',
        'Time Intelligence': 'ذكاء الوقت',
        'Flags': 'الأعلام',
        'Calculations': 'الحسابات',
        'Groups': 'المجموعات',
        'Sets': 'المجموعات',
        'Bins': 'الفواصل',
        'Parameters': 'المعلمات',
        'Field Parameters': 'معلمات الحقل',
        'Calculation Groups': 'مجموعات الحساب',
    },
    'hi-IN': {
        'Dimensions': 'आयाम',
        'Measures': 'माप',
        'Time Intelligence': 'समय बुद्धिमत्ता',
        'Flags': 'झंडे',
        'Calculations': 'गणनाएँ',
        'Groups': 'समूह',
        'Sets': 'सेट',
        'Bins': 'अंतराल',
        'Parameters': 'पैरामीटर',
        'Field Parameters': 'फ़ील्ड पैरामीटर',
        'Calculation Groups': 'गणना समूह',
    },
    'th-TH': {
        'Dimensions': 'มิติ',
        'Measures': 'การวัด',
        'Time Intelligence': 'ความฉลาดด้านเวลา',
        'Flags': 'ธง',
        'Calculations': 'การคำนวณ',
        'Groups': 'กลุ่ม',
        'Sets': 'ชุด',
        'Bins': 'ช่วง',
        'Parameters': 'พารามิเตอร์',
        'Field Parameters': 'พารามิเตอร์ฟิลด์',
        'Calculation Groups': 'กลุ่มการคำนวณ',
    },
}


def _get_display_folder_translations(culture_name):
    """Look up display folder translations for a given culture.

    Falls back to translating using the language portion (e.g. 'fr' from 'fr-CA').
    Returns empty dict if no translations are available.
    """
    # Exact match
    if culture_name in _DISPLAY_FOLDER_TRANSLATIONS:
        return _DISPLAY_FOLDER_TRANSLATIONS[culture_name]

    # Try language-only match (e.g. 'fr' from 'fr-CA' → 'fr-FR')
    lang = culture_name.split('-')[0].lower()
    for key, val in _DISPLAY_FOLDER_TRANSLATIONS.items():
        if key.split('-')[0].lower() == lang:
            return val

    return {}


def _write_database_tmdl(def_dir, model):
    """Generate database.tmdl."""
    compat = model.get('compatibilityLevel', 1567)
    if compat < 1600:
        compat = 1600

    content = f"database\n\tcompatibilityLevel: {compat}\n\n"

    filepath = os.path.join(def_dir, 'database.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_model_tmdl(def_dir, model, tables, roles=None, relationships=None):
    """Generate model.tmdl."""
    culture = model.get('culture', 'en-US')
    perspectives = model.get('perspectives', [])

    has_calc_groups = any(t.get('calculationGroup') for t in tables)

    lines = []
    lines.append("model Model")
    lines.append(f"\tculture: {culture}")
    lines.append("\tdefaultPowerBIDataSourceVersion: powerBI_V3")
    lines.append("\tsourceQueryCulture: en-US")
    if model.get('defaultMode') == 'directLake':
        lines.append("\tdefaultMode: directLake")
    if has_calc_groups:
        lines.append("\tdiscourageImplicitMeasures")
    lines.append("\tdataAccessOptions")
    lines.append("\t\tlegacyRedirects")
    lines.append("\t\treturnErrorValuesAsNull")
    lines.append("")

    # Table order annotation
    table_names = [t.get('name', '') for t in tables]
    table_names_json = '["' + '","'.join(table_names) + '"]'
    lines.append(f"annotation PBI_QueryOrder = {table_names_json}")
    lines.append("")

    # Ref tables
    for table in tables:
        tname = _quote_name(table.get('name', ''))
        lines.append(f"ref table {tname}")

    lines.append("")

    # Ref relationships
    if relationships:
        for rel in relationships:
            rel_id = rel.get('name', str(uuid.uuid4()))
            lines.append(f"ref relationship {rel_id}")
        lines.append("")

    # Ref the storage expression used by every table partition.
    expression_name = 'DatabaseQuery' if model.get('defaultMode') == 'directLake' else 'DataFolder'
    lines.append(f"ref expression {expression_name}")
    lines.append("")

    # Ref roles (RLS)
    if roles:
        for role in roles:
            rname = _quote_name(role.get('name', ''))
            lines.append(f"ref role {rname}")
        lines.append("")

    # Ref perspectives
    if perspectives:
        for persp in perspectives:
            pname = _quote_name(persp.get('name', 'Default'))
            lines.append(f"ref perspective {pname}")
        lines.append("")

    # Ref culture
    if culture and culture != 'en-US':
        lines.append(f"ref culture {_quote_name(culture)}")
        lines.append("")

    content = '\n'.join(lines) + '\n'

    filepath = os.path.join(def_dir, 'model.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_expressions_tmdl(def_dir, tables, datasources=None, incremental_params=None):
    """Generate expressions.tmdl with M parameters.

    Creates parameterized data source expressions:
    - DataFolder: for file-based data sources
    - ServerName: for server-based connections (SQL, Oracle, PostgreSQL, etc.)
    - DatabaseName: for database-based connections
    - RangeStart / RangeEnd: for incremental refresh (when configured)

    These M parameters allow easy switching between dev/staging/prod environments.
    """
    file_dirs = []          # directory paths for DataFolder
    has_file_source = False  # whether any file-based DataFolder ref exists
    server_names = set()
    database_names = set()

    for table in tables:
        for partition in table.get('partitions', []):
            source = partition.get('source', {})
            if isinstance(source, dict):
                expr = source.get('expression', '')
            elif isinstance(source, str):
                expr = source
            else:
                continue

            # Detect file-based sources (DataFolder references)
            if re.search(r'DataFolder\s*&\s*"\\', expr):
                has_file_source = True
            if re.search(r'File\.Contents\(', expr):
                has_file_source = True

            # Detect server/database references from M queries
            for m in re.finditer(r'(?:Sql\.Database|PostgreSQL\.Database|Oracle\.Database|Mysql\.Database)\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', expr):
                server_names.add(m.group(1))
                database_names.add(m.group(2))
            for m in re.finditer(r'(?:Snowflake\.Databases|AmazonRedshift\.Database|GoogleBigQuery\.Database)\s*\(\s*"([^"]+)"', expr):
                server_names.add(m.group(1))

    # Extract directory info from datasource connection metadata
    if datasources:
        for ds in (datasources if isinstance(datasources, list) else [datasources]):
            conn = ds.get('connection', {})
            server = conn.get('server', conn.get('host', ''))
            db = conn.get('dbname', conn.get('database', ''))
            if server:
                server_names.add(server)
            if db:
                database_names.add(db)

            # Extract file directory from connection details (filename / directory)
            for cmap_val in list(ds.get('connection_map', {}).values()) + [conn]:
                details = cmap_val.get('details', cmap_val) if isinstance(cmap_val, dict) else {}
                fn = details.get('filename', '')
                dr = details.get('directory', '')
                if fn:
                    norm = fn.replace('\\', '/').lstrip('/')
                    parent = norm.rsplit('/', 1)[0] if '/' in norm else ''
                    if parent:
                        file_dirs.append(parent)
                        has_file_source = True
                if dr:
                    file_dirs.append(dr.replace('\\', '/').lstrip('/'))
                    has_file_source = True

    default_folder = "C:\\Data"

    if file_dirs:
        unique_dirs = list(dict.fromkeys(file_dirs))  # deduplicate, preserve order

        if len(unique_dirs) == 1:
            common_dir = unique_dirs[0]
        else:
            common = os.path.commonprefix(unique_dirs)
            if '/' in common:
                common_dir = common[:common.rfind('/')]
            else:
                common_dir = common  # all in same directory

        if common_dir:
            default_folder = "C:\\" + common_dir.replace('/', '\\')

    # TMDL strings require doubled backslashes for literal backslash characters
    escaped_folder = default_folder.replace('\\', '\\\\')
    lines = []
    lines.append(f'expression DataFolder = "{escaped_folder}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]')
    lines.append("")

    # Add server/database M parameters for easy environment switching
    if server_names:
        default_server = sorted(server_names)[0]
        lines.append(f'expression ServerName = "{default_server}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]')
        lines.append("")

    if database_names:
        default_db = sorted(database_names)[0]
        lines.append(f'expression DatabaseName = "{default_db}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]')
        lines.append("")

    # Add RangeStart/RangeEnd parameters for incremental refresh
    if incremental_params:
        for param_name, param_expr in incremental_params:
            lines.append(f'expression {param_name} = {param_expr}')
            lines.append("")

    content = '\n'.join(lines) + '\n'

    filepath = os.path.join(def_dir, 'expressions.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_roles_tmdl(def_dir, roles):
    """Generate roles.tmdl with RLS role definitions."""
    if not roles:
        return

    lines = []

    for role in roles:
        role_name = _quote_name(role.get('name', 'DefaultRole'))
        model_permission = role.get('modelPermission', 'read')

        lines.append(f"role {role_name}")
        lines.append(f"\tmodelPermission: {model_permission}")

        migration_note = role.get('_migration_note', '')
        if migration_note:
            note_escaped = migration_note.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
            note_escaped = note_escaped.replace('"', '\\"')
            # Collapse multiple spaces from newline removal
            while '  ' in note_escaped:
                note_escaped = note_escaped.replace('  ', ' ')
            lines.append(f'\tannotation MigrationNote = "{note_escaped}"')

        lines.append("")

        for tp in role.get('tablePermissions', []):
            tp_name = tp.get('name', '') or ''
            if not tp_name:
                continue
            table_name = _quote_name(tp_name)
            filter_expr = tp.get('filterExpression', '')

            lines.append(f"\ttablePermission {table_name}")

            if filter_expr:
                filter_clean = filter_expr.replace('\n', ' ').replace('\r', ' ').strip()
                lines.append(f"\t\tfilterExpression = {filter_clean}")

            lines.append("")

    content = '\n'.join(lines) + '\n'

    filepath = os.path.join(def_dir, 'roles.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_relationships_tmdl(def_dir, relationships):
    """Generate relationships.tmdl."""
    if not relationships:
        filepath = os.path.join(def_dir, 'relationships.tmdl')
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("")
        return

    lines = []

    for rel in relationships:
        rel_id = rel.get('name', str(uuid.uuid4()))
        try:
            uuid.UUID(rel_id)
        except ValueError:
            rel_id = str(uuid.uuid4())

        from_table = _quote_name(rel.get('fromTable', ''))
        from_col = _quote_name(rel.get('fromColumn', ''))
        to_table = _quote_name(rel.get('toTable', ''))
        to_col = _quote_name(rel.get('toColumn', ''))

        lines.append(f"relationship {rel_id}")
        lines.append(f"\tfromColumn: {from_table}.{from_col}")
        lines.append(f"\ttoColumn: {to_table}.{to_col}")

        from_card = rel.get('fromCardinality', '')
        to_card = rel.get('toCardinality', '')
        if from_card == 'many' and to_card == 'many':
            lines.append("\tfromCardinality: many")
            lines.append("\ttoCardinality: many")
        elif from_card == 'many' and to_card == 'one':
            pass

        cfb = rel.get('crossFilteringBehavior', 'oneDirection')
        lines.append(f"\tcrossFilteringBehavior: {cfb}")

        if rel.get('isActive') == False:
            lines.append("\tisActive: false")

        lines.append("")

    content = '\n'.join(lines) + '\n'

    filepath = os.path.join(def_dir, 'relationships.tmdl')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _generate_table_description(table):
    """Auto-generate a human-readable description for a table.

    Priority: (1) explicit description, (2) caption-based, (3) synthesized
    from table name and column summary.
    """
    existing = table.get('description', '')
    if existing:
        return existing

    table_name = table.get('name', 'Table')
    columns = table.get('columns', [])
    measures = table.get('measures', [])

    col_names = [c.get('name', '') for c in columns[:8] if c.get('name')]
    col_summary = ', '.join(col_names)
    if len(columns) > 8:
        col_summary += f', ... ({len(columns)} columns total)'

    parts = [f"Contains {len(columns)} columns"]
    if measures:
        parts.append(f"{len(measures)} measures")
    parts_str = ' and '.join(parts)

    if col_summary:
        return f"{parts_str}: {col_summary}."
    return f"{parts_str}."


def _generate_column_description(column):
    """Auto-generate a description for a column when none exists.

    Uses data type, semantic role, and data category to create a readable
    description for PBI Copilot/Q&A.
    """
    existing = column.get('description', '')
    if existing:
        return existing

    col_name = column.get('name', 'Column')
    data_type = column.get('dataType', 'string')
    data_category = column.get('dataCategory', '')
    is_calculated = column.get('isCalculated', False)
    expression = column.get('expression', '')

    parts = []
    if is_calculated and expression:
        parts.append(f"Calculated column ({data_type})")
    else:
        parts.append(f"{data_type.capitalize()} column")

    if data_category:
        parts.append(f"categorized as {data_category}")

    if column.get('isKey', False):
        parts.append("(table key)")

    return '. '.join(parts) + '.'


def _generate_measure_description(measure):
    """Auto-generate a description for a measure when none exists.

    Includes the original Tableau formula as documentation when available.
    """
    existing = measure.get('description', '')
    if existing:
        return existing

    measure_name = measure.get('name', 'Measure')
    expression = measure.get('expression', '')
    original_formula = measure.get('_original_formula', '')

    parts = []
    if original_formula:
        parts.append(f"Migrated from Tableau: {original_formula}")
    if expression and expression != '0':
        dax_preview = expression[:200]
        if len(expression) > 200:
            # Ensure truncation doesn't leave unclosed brackets
            # which would cause validator false positives
            open_brackets = dax_preview.count('[') - dax_preview.count(']')
            open_parens = dax_preview.count('(') - dax_preview.count(')')
            suffix = ']' * max(0, open_brackets) + ')' * max(0, open_parens)
            dax_preview += suffix + '...'
        parts.append(f"DAX: {dax_preview}")

    if not parts:
        parts.append(f"Measure: {measure_name}")

    return ' | '.join(parts)


def _write_table_tmdl(tables_dir, table):
    """Generate a {table_name}.tmdl file."""
    table_name = table.get('name', 'Table')
    tname_quoted = _quote_name(table_name)

    lines = []
    lines.append(f"table {tname_quoted}")
    lines.append(f"\tlineageTag: {uuid.uuid4()}")

    # Table description — TMDL does not support 'description:' at the table level.
    # The description is preserved as a Copilot_TableDescription annotation instead.
    table_desc = _generate_table_description(table)

    lines.append("")

    # Calculation group block (must come before columns/measures)
    cg = table.get('calculationGroup')
    if cg:
        lines.append("\tcalculationGroup")
        lines.append(f"\t\tprecedence: {cg.get('precedence', 0)}")
        lines.append("")
        for item in cg.get('calculationItems', []):
            item_name = _quote_name(item.get('name', 'Item'))
            lines.append(f"\t\tcalculationItem {item_name}")
            expr = item.get('expression', 'CALCULATE(SELECTEDMEASURE())')
            if '\n' in expr:
                lines.append(f"\t\t\texpression = ```")
                for el in expr.split('\n'):
                    lines.append(f"\t\t\t\t{el}")
                lines.append("\t\t\t\t```")
            else:
                lines.append(f"\t\t\texpression = {expr}")
            ordinal = item.get('ordinal')
            if ordinal is not None:
                lines.append(f"\t\t\tordinal: {ordinal}")
            lines.append("")
        lines.append("")

    # Measures (before columns, as in PBI Hero reference)
    # Deduplicate by name — first wins
    seen_measure_names = set()
    for measure in table.get('measures', []):
        mn = measure.get('name', '').lower()
        if mn not in seen_measure_names:
            seen_measure_names.add(mn)
            _write_measure(lines, measure)

    # Columns (deduplicate by name — last wins)
    seen_col_names = set()
    deduped_columns = []
    for column in reversed(table.get('columns', [])):
        cn = column.get('name', '').lower()
        if cn not in seen_col_names:
            seen_col_names.add(cn)
            deduped_columns.append(column)
    deduped_columns.reverse()
    for column in deduped_columns:
        _write_column(lines, column)

    # Hierarchies
    for hierarchy in table.get('hierarchies', []):
        _write_hierarchy(lines, hierarchy)

    # Partition
    for partition in table.get('partitions', []):
        _write_partition(lines, table_name, partition)

    # Incremental refresh policy (if configured)
    refresh_policy = table.get('refreshPolicy')
    if refresh_policy:
        _write_refresh_policy(lines, refresh_policy)

    # Annotations
    lines.append("\tannotation PBI_ResultType = Table")

    # Copilot optimization hints
    if table_name == 'Calendar':
        lines.append("\tannotation Copilot_DateTable = true")
    lines.append(f"\tannotation Copilot_TableDescription = {_generate_table_description(table)}")

    # Lineage annotations (from merge)
    source_wbs = table.get('_source_workbooks', [])
    if source_wbs:
        lines.append(f"\tannotation MigrationSource = {json.dumps(source_wbs)}")
    merge_action = table.get('_merge_action', '')
    if merge_action:
        lines.append(f'\tannotation MergeAction = {merge_action}')
    lines.append("")

    content = '\n'.join(lines) + '\n'

    filename = _safe_filename(table_name, tables_dir, '.tmdl') + '.tmdl'
    filepath = os.path.join(tables_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_measure(lines, measure):
    """Write a measure in TMDL."""
    mname = _quote_name(measure.get('name', 'Measure'))
    expression = measure.get('expression') or '0'

    if '\n' in expression:
        lines.append(f"\tmeasure {mname} = ```")
        for expr_line in expression.split('\n'):
            lines.append(f"\t\t\t{expr_line}")
        lines.append("\t\t\t```")
    else:
        lines.append(f"\tmeasure {mname} = {expression}")

    fmt = measure.get('formatString', '')
    if fmt and fmt != '0':
        lines.append(f"\t\tformatString: {fmt}")

    folder = measure.get('displayFolder', '')
    if folder:
        lines.append(f"\t\tdisplayFolder: {folder}")

    if measure.get('isHidden', False):
        lines.append("\t\tisHidden")

    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")

    # Description as annotation for Copilot/Q&A readiness
    measure_desc = _generate_measure_description(measure)
    safe_desc = measure_desc.replace('\n', ' ').replace('\r', '').strip()
    if safe_desc:
        lines.append(f"\t\tannotation Copilot_Description = {safe_desc}")

    # Lineage annotations (from merge)
    source_wbs = measure.get('_source_workbooks', [])
    if source_wbs:
        lines.append(f"\t\tannotation MigrationSource = {json.dumps(source_wbs)}")
    merge_action = measure.get('_merge_action', '')
    if merge_action:
        lines.append(f'\t\tannotation MergeAction = {merge_action}')

    lines.append("")


def _write_column_properties(lines, column):
    """Write shared column properties (formatString, lineageTag, summarizeBy, etc.)."""
    fmt = column.get('formatString', '')
    if fmt:
        lines.append(f"\t\tformatString: {fmt}")

    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")

    summarize = _tmdl_summarize(column.get('summarizeBy', 'none'))
    lines.append(f"\t\tsummarizeBy: {summarize}")


def _write_column_flags(lines, column):
    """Write optional column flags (isHidden, isKey, dataCategory, etc.)."""
    if column.get('isHidden', False):
        lines.append("\t\tisHidden")
    if column.get('isKey', False):
        lines.append("\t\tisKey")
    data_category = column.get('dataCategory', '')
    if data_category:
        lines.append(f"\t\tdataCategory: {data_category}")
    display_folder = column.get('displayFolder', '')
    if display_folder:
        lines.append(f"\t\tdisplayFolder: {display_folder}")
    sort_by = column.get('sortByColumn', '')
    if sort_by:
        lines.append(f"\t\tsortByColumn: {_quote_name(sort_by)}")

    # Custom annotations (e.g. alternateOf for agg tables)
    for ann in column.get('annotations', []):
        ann_name = ann.get('name', '')
        ann_value = ann.get('value', '')
        if ann_name and ann_value:
            lines.append(f"\t\tannotation {ann_name} = {ann_value}")

    # Description as annotation for Copilot/Q&A readiness, as for measures
    column_desc = _generate_column_description(column)
    safe_desc = column_desc.replace('\n', ' ').replace('\r', '').strip()
    if safe_desc:
        lines.append(f"\t\tannotation Copilot_Description = {safe_desc}")

    # Copilot optimization: mark technical columns as hidden from Copilot
    # Match patterns like OrderID, Customer_ID, product_key, etc.
    # but not words like "Valid", "Fluid", "Avid"
    col_name = column.get('name', '')
    _is_technical = bool(re.search(
        r'(?:_id|_key|_sk|_fk|_pk|ID|Key|SK|FK|PK)$',
        col_name
    ))
    if _is_technical:
        lines.append("\t\tannotation Copilot_Hidden = true")

    lines.append("")
    lines.append("\t\tannotation SummarizationSetBy = Automatic")
    lines.append("")


def _write_column(lines, column):
    """Write a column in TMDL (physical or calculated)."""
    col_name = column.get('name', 'Column')
    cname_quoted = _quote_name(col_name)
    data_type = _tmdl_datatype(column.get('dataType', 'string'))
    expression = column.get('expression', '')
    is_calculated = column.get('isCalculated', False)

    if is_calculated and expression:
        if '\n' in expression:
            lines.append(f"\tcolumn {cname_quoted} = ```")
            for expr_line in expression.split('\n'):
                lines.append(f"\t\t\t{expr_line}")
            lines.append("\t\t\t```")
        else:
            lines.append(f"\tcolumn {cname_quoted} = {expression}")
        lines.append(f"\t\tdataType: {data_type}")
        _write_column_properties(lines, column)
        _write_column_flags(lines, column)
    else:
        lines.append(f"\tcolumn {cname_quoted}")
        lines.append(f"\t\tdataType: {data_type}")
        _write_column_properties(lines, column)

        source_col = column.get('sourceColumn', col_name)
        source_col_quoted = _quote_name(source_col) if re.search(r'[^a-zA-Z0-9_]', source_col) else source_col
        lines.append(f"\t\tsourceColumn: {source_col_quoted}")
        _write_column_flags(lines, column)


def _write_hierarchy(lines, hierarchy):
    """Write a hierarchy in TMDL."""
    h_name = _quote_name(hierarchy.get('name', 'Hierarchy'))
    levels = hierarchy.get('levels', [])

    lines.append(f"\thierarchy {h_name}")
    lines.append(f"\t\tlineageTag: {uuid.uuid4()}")
    lines.append("")

    for level in levels:
        level_name = _quote_name(level.get('name', 'Level'))
        col_name = _quote_name(level.get('column', level.get('name', '')))
        ordinal = level.get('ordinal', 0)

        lines.append(f"\t\tlevel {level_name}")
        lines.append(f"\t\t\tordinal: {ordinal}")
        lines.append(f"\t\t\tcolumn: {col_name}")
        lines.append(f"\t\t\tlineageTag: {uuid.uuid4()}")
        lines.append("")

    lines.append("")


def _write_refresh_policy(lines, policy):
    """Write an incremental refresh policy in TMDL format.

    The policy dict should contain:
      - incrementalGranularity: 'Day' | 'Month' | 'Quarter' | 'Year'
      - incrementalPeriods: int (number of periods to refresh)
      - rollingWindowGranularity: 'Day' | 'Month' | 'Quarter' | 'Year'
      - rollingWindowPeriods: int (total window size)
      - pollingExpression: M expression for the date column (optional)
      - sourceExpression: M source expression (optional)
    """
    lines.append("\trefreshPolicy")
    gran = policy.get('incrementalGranularity', 'Day')
    inc_periods = policy.get('incrementalPeriods', 1)
    rw_gran = policy.get('rollingWindowGranularity', 'Month')
    rw_periods = policy.get('rollingWindowPeriods', 12)

    lines.append(f"\t\tincrementalGranularity: {gran}")
    lines.append(f"\t\tincrementalPeriods: {inc_periods}")
    lines.append(f"\t\trollingWindowGranularity: {rw_gran}")
    lines.append(f"\t\trollingWindowPeriods: {rw_periods}")

    # Polling expression (the date column to filter on)
    polling = policy.get('pollingExpression', '')
    if polling:
        lines.append(f"\t\tpollingExpression =")
        for pl in polling.split('\n'):
            lines.append(f"\t\t\t\t{pl}")

    # Source expression (the M query with RangeStart/RangeEnd parameters)
    source_expr = policy.get('sourceExpression', '')
    if source_expr:
        lines.append(f"\t\tsourceExpression =")
        for sl in source_expr.split('\n'):
            lines.append(f"\t\t\t\t{sl}")

    lines.append("")


def _write_direct_lake_expression(def_dir, direct_lake):
    """Write the shared OneLake expression for Direct Lake entity partitions."""
    workspace_id = direct_lake.get('workspace_id', '')
    lakehouse_id = direct_lake.get('lakehouse_id', '')
    source_url = (
        'https://onelake.dfs.fabric.microsoft.com/'
        f'{workspace_id}/{lakehouse_id}'
    )
    lines = [
        'expression DatabaseQuery =',
        '\t\tlet',
        f'\t\t\tSource = AzureStorage.DataLake("{source_url}", [HierarchicalNavigation=true])',
        '\t\tin',
        '\t\t\tSource',
        '',
    ]
    path = os.path.join(def_dir, 'expressions.tmdl')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _write_partition(lines, table_name, partition):
    """Write a partition in TMDL."""
    part_name = f"{table_name}-{uuid.uuid4()}"
    mode = partition.get('mode', 'import')
    source = partition.get('source', {})
    source_type = source.get('type', 'm')
    expression = source.get('expression', '')

    lines.append(f"\tpartition {_quote_name(part_name)} = {source_type}")
    lines.append(f"\t\tmode: {mode}")

    if source_type == 'entity':
        lines.append("\t\tsource")
        lines.append(f"\t\t\tentityName: {_quote_name(source.get('entityName', table_name))}")
        schema_name = source.get('schemaName', '')
        if schema_name:
            lines.append(f"\t\t\tschemaName: {_quote_name(schema_name)}")
        lines.append(
            f"\t\t\texpressionSource: {_quote_name(source.get('expressionSource', 'DatabaseQuery'))}"
        )
        lines.append("")
        return

    # Calculation group partitions have no source expression
    if source_type == 'calculationGroup':
        lines.append("")
        return

    if expression:
        if source_type == 'calculated':
            expr_clean = expression.replace('\r\n', '\n').replace('\r', '\n')
            if '\n' in expr_clean:
                lines.append("\t\tsource = ```")
                for expr_line in expr_clean.split('\n'):
                    lines.append(f"\t\t\t\t{expr_line}")
                lines.append("\t\t\t\t```")
            else:
                lines.append(f"\t\tsource = {expr_clean}")
        else:
            # Strip inline // comments and fix corrupted patterns before writing
            expression = _strip_m_inline_comments(expression)
            # Validate M if/else balance before writing
            expression = _fix_m_if_else_balance(expression)
            lines.append(f"\t\tsource =")
            for expr_line in expression.split('\n'):
                lines.append(f"\t\t\t\t{expr_line}")
    else:
        lines.append(f"\t\tsource =")
        lines.append("\t\t\t\tlet")
        lines.append("\t\t\t\t\tSource = #table(type table [], {})")
        lines.append("\t\t\t\t\t// TODO: Configure data source — replace with actual connection")
        lines.append("\t\t\t\tin")
        lines.append("\t\t\t\t\tSource")

    lines.append("")
