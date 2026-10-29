"""
PySpark Notebook Generator for Microsoft Fabric.

Generates Jupyter notebooks (.ipynb) with PySpark code for:
1. Data ingestion from original sources into Lakehouse Delta tables
2. Data transformations (calculated columns materialised via withColumn,
   as decided by ``calc_column_plan``)
3. Schema validation and data quality checks
"""

import json
import os
import re
from datetime import datetime

from .calc_column_plan import plan_calculated_columns
from .fabric_item import logical_id, write_platform
from .fabric_naming import make_python_var as _make_var_name
from .fabric_naming import sanitize_table_name as _sanitize_table_name
from .fabric_sources import is_file_connection, table_connection
from .pyspark_converter import todo_statement

# Connection type → PySpark read snippet
_SPARK_READ_TEMPLATES = {
    'SQL Server': (
        '# Read from SQL Server\n'
        'jdbc_url = "jdbc:sqlserver://{server};databaseName={database}"\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("jdbc") \\\n'
        '    .option("url", jdbc_url) \\\n'
        '    .option("dbtable", "{table_name}") \\\n'
        '    .option("user", "<username>") \\\n'
        '    .option("password", "<password>") \\\n'
        '    .load()\n'
    ),
    'PostgreSQL': (
        '# Read from PostgreSQL\n'
        'jdbc_url = "jdbc:postgresql://{server}:{port}/{database}"\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("jdbc") \\\n'
        '    .option("url", jdbc_url) \\\n'
        '    .option("dbtable", "{table_name}") \\\n'
        '    .option("user", "<username>") \\\n'
        '    .option("password", "<password>") \\\n'
        '    .option("driver", "org.postgresql.Driver") \\\n'
        '    .load()\n'
    ),
    'Oracle': (
        '# Read from Oracle\n'
        'jdbc_url = "jdbc:oracle:thin:@{server}:{port}/{service}"\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("jdbc") \\\n'
        '    .option("url", jdbc_url) \\\n'
        '    .option("dbtable", "{table_name}") \\\n'
        '    .option("user", "<username>") \\\n'
        '    .option("password", "<password>") \\\n'
        '    .option("driver", "oracle.jdbc.driver.OracleDriver") \\\n'
        '    .load()\n'
    ),
    'MySQL': (
        '# Read from MySQL\n'
        'jdbc_url = "jdbc:mysql://{server}:{port}/{database}"\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("jdbc") \\\n'
        '    .option("url", jdbc_url) \\\n'
        '    .option("dbtable", "{table_name}") \\\n'
        '    .option("user", "<username>") \\\n'
        '    .option("password", "<password>") \\\n'
        '    .load()\n'
    ),
    'Snowflake': (
        '# Read from Snowflake\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("snowflake") \\\n'
        '    .option("sfURL", "{server}") \\\n'
        '    .option("sfDatabase", "{database}") \\\n'
        '    .option("sfSchema", "{schema}") \\\n'
        '    .option("sfWarehouse", "{warehouse}") \\\n'
        '    .option("dbtable", "{table_name}") \\\n'
        '    .option("sfUser", "<username>") \\\n'
        '    .option("sfPassword", "<password>") \\\n'
        '    .load()\n'
    ),
    'BigQuery': (
        '# Read from BigQuery\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("bigquery") \\\n'
        '    .option("table", "{project}.{dataset}.{table_name}") \\\n'
        '    .load()\n'
    ),
    'CSV': (
        '# Read CSV file\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("csv") \\\n'
        '    .option("header", "true") \\\n'
        '    .option("inferSchema", "true") \\\n'
        '    .option("delimiter", "{delimiter}") \\\n'
        '    .load("Files/{filename}")\n'
    ),
    'Excel': (
        '# Read the Excel sheet converted to CSV during local staging\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("csv") \\\n'
        '    .option("header", "true") \\\n'
        '    .option("inferSchema", "true") \\\n'
        '    .load("Files/{filename}")\n'
    ),
    'Custom SQL': (
        '# Read via Custom SQL\n'
        'jdbc_url = "jdbc:sqlserver://{server};databaseName={database}"\n'
        'custom_query = """{custom_sql}"""\n'
        'df_{table_var} = spark.read \\\n'
        '    .format("jdbc") \\\n'
        '    .option("url", jdbc_url) \\\n'
        '    .option("query", custom_query) \\\n'
        '    .option("user", "<username>") \\\n'
        '    .option("password", "<password>") \\\n'
        '    .load()\n'
    ),
}


def _make_notebook(cells, metadata=None):
    """Create a Jupyter notebook JSON structure."""
    if metadata is None:
        metadata = {}
    lakehouse_name = metadata.get('lakehouse_name', '')
    lakehouse_id = metadata.get('lakehouse_id', '')
    workspace_id = metadata.get('workspace_id', '')
    dependencies = {}
    if lakehouse_id and workspace_id:
        dependencies = {
            'lakehouse': {
                'default_lakehouse': lakehouse_id,
                'default_lakehouse_workspace_id': workspace_id,
                'default_lakehouse_name': lakehouse_name,
            },
        }
    return {
        'nbformat': 4,
        'nbformat_minor': 5,
        'metadata': {
            'kernel_info': {'name': 'synapse_pyspark'},
            'kernelspec': {
                'name': 'synapse_pyspark',
                'display_name': 'Synapse PySpark',
                'language': 'Python',
            },
            'language_info': {
                'name': 'python',
                'version': '3.10',
                'mimetype': 'text/x-python',
                'file_extension': '.py',
                'pygments_lexer': 'ipython3',
                'codemirror_mode': {'name': 'ipython', 'version': 3},
            },
            'microsoft': {
                'language': 'python',
                'ms_spell_check': {'ms_spell_check_language': 'en'},
            },
            'trident': {
                'lakehouse': {
                    'default_lakehouse_name': lakehouse_name,
                    'known_lakehouses': [],
                },
            },
            'dependencies': dependencies,
            **metadata,
        },
        'cells': cells,
    }


def _markdown_cell(source, cell_id=None):
    """Create a markdown cell."""
    return {
        'cell_type': 'markdown',
        'metadata': {'nteract': {'transient': {'deleting': False}}},
        'source': source if isinstance(source, list) else [source],
        'id': cell_id or '',
    }


def _code_cell(source, cell_id=None):
    """Create a code cell."""
    return {
        'cell_type': 'code',
        'metadata': {
            'jupyter': {'outputs_hidden': False, 'source_hidden': False},
            'nteract': {'transient': {'deleting': False}},
            'microsoft': {'language': 'python'},
        },
        'source': source if isinstance(source, list) else [source],
        'outputs': [],
        'execution_count': None,
        'id': cell_id or '',
    }


def _source_lines(text):
    """Split ``text`` into notebook source lines (every line but the last ends with a newline)."""
    lines = text.split('\n')
    return [line + '\n' for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def _one_line(text, limit=None):
    """Collapse whitespace and non-printable characters so the text fits on one line."""
    collapsed = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in str(text or '')).split())
    if limit and len(collapsed) > limit:
        collapsed = collapsed[:limit - 1].rstrip() + '…'
    return collapsed


def _md_code(text, limit=None):
    """Render ``text`` as a Markdown inline code span, whatever backticks it contains."""
    text = _one_line(text, limit) or ' '
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    fence = '`' * (longest + 1)
    pad = ' ' if text.startswith('`') or text.endswith('`') else ''
    return f'{fence}{pad}{text}{pad}{fence}'


def _md_text(text, limit=None):
    """Escape Markdown and HTML specials so workbook-controlled text renders literally."""
    return re.sub(r'([\\`*_\[\]<>&~])', r'\\\1', _one_line(text, limit))


_ASSUMPTIONS = [
    '- Parameters are replaced by the value they had when the workbook was migrated.\n',
    '- `TODAY()` and `NOW()` are evaluated when the cell runs, not when a report is viewed.\n',
    '- Spark runs with `spark.sql.ansi.enabled = false` (the setup cell sets it): a failed cast or a division by zero gives NULL.\n',
    '- String comparisons are case-sensitive (Spark binary collation).\n',
    '- Results outside a function domain (for example `SQRT` of a negative number) were not compared with Tableau.\n',
    '- Field references use the Tableau column names: rename the Delta columns, or edit the statements, if the ingestion wrote other names.\n',
    '- Nothing in this notebook has been executed on Spark: review the output before relying on it.\n',
]


class NotebookGenerator:
    """Generates PySpark notebooks for Fabric ETL pipelines."""

    def __init__(self, project_dir, project_name, item_id=None,
                 lakehouse_id=None, workspace_id=None):
        self.project_dir = project_dir
        self.project_name = project_name
        self.item_id = item_id or logical_id(project_name, 'Notebook')
        self.lakehouse_id = lakehouse_id or logical_id(project_name, 'Lakehouse')
        self.workspace_id = workspace_id or logical_id(project_name, 'Workspace')
        self.notebook_dir = os.path.join(project_dir, f'{project_name}.Notebook')
        os.makedirs(self.notebook_dir, exist_ok=True)

    def generate(self, extracted_data):
        """Generate PySpark notebooks from extracted Tableau data.

        Args:
            extracted_data: Dict with 'datasources', 'custom_sql', etc.

        Returns:
            Dict with generation stats
            {'cells': int, 'notebooks': int, 'calc_columns': int,
             'calc_columns_pending': int}
            ``calc_columns`` counts the columns materialised by the
            transformations; ``calc_columns_pending`` those left as TODO.
        """
        datasources = extracted_data.get('datasources', [])
        custom_sql = extracted_data.get('custom_sql', [])
        calculations = extracted_data.get('calculations', [])

        plan = plan_calculated_columns(extracted_data)

        etl_cells = self._generate_etl_cells(datasources, custom_sql)
        etl_nb = _make_notebook(etl_cells, {
            'lakehouse_name': f'{self.project_name}_Lakehouse',
            'description': f'ETL pipeline for {self.project_name}',
        })

        etl_path = os.path.join(self.notebook_dir, 'etl_pipeline.ipynb')
        with open(etl_path, 'w', encoding='utf-8') as f:
            json.dump(etl_nb, f, indent=2, ensure_ascii=False)

        total_cells = len(etl_cells)
        deployable_cells = list(etl_cells)

        if calculations:
            transform_cells = self._generate_transformation_cells(plan)
            transform_nb = _make_notebook(transform_cells, {
                'lakehouse_name': f'{self.project_name}_Lakehouse',
                'description': f'Data transformations for {self.project_name}',
            })

            transform_path = os.path.join(self.notebook_dir, 'transformations.ipynb')
            with open(transform_path, 'w', encoding='utf-8') as f:
                json.dump(transform_nb, f, indent=2, ensure_ascii=False)

            total_cells += len(transform_cells)
            deployable_cells.extend(transform_cells)

        deployable_nb = _make_notebook(deployable_cells, {
            'lakehouse_name': f'{self.project_name}_Lakehouse',
            'lakehouse_id': self.lakehouse_id,
            'workspace_id': self.workspace_id,
            'description': f'ETL and transformations for {self.project_name}',
        })
        deployable_path = os.path.join(
            self.notebook_dir, 'artifact.content.ipynb')
        with open(deployable_path, 'w', encoding='utf-8') as f:
            json.dump(deployable_nb, f, indent=2, ensure_ascii=False)

        write_platform(
            self.notebook_dir,
            'Notebook',
            f'{self.project_name}_Notebook',
            self.item_id,
        )

        return {
            'cells': total_cells,
            'notebooks': 1,
            'calc_columns': len(plan.materialised),
            'calc_columns_pending': len(plan.pending),
        }

    def _generate_etl_cells(self, datasources, custom_sql):
        """Generate cells for the ETL pipeline notebook."""
        cells = []

        cells.append(_markdown_cell([
            f'# ETL Pipeline — {self.project_name}\n',
            '\n',
            'This notebook ingests data from the original Tableau data sources\n',
            'into Fabric Lakehouse Delta tables.\n',
            '\n',
            f'**Generated:** {datetime.now().strftime("%Y-%m-%d %H:%M")}\n',
        ]))

        cells.append(_code_cell([
            '# Setup\n',
            'from pyspark.sql import SparkSession\n',
            'from pyspark.sql.types import *\n',
            'from pyspark.sql.functions import *\n',
            'from datetime import datetime\n',
            '\n',
            f'LAKEHOUSE_NAME = "{self.project_name}_Lakehouse"\n',
            'print(f"ETL Pipeline started: {datetime.now()}")\n',
        ]))

        tables_info = []
        seen_tables = set()

        for ds in datasources:
            for table in ds.get('tables', []):
                table_name = table.get('name', '')
                if not table_name or table_name in seen_tables:
                    continue
                conn = table_connection(ds, table)
                if not is_file_connection(conn):
                    continue
                seen_tables.add(table_name)

                tables_info.append({
                    'name': table_name,
                    'connection': conn,
                    'columns': table.get('columns', []),
                })

        cells.append(_markdown_cell([
            '## Data Ingestion\n',
            '\n',
            f'Loading {len(tables_info)} table(s) from source systems.\n',
        ]))

        for table_info in tables_info:
            table_name = table_info['name']
            conn = table_info['connection']
            conn_type = conn.get('type', 'Unknown')
            details = conn.get('details', {})
            table_var = _make_var_name(table_name)
            lh_table = _sanitize_table_name(table_name)

            cells.append(_markdown_cell(f'### Table: `{table_name}` ({conn_type})'))

            template = _SPARK_READ_TEMPLATES.get(conn_type, '')
            if template:
                filename = details.get('filename', '')
                if conn_type == 'Excel':
                    filename = f'{lh_table}.csv'
                code = template.format(
                    table_var=table_var,
                    table_name=table_name,
                    server=details.get('server', 'localhost'),
                    port=details.get('port', ''),
                    database=details.get('database', ''),
                    schema=details.get('schema', 'public'),
                    warehouse=details.get('warehouse', ''),
                    project=details.get('project', ''),
                    dataset=details.get('dataset', ''),
                    filename=filename,
                    delimiter=details.get('delimiter', ','),
                    service=details.get('service', ''),
                    custom_sql='',
                )
            else:
                code = (
                    f'# TODO: Configure data source for {conn_type}\n'
                    f'df_{table_var} = spark.createDataFrame([], schema=StructType([]))\n'
                )

            code += f'\n# Preview\ndf_{table_var}.printSchema()\n'
            code += f'print(f"Rows: {{df_{table_var}.count()}}")\n'
            code += '\n# Write to Lakehouse Delta table\n'
            code += f'df_{table_var}.write \\\n'
            code += '    .format("delta") \\\n'
            code += '    .mode("overwrite") \\\n'
            code += f'    .saveAsTable("{lh_table}")\n'
            code += f'print(f"Written to Lakehouse: {lh_table}")\n'

            cells.append(_code_cell(code))

        if custom_sql:
            cells.append(_markdown_cell('## Custom SQL Queries'))
            for sql_entry in custom_sql:
                sql_name = sql_entry.get('name', 'Custom Query')
                sql_query = sql_entry.get('query', '')
                table_var = _make_var_name(sql_name)
                lh_table = _sanitize_table_name(sql_name)

                cells.append(_markdown_cell(f'### {sql_name}'))
                code = (
                    f'# Custom SQL: {sql_name}\n'
                    f'custom_query = """\n{sql_query}\n"""\n\n'
                    f'# TODO: Configure JDBC connection\n'
                    f'# df_{table_var} = spark.read.format("jdbc").option("query", custom_query).load()\n'
                    f'# df_{table_var}.write.format("delta").mode("overwrite").saveAsTable("{lh_table}")\n'
                )
                cells.append(_code_cell(code))

        cells.append(_markdown_cell('## Summary'))
        cells.append(_code_cell([
            'print("\\n" + "=" * 60)\n',
            'print("ETL Pipeline Complete")\n',
            'print("=" * 60)\n',
            f'print(f"Tables loaded: {len(tables_info)}")\n',
            'print(f"Completed: {datetime.now()}")\n',
            '\n',
            'print("\\nLakehouse tables:")\n',
            'for t in spark.catalog.listTables():\n',
            '    print(f"  - {t.name} ({t.tableType})")\n',
        ]))

        return cells

    def _generate_transformation_cells(self, plan):
        """Generate the transformation cells from a ``CalcColumnPlan``.

        A code cell holds either one validated ``withColumn`` statement or
        commented lines saying why the column was not converted, so
        "Run all" cannot stop on a formula the converter refused.
        """
        cells = []

        cells.append(_markdown_cell([
            f'# Data Transformations — {self.project_name}\n',
            '\n',
            'Materialises the row-level calculated columns as physical columns\n',
            'of the Lakehouse Delta tables via PySpark `withColumn()`.\n',
            '\n',
            f'**Generated:** {datetime.now().strftime("%Y-%m-%d %H:%M")}\n',
            '\n',
            '**Assumptions**\n',
            '\n',
        ] + _ASSUMPTIONS))

        cells.append(_code_cell([
            'from pyspark.sql import SparkSession\n',
            'import pyspark.sql.functions as F\n',
            'from pyspark.sql.types import *\n',
            'from pyspark.sql.window import Window\n',
            'from datetime import datetime\n',
            '\n',
            '# Tableau semantics: a failed cast or a division by zero gives NULL.\n',
            'spark.conf.set("spark.sql.ansi.enabled", "false")\n',
            '\n',
            'print(f"Transformations started: {datetime.now()}")\n',
        ]))

        for delta_table, columns in plan.by_table():
            done = [column for column in columns if column.materialised]
            cells.append(_markdown_cell([
                f'## Table {_md_code(delta_table)}\n',
                '\n',
                f'{len(done)} of {len(columns)} calculated column(s) are materialised.\n',
            ]))

            if done:
                cells.append(_code_cell([
                    f'df = spark.read.format("delta").table({json.dumps(delta_table)})\n',
                    'print(f"Rows: {df.count()}, Columns: {len(df.columns)}")\n',
                ]))

            for column in columns:
                cells.append(_markdown_cell(self._column_header(column)))
                if column.materialised:
                    statement = column.statement
                else:
                    statement = todo_statement(
                        column.caption, column.reason or 'not converted',
                        column.formula)
                cells.append(_code_cell(_source_lines(statement)))

            if done:
                cells.append(_code_cell([
                    '# Overwrite the Delta table with the new columns\n',
                    'df.write.format("delta").mode("overwrite")'
                    '.option("overwriteSchema", "true")'
                    f'.saveAsTable({json.dumps(delta_table)})\n',
                    f'print(f"Written {{len(df.columns)}} columns to '
                    f'{delta_table}")\n',
                ]))

        unplaced = plan.unplaced()
        if unplaced:
            lines = [
                '## Calculated columns without a Delta table\n',
                '\n',
                'This notebook does not materialise these calculations:\n',
                '\n',
            ]
            for column in unplaced:
                lines.append(
                    f'- {_md_code(column.caption)}: '
                    f'{_md_text(column.reason, 240)}\n')
            cells.append(_markdown_cell(lines))

        if plan.measures:
            lines = [
                '## Calculations kept as DAX measures (informational)\n',
                '\n',
                'These calculations aggregate, or cannot be computed row by row,\n',
                'so this notebook does not materialise them.\n',
                '\n',
            ]
            for measure in plan.measures:
                lines.append(
                    f'- {_md_code(measure.caption)}: '
                    f'{_md_text(measure.reason, 120)} — '
                    f'{_md_code(measure.formula, 160)}\n')
            cells.append(_markdown_cell(lines))

        cells.append(_markdown_cell('## Summary'))
        cells.append(_code_cell([
            'print("\\n" + "=" * 60)\n',
            'print("Transformations Complete")\n',
            'print("=" * 60)\n',
            f'print("Calculated columns materialised:     {len(plan.materialised)}")\n',
            f'print("Calculated columns not converted:    {len(plan.pending)}")\n',
            f'print("Calculations kept as DAX measures:   {len(plan.measures)}")\n',
            'print(f"Completed: {datetime.now()}")\n',
        ]))

        return cells

    @staticmethod
    def _column_header(column):
        """Markdown cell describing one planned column."""
        lines = [
            f'### {_md_code(column.caption)} → {_md_code(column.delta_name)} '
            f'({column.spark_type})\n',
            '\n',
            f'**Tableau formula:** {_md_code(column.formula, 400)}\n',
        ]
        for note in column.notes:
            lines.append(f'- {_md_text(note, 240)}\n')
        if not column.materialised:
            lines.append(
                f'- **Not converted:** {_md_text(column.reason, 240)}\n')
        return lines
