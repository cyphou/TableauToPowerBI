import * as assert from 'assert';
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';

import { buildMigrateArgs, resolveEngineRoot } from '../../pythonRunner';
import { buildTreeFromExtraction } from '../../tableauTreeProvider';
import { renderAssessmentHtml } from '../../assessmentPanel';
import { parseFidelity, parseOutputPath } from '../../migrateCommand';
import { StatusBar } from '../../statusBar';
import { extractAssessmentJson, buildAssessScript } from '../../assessCommand';
import { applyOverride } from '../../overrideManager';
import {
    renderDaxPreviewHtml,
    statusConfidence,
    buildDaxScript,
    extractJsonArray,
} from '../../daxPreviewPanel';

describe('pythonRunner.buildMigrateArgs', () => {
    it('builds base args', () => {
        assert.deepStrictEqual(buildMigrateArgs('wb.twbx'), [
            'migrate.py',
            'wb.twbx',
        ]);
    });
    it('adds --assess', () => {
        assert.ok(buildMigrateArgs('wb.twbx', { assess: true }).includes('--assess'));
    });
    it('adds --dry-run', () => {
        assert.ok(buildMigrateArgs('wb.twbx', { dryRun: true }).includes('--dry-run'));
    });
    it('adds --output-dir with value', () => {
        const args = buildMigrateArgs('wb.twbx', { outputDir: '/tmp/out' });
        const i = args.indexOf('--output-dir');
        assert.ok(i >= 0);
        assert.strictEqual(args[i + 1], '/tmp/out');
    });
    it('combines all flags', () => {
        const args = buildMigrateArgs('wb.twbx', {
            assess: true,
            dryRun: true,
            outputDir: 'o',
        });
        assert.ok(args.includes('--assess'));
        assert.ok(args.includes('--dry-run'));
        assert.ok(args.includes('--output-dir'));
    });
});

describe('pythonRunner.resolveEngineRoot', () => {
    it('walks up to find migrate.py', () => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'engine-'));
        fs.writeFileSync(path.join(root, 'migrate.py'), '# stub');
        const nested = path.join(root, 'a', 'b');
        fs.mkdirSync(nested, { recursive: true });
        const found = resolveEngineRoot(path.join(nested, 'wb.twbx'));
        assert.strictEqual(found && fs.existsSync(path.join(found, 'migrate.py')), true);
    });
    it('returns undefined when no engine present', () => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noengine-'));
        assert.strictEqual(resolveEngineRoot(path.join(root, 'wb.twbx')), undefined);
    });
});

describe('tableauTreeProvider.buildTreeFromExtraction', () => {
    const data = {
        datasources: [
            { name: 'Sales', tables: [{ name: 'Orders', columns: [{ name: 'Amount' }] }] },
        ],
        worksheets: [{ name: 'Sheet1', fields: [{ name: 'Region' }] }],
        dashboards: [{ name: 'Dash1' }],
        parameters: [{ name: 'Year' }],
    };
    it('creates four sections', () => {
        const tree = buildTreeFromExtraction(data);
        const labels = tree.map((n) => n.label);
        assert.deepStrictEqual(labels, [
            'Datasources',
            'Worksheets',
            'Dashboards',
            'Parameters',
        ]);
    });
    it('nests tables and columns under datasource', () => {
        const tree = buildTreeFromExtraction(data);
        const ds = tree[0].children[0];
        assert.strictEqual(ds.label, 'Sales');
        assert.strictEqual(ds.children[0].label, 'Orders');
        assert.strictEqual(ds.children[0].children[0].label, 'Amount');
    });
    it('omits empty sections', () => {
        const tree = buildTreeFromExtraction({ worksheets: [{ name: 'S' }] });
        assert.strictEqual(tree.length, 1);
        assert.strictEqual(tree[0].label, 'Worksheets');
    });
    it('handles non-array input safely', () => {
        assert.deepStrictEqual(buildTreeFromExtraction(null), []);
        assert.deepStrictEqual(buildTreeFromExtraction({}), []);
    });
});

describe('assessmentPanel.renderAssessmentHtml', () => {
    const data = {
        workbook_name: 'My <Book>',
        overall_score: 87,
        summary: 'Looks good',
        categories: [
            {
                name: 'Datasource',
                worst_severity: 'warn',
                checks: [
                    { name: 'Live connection', severity: 'warn', detail: 'd & d' },
                ],
            },
        ],
    };
    it('includes the score', () => {
        assert.ok(renderAssessmentHtml(data).includes('87'));
    });
    it('escapes HTML in names', () => {
        const html = renderAssessmentHtml(data);
        assert.ok(html.includes('My &lt;Book&gt;'));
        assert.ok(!html.includes('My <Book>'));
    });
    it('renders severity badges', () => {
        assert.ok(renderAssessmentHtml(data).toUpperCase().includes('WARN'));
    });
    it('does not throw on empty categories', () => {
        assert.doesNotThrow(() => renderAssessmentHtml({ categories: [] }));
    });
});

describe('migrateCommand parsers', () => {
    it('parses fidelity percentage', () => {
        assert.strictEqual(parseFidelity('Final fidelity: 92.5%'), 92.5);
    });
    it('returns undefined when no fidelity', () => {
        assert.strictEqual(parseFidelity('nothing here'), undefined);
    });
    it('parses .pbip output path', () => {
        const p = parseOutputPath('Generated C:/out/MyWb.pbip done');
        assert.ok(p && p.endsWith('MyWb.pbip'));
    });
    it('returns undefined when no path', () => {
        assert.strictEqual(parseOutputPath('no output'), undefined);
    });
});

describe('StatusBar static renderers', () => {
    it('renders idle', () => {
        assert.ok(StatusBar.render('idle').includes('Tableau'));
    });
    it('renders migrating with detail', () => {
        assert.ok(StatusBar.render('migrating', 'wb.twbx').includes('wb.twbx'));
    });
    it('renders done', () => {
        assert.ok(StatusBar.render('done').toLowerCase().includes('complete'));
    });
    it('renders error', () => {
        assert.ok(StatusBar.render('error').toLowerCase().includes('failed'));
    });
    it('tooltip varies by state', () => {
        assert.notStrictEqual(
            StatusBar.tooltip('assessing'),
            StatusBar.tooltip('done')
        );
    });
});

describe('assessCommand.extractAssessmentJson', () => {
    it('extracts JSON with categories from noisy stdout', () => {
        const out =
            'INFO loading\n{"overall_score":80,"categories":[]}\nDONE';
        const data = extractAssessmentJson(out);
        assert.ok(data);
        assert.strictEqual(data!.overall_score, 80);
    });
    it('ignores JSON without categories', () => {
        assert.strictEqual(extractAssessmentJson('{"foo":1}'), undefined);
    });
    it('returns undefined when no JSON', () => {
        assert.strictEqual(extractAssessmentJson('plain text'), undefined);
    });
    it('buildAssessScript references the workbook path', () => {
        const args = buildAssessScript('C:/data/wb.twbx');
        assert.strictEqual(args[0], '-c');
        assert.ok(args[1].includes('wb.twbx'));
        assert.ok(args[1].includes('run_assessment'));
    });
});

describe('overrideManager.applyOverride', () => {
    it('adds an override', () => {
        const cfg = applyOverride({}, 'Sales', 'SUM(x)');
        assert.strictEqual(cfg.dax_overrides!['Sales'], 'SUM(x)');
    });
    it('clears an override with null', () => {
        const cfg = applyOverride(
            { dax_overrides: { Sales: 'x' } },
            'Sales',
            null
        );
        assert.strictEqual(cfg.dax_overrides!['Sales'], undefined);
    });
    it('does not mutate the input', () => {
        const input: { dax_overrides: Record<string, string> } = {
            dax_overrides: { A: '1' },
        };
        applyOverride(input, 'B', '2');
        assert.strictEqual(input.dax_overrides['B'], undefined);
    });
});

describe('daxPreviewPanel helpers', () => {
    it('statusConfidence maps exact to 1.0', () => {
        assert.strictEqual(statusConfidence('exact'), 1.0);
    });
    it('statusConfidence maps unsupported to 0.0', () => {
        assert.strictEqual(statusConfidence('unsupported'), 0.0);
    });
    it('renderDaxPreviewHtml escapes formulas', () => {
        const html = renderDaxPreviewHtml([
            {
                name: 'M',
                tableau_formula: 'IF [x] > 1',
                dax_formula: 'IF([x]>1,1)',
                status: 'exact',
            },
        ]);
        assert.ok(html.includes('&gt;'));
        assert.ok(html.includes('exact'));
    });
    it('renderDaxPreviewHtml handles empty list', () => {
        assert.ok(renderDaxPreviewHtml([]).includes('No DAX'));
    });
    it('buildDaxScript references the workbook', () => {
        const args = buildDaxScript('wb.twbx');
        assert.strictEqual(args[0], '-c');
        assert.ok(args[1].includes('convert_calculation'));
    });
    it('extractJsonArray parses an array from stdout', () => {
        const rows = extractJsonArray('log\n[{"name":"a"}]\nend');
        assert.ok(rows);
        assert.strictEqual(rows!.length, 1);
    });
    it('extractJsonArray returns undefined for no array', () => {
        assert.strictEqual(extractJsonArray('nothing'), undefined);
    });
});
