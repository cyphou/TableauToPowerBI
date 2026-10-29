"""
Lakehouse Definition Generator for Microsoft Fabric.

Generates Lakehouse table schema definitions from extracted
Tableau datasource metadata. Creates:
- lakehouse_definition.json: Lakehouse metadata + table schemas
- Table DDL scripts for Delta Lake tables
- Table metadata JSON for other generators
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime

from .calc_column_plan import plan_calculated_columns
from .fabric_constants import map_to_spark_type as _map_to_spark_type
from .fabric_item import logical_id, write_platform
from .fabric_naming import sanitize_column_name as _sanitize_column_name
from .fabric_naming import sanitize_table_name as _sanitize_table_name

logger = logging.getLogger(__name__)


class LakehouseGenerator:
    """Generates Lakehouse definitions from Tableau datasources."""

    def __init__(self, project_dir, project_name, item_id=None):
        self.project_dir = project_dir
        self.project_name = project_name
        self.item_id = item_id or logical_id(project_name, 'Lakehouse')
        self.lakehouse_dir = os.path.join(project_dir, f'{project_name}.Lakehouse')
        os.makedirs(self.lakehouse_dir, exist_ok=True)

    def generate(self, extracted_data):
        """Generate Lakehouse definition from extracted Tableau data.

        Args:
            extracted_data: Dict with 'datasources', 'calculations', etc.

        Returns:
            Dict with generation stats {'tables': int, 'columns': int,
                                         'calc_columns': int,
                                         'calc_columns_pending': int}
            ``calc_columns`` counts the calculated columns the PySpark
            notebook materialises (see ``calc_column_plan``); the ones it
            could not convert are listed in ``table_metadata.json``.
        """
        datasources = extracted_data.get('datasources', [])
        custom_sql = extracted_data.get('custom_sql', [])

        plan = plan_calculated_columns(extracted_data)

        tables = []
        seen_tables = set()

        for ds in datasources:
            for table in ds.get('tables', []):
                table_name = _sanitize_table_name(table.get('name', ''))
                if not table_name or table_name in seen_tables:
                    continue
                seen_tables.add(table_name)

                columns = []
                for col in table.get('columns', []):
                    col_name = _sanitize_column_name(col.get('name', ''))
                    spark_type = _map_to_spark_type(col.get('datatype', 'string'))
                    nullable = col.get('nullable', True)
                    columns.append({
                        'name': col_name,
                        'datatype': spark_type,
                        'nullable': nullable,
                        'original_name': col.get('name', ''),
                        'original_type': col.get('datatype', 'string'),
                    })

                conn_details = table.get('connection_details', {})
                ds_conn = ds.get('connection', {})
                if not ds_conn and ds.get('connections'):
                    ds_conn = ds['connections'][0]
                source_type = conn_details.get('type', ds_conn.get('type', 'Unknown'))

                tables.append({
                    'name': table_name,
                    'original_name': table.get('name', ''),
                    'columns': columns,
                    'source_type': source_type,
                    'source_connection': conn_details.get('details', {}),
                    'format': 'delta',
                })

        # Custom SQL tables
        for sql_entry in custom_sql:
            sql_name = _sanitize_table_name(sql_entry.get('name', 'custom_query'))
            if sql_name not in seen_tables:
                seen_tables.add(sql_name)
                tables.append({
                    'name': sql_name,
                    'original_name': sql_entry.get('name', ''),
                    'columns': [],
                    'source_type': 'Custom SQL',
                    'custom_sql': sql_entry.get('query', ''),
                    'format': 'delta',
                })

        # Calculated columns live in the table the plan chose for them, so the
        # definition matches what the transformations notebook materialises.
        by_name = {table['name']: table for table in tables}
        num_calc_cols = 0
        for planned in plan.materialised:
            table = by_name.get(planned.delta_table)
            if table is None:
                logger.warning(
                    'Calculated column %r targets unknown table %r',
                    planned.caption, planned.delta_table)
                continue
            table['columns'].append({
                'name': planned.delta_name,
                'datatype': planned.spark_type,
                'nullable': True,
                'original_name': planned.caption,
                'original_type': planned.datatype or 'string',
                'is_calculated': True,
                'formula': planned.formula,
            })
            num_calc_cols += 1

        lakehouse_def = {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/lakehouse/definition/lakehouse/1.0.0/schema.json',
            'properties': {
                'displayName': f'{self.project_name}_Lakehouse',
                'description': f'Lakehouse generated from Tableau workbook: {self.project_name}',
                'created': datetime.now().isoformat(),
                'defaultSchema': 'dbo',
            },
            'tables': tables,
        }

        def_path = os.path.join(self.lakehouse_dir, 'lakehouse_definition.json')
        with open(def_path, 'w', encoding='utf-8') as f:
            json.dump(lakehouse_def, f, indent=2, ensure_ascii=False)

        metadata_path = os.path.join(
            self.lakehouse_dir, 'lakehouse.metadata.json')
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump({'defaultSchema': 'dbo'}, f, indent=2)

        self._generate_ddl(tables)
        self._generate_table_metadata(tables, plan)
        write_platform(
            self.lakehouse_dir,
            'Lakehouse',
            f'{self.project_name}_Lakehouse',
            self.item_id,
        )

        total_columns = sum(len(t.get('columns', [])) for t in tables)
        return {'tables': len(tables), 'columns': total_columns,
                'calc_columns': num_calc_cols,
                'calc_columns_pending': len(plan.pending)}

    def _generate_ddl(self, tables):
        """Generate Spark SQL DDL scripts for creating Delta tables."""
        ddl_dir = os.path.join(self.lakehouse_dir, 'ddl')
        os.makedirs(ddl_dir, exist_ok=True)

        all_ddl = []
        for table in tables:
            if not table.get('columns'):
                continue

            ddl = f"-- Create table: {table['name']}\n"
            ddl += f"-- Source: {table.get('original_name', '')} ({table.get('source_type', '')})\n"
            ddl += f"CREATE TABLE IF NOT EXISTS {table['name']} (\n"

            col_defs = []
            last = len(table['columns']) - 1
            for index, col in enumerate(table['columns']):
                nullable = '' if col.get('nullable', True) else ' NOT NULL'
                separator = ',' if index < last else ''
                col_def = f"    {col['name']} {col['datatype']}{nullable}{separator}"
                if col.get('is_calculated'):
                    # The comma must precede the comment, and a one-line comment
                    # cannot contain a newline from a multi-line formula.
                    formula = ' '.join(str(col.get('formula', '')).split())
                    col_def += f'  -- calc: {formula}'
                col_defs.append(col_def)

            ddl += '\n'.join(col_defs)
            ddl += '\n)\nUSING DELTA;\n'
            all_ddl.append(ddl)

            ddl_path = os.path.join(ddl_dir, f'{table["name"]}.sql')
            with open(ddl_path, 'w', encoding='utf-8') as f:
                f.write(ddl)

        if all_ddl:
            combined_path = os.path.join(ddl_dir, '_all_tables.sql')
            with open(combined_path, 'w', encoding='utf-8') as f:
                f.write('-- Lakehouse DDL — Generated by Tableau to Power BI Migration\n')
                f.write('-- Run in a Fabric Notebook or Spark SQL editor\n\n')
                f.write('\n\n'.join(all_ddl))

    def _generate_table_metadata(self, tables, plan=None):
        """Generate table metadata JSON for other generators to consume.

        Calculated columns the notebook does not materialise are listed
        with their reason, per table or under ``unplaced_calc_columns``.
        """
        pending = defaultdict(list)
        unplaced = []
        if plan is not None:
            for column in plan.pending:
                entry = {
                    'name': column.delta_name,
                    'original_name': column.caption,
                    'reason': column.reason,
                }
                if column.delta_table:
                    pending[column.delta_table].append(entry)
                else:
                    unplaced.append(entry)

        meta = {
            'lakehouse_name': f'{self.project_name}_Lakehouse',
            'tables': [
                {
                    'name': t['name'],
                    'original_name': t.get('original_name', ''),
                    'column_count': len(t.get('columns', [])),
                    'source_type': t.get('source_type', 'Unknown'),
                    'format': t.get('format', 'delta'),
                    'pending_calc_columns': pending.get(t['name'], []),
                }
                for t in tables
            ],
            'unplaced_calc_columns': unplaced,
            'calc_measures': len(plan.measures) if plan is not None else 0,
        }
        meta_path = os.path.join(self.lakehouse_dir, 'table_metadata.json')
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
