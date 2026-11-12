import * as assert from 'assert';
import * as cp from 'child_process';
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';

import { resolveEngineRoot, runSingleWorkbookMigration } from '../../pythonRunner';
import { buildTreeFromExtraction } from '../../tableauTreeProvider';
import { renderAssessmentHtml } from '../../assessmentPanel';
import { classifyMigrationResult, parseFidelity, parseOutputPath } from '../../migrateCommand';
import { StatusBar } from '../../statusBar';
import { assessCommand, extractAssessmentJson, buildAssessScript } from '../../assessCommand';
import { applyOverride } from '../../overrideManager';
import {
    renderDaxPreviewHtml,
    statusConfidence,
    buildDaxScript,
    extractJsonArray,
} from '../../daxPreviewPanel';

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

describe('single workbook migration', () => {
    it('migrates only the selected source through batch-config', async function () {
        this.timeout(30000);
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-extension-été-'));
        const oldConfig = vscode.workspace.getConfiguration;
        try {
            const workbook = path.join(directory, 'Selected workbook.twb');
            const other = path.join(directory, 'Other workbook.twb');
            const sample = path.join(root, 'examples', 'tableau_samples', 'Superstore_Sales.twb');
            fs.copyFileSync(sample, workbook);
            fs.copyFileSync(sample, other);
            const output = path.join(directory, 'output');
            const previous = path.join(output, 'Selected workbook', 'Selected workbook.pbip');
            fs.mkdirSync(path.dirname(previous), { recursive: true });
            fs.writeFileSync(previous, 'previous output');
            (vscode.workspace as any).getConfiguration = () => ({ get: (name: string) =>
                name === 'pythonPath' ? python : undefined });
            const result = await runSingleWorkbookMigration(root, workbook, { outputDir: output });
            assert.strictEqual(result.code, 0, result.stderr + result.stdout);
            assert.ok(result.outputDir);
            const project = path.join(result.outputDir!, 'Selected workbook', 'Selected workbook.pbip');
            assert.ok(fs.existsSync(project));
            assert.deepStrictEqual(classifyMigrationResult(root, result), {
                status: 'complete', projectPath: project,
            });
            assert.strictEqual(fs.readFileSync(previous, 'utf8'), 'previous output');
            assert.ok(!fs.existsSync(path.join(result.outputDir!, 'Other workbook')));
            assert.ok(fs.existsSync(workbook) && fs.existsSync(other));
        } finally {
            (vscode.workspace as any).getConfiguration = oldConfig;
            fs.rmSync(directory, { recursive: true, force: true });
        }
    });
    it('identifies a retained partial project without calling it a success', async function () {
        this.timeout(30000);
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-extension-partial-'));
        const oldConfig = vscode.workspace.getConfiguration;
        try {
            const workbook = path.join(directory, 'Partial workbook.twb');
            fs.copyFileSync(path.join(root, 'examples', 'tableau_samples', 'BigQuery_Analytics.twb'), workbook);
            const output = path.join(directory, 'output');
            (vscode.workspace as any).getConfiguration = () => ({ get: (name: string) =>
                name === 'pythonPath' ? python : undefined });
            const result = await runSingleWorkbookMigration(root, workbook, { outputDir: output });
            assert.notStrictEqual(result.code, 0);
            assert.deepStrictEqual(classifyMigrationResult(root, result), {
                status: 'partial', projectPath: path.join(result.outputDir!, 'Partial workbook', 'Partial workbook.pbip'),
            });
        } finally {
            (vscode.workspace as any).getConfiguration = oldConfig;
            fs.rmSync(directory, { recursive: true, force: true });
        }
    });
    it('does not report an old project when the selected source is missing', async function () {
        this.timeout(30000);
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-extension-missing-'));
        const oldConfig = vscode.workspace.getConfiguration;
        try {
            const output = path.join(directory, 'output');
            const previous = path.join(output, 'Missing', 'Missing.pbip');
            fs.mkdirSync(path.dirname(previous), { recursive: true });
            fs.writeFileSync(previous, 'previous output');
            (vscode.workspace as any).getConfiguration = () => ({ get: (name: string) =>
                name === 'pythonPath' ? python : undefined });
            const result = await runSingleWorkbookMigration(root, path.join(directory, 'Missing.twb'), { outputDir: output });
            assert.notStrictEqual(result.code, 0);
            assert.deepStrictEqual(classifyMigrationResult(root, result), { status: 'failed' });
            assert.strictEqual(fs.readFileSync(previous, 'utf8'), 'previous output');
        } finally {
            (vscode.workspace as any).getConfiguration = oldConfig;
            fs.rmSync(directory, { recursive: true, force: true });
        }
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
        const p = parseOutputPath('  .pbip file created: C:/out/MyWb.pbip');
        assert.ok(p && p.endsWith('MyWb.pbip'));
    });
    it('parses generated paths containing spaces', () => {
        assert.strictEqual(parseOutputPath('  ✓ .pbip file created: C:\\My Output\\Report One.pbip\n'),
                           'C:\\My Output\\Report One.pbip');
    });
    it('returns undefined when no path', () => {
        assert.strictEqual(parseOutputPath('no output'), undefined);
    });
    it('does not trust a project path outside the current run', () => {
        const result = {
            code: 7, stdout: '  .pbip file created: C:/old/project.pbip\n', stderr: '',
            outputDir: path.resolve('current-run'),
        };
        assert.deepStrictEqual(classifyMigrationResult(process.cwd(), result), { status: 'failed' });
    });
    it('does not claim success when no current PBIP can be identified', () => {
        assert.deepStrictEqual(classifyMigrationResult(process.cwd(), {
            code: 0, stdout: '', stderr: '', outputDir: path.resolve('current-run'),
        }), { status: 'failed' });
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
        assert.strictEqual(args[2], 'C:/data/wb.twbx');
        assert.ok(args[1].includes('run_assessment'));
    });
    it('executes assessment against the current engine', () => {
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const workbook = path.join(root, 'examples', 'tableau_samples', 'Superstore_Sales.twb');
        const result = cp.spawnSync(python, buildAssessScript(workbook), {
            cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 30000,
        });
        assert.strictEqual(result.status, 0, result.stderr);
        const assessment = extractAssessmentJson(result.stdout);
        assert.ok(assessment);
        assert.ok(Array.isArray(assessment!.categories) && assessment!.categories!.length > 0);
        assert.ok(['GREEN', 'YELLOW', 'RED'].includes(String(assessment!.overall_score)));
        const html = renderAssessmentHtml(assessment!);
        assert.ok(html.includes(String(assessment!.overall_score)));
        assert.ok(!html.includes('[object Object]'));
    });
    it('handles quoted workbook paths and rejects missing workbooks', () => {
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-extension-'));
        try {
            const workbook = path.join(directory, "source é's.twb");
            fs.copyFileSync(path.join(root, 'examples', 'tableau_samples', 'Superstore_Sales.twb'), workbook);
            for (const buildScript of [buildAssessScript, buildDaxScript]) {
                const options = { cwd: root, encoding: 'utf8' as const,
                    env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 30000 };
                const success = cp.spawnSync(python, buildScript(workbook), options);
                assert.strictEqual(success.status, 0, success.stderr);
                const missing = cp.spawnSync(python, buildScript(path.join(directory, 'missing.twb')), options);
                assert.notStrictEqual(missing.status, 0);
                assert.strictEqual(extractAssessmentJson(missing.stdout), undefined);
                assert.strictEqual(extractJsonArray(missing.stdout), undefined);
            }
        } finally {
            fs.rmSync(directory, { recursive: true, force: true });
        }
    });
    it('does not display an old assessment after extraction fails', async () => {
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-stale-'));
        const oldConfig = vscode.workspace.getConfiguration;
        const oldWarning = vscode.window.showWarningMessage;
        const localPython = path.join(path.resolve(__dirname, '../../../..'), '.venv',
            process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        let warnings = 0;
        try {
            fs.writeFileSync(path.join(directory, 'migrate.py'), '');
            const oldDir = path.join(directory, 'artifacts', 'old');
            fs.mkdirSync(oldDir, { recursive: true });
            fs.writeFileSync(path.join(oldDir, 'assessment.json'), JSON.stringify({
                workbook_name: 'Other workbook', categories: [], overall_score: 'GREEN',
            }));
            (vscode.workspace as any).getConfiguration = () => ({ get: (name: string) =>
                name === 'engineRoot' ? directory : name === 'pythonPath' ? python : undefined });
            (vscode.window as any).showWarningMessage = async () => { warnings++; return undefined; };
            const output = vscode.window.createOutputChannel('test');
            const status = new StatusBar();
            const result = await assessCommand(vscode.Uri.file(path.join(directory, 'missing.twb')), output, status);
            status.dispose();
            assert.strictEqual(result, undefined);
            assert.strictEqual(warnings, 1);
        } finally {
            (vscode.workspace as any).getConfiguration = oldConfig;
            (vscode.window as any).showWarningMessage = oldWarning;
            fs.rmSync(directory, { recursive: true, force: true });
        }
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
        assert.strictEqual(args[2], 'wb.twbx');
        assert.ok(args[1].includes('convert_tableau_formula_to_dax'));
    });
    it('executes DAX preview using the current engine', () => {
        const root = path.resolve(__dirname, '../../../..');
        const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        const python = process.env.TTPBI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
        const workbook = path.join(root, 'examples', 'tableau_samples', 'Superstore_Sales.twb');
        const result = cp.spawnSync(python, buildDaxScript(workbook), {
            cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 30000,
        });
        assert.strictEqual(result.status, 0, result.stderr);
        const rows = extractJsonArray(result.stdout);
        assert.ok(rows && rows.length > 0);
        assert.ok(rows!.every(row => ['needs_review', 'unsupported'].includes(row.status)));
        assert.ok(rows!.some(row => row.dax_formula && row.tableau_formula));
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
