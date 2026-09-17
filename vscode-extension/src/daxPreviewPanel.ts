// Side-by-side DAX conversion preview panel.
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { resolveEngineRoot, runEngine } from './pythonRunner';
import { OverrideManager } from './overrideManager';

export interface DaxConversion {
    name: string;
    tableau_formula: string;
    dax_formula: string;
    status: string; // exact | approximated | unsupported | overridden
    confidence?: number;
    migration_note?: string;
}

const STATUS_COLOR: Record<string, string> = {
    exact: '#2da44e',
    overridden: '#0969da',
    approximated: '#bf8700',
    unsupported: '#cf222e',
};

function escapeHtml(text: string): string {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/** Map a status string to a confidence value. Pure/testable. */
export function statusConfidence(status: string): number {
    switch ((status || '').toLowerCase()) {
        case 'exact':
        case 'overridden':
            return 1.0;
        case 'approximated':
            return 0.5;
        case 'unsupported':
            return 0.0;
        default:
            return 0.5;
    }
}

/** Render the DAX preview HTML. Pure function exposed for testing. */
export function renderDaxPreviewHtml(rows: DaxConversion[]): string {
    if (!rows.length) {
        return '<!DOCTYPE html><html><body><p>No DAX conversions found.</p></body></html>';
    }
    const cards = rows
        .map((r, idx) => {
            const status = (r.status || 'approximated').toLowerCase();
            const color = STATUS_COLOR[status] || '#57606a';
            const note = r.migration_note
                ? `<div class="note">📝 ${escapeHtml(r.migration_note)}</div>`
                : '';
            return `<div class="card">
  <div class="head"><span class="dot" style="background:${color}"></span>
    <span class="name">${escapeHtml(r.name)}</span>
    <span class="status" style="color:${color}">${escapeHtml(status)}</span></div>
  <div class="cols">
    <div class="col"><div class="label">Tableau</div>
      <pre class="tableau">${escapeHtml(r.tableau_formula)}</pre></div>
    <div class="col"><div class="label">DAX</div>
      <textarea class="dax" data-name="${escapeHtml(r.name)}" data-idx="${idx}">${escapeHtml(
                r.dax_formula
            )}</textarea>
      <button class="save" data-name="${escapeHtml(r.name)}">Save override</button></div>
  </div>${note}
</div>`;
        })
        .join('');

    return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><style>
body { font-family: var(--vscode-font-family); padding: 14px; color: var(--vscode-foreground); }
.card { border: 1px solid var(--vscode-panel-border); border-radius: 8px; margin-bottom: 14px; padding: 10px 12px; }
.head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.name { font-weight: 700; }
.status { font-size: 0.78em; text-transform: uppercase; font-weight: 700; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.label { font-size: 0.75em; opacity: 0.7; margin-bottom: 3px; }
pre.tableau { background: var(--vscode-textCodeBlock-background); padding: 8px; border-radius: 5px; white-space: pre-wrap; }
textarea.dax { width: 100%; min-height: 70px; font-family: var(--vscode-editor-font-family); background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 5px; padding: 6px; }
button.save { margin-top: 6px; cursor: pointer; }
.note { font-size: 0.85em; margin-top: 8px; opacity: 0.85; }
</style></head><body>
<h2>DAX Conversion Preview</h2>
${cards}
<script>
const vscode = acquireVsCodeApi();
document.querySelectorAll('button.save').forEach(btn => {
  btn.addEventListener('click', () => {
    const name = btn.getAttribute('data-name');
    const ta = document.querySelector('textarea.dax[data-name="' + CSS.escape(name) + '"]');
    vscode.postMessage({ type: 'saveOverride', name, dax: ta ? ta.value : '' });
  });
});
</script>
</body></html>`;
}

/** Build the python args that emit DAX conversions as JSON. */
export function buildDaxScript(workbookPath: string): string[] {
    const code = [
        'import json,sys,os',
        'sys.path.insert(0, os.getcwd())',
        'sys.path.insert(0, os.path.join(os.getcwd(), "tableau_export"))',
        'from tableau_export.extract_tableau_data import extract_workbook',
        'from tableau_export.dax_converter import convert_calculation',
        `data = extract_workbook(${JSON.stringify(workbookPath)})`,
        'rows = []',
        'for c in data.get("calculations", []):',
        '    formula = c.get("formula", "")',
        '    try:',
        '        dax = convert_calculation(formula)',
        '        status = "exact"',
        '    except Exception as e:',
        '        dax = formula',
        '        status = "unsupported"',
        '    rows.append({"name": c.get("name", "(calc)"), "tableau_formula": formula, "dax_formula": dax, "status": status})',
        'print(json.dumps(rows))',
    ].join('\n');
    return ['-c', code];
}

/** Extract the first top-level JSON array from stdout. Pure/testable. */
export function extractJsonArray(stdout: string): DaxConversion[] | undefined {
    const start = stdout.indexOf('[');
    if (start < 0) {
        return undefined;
    }
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let i = start; i < stdout.length; i++) {
        const ch = stdout[i];
        if (inStr) {
            if (esc) {
                esc = false;
            } else if (ch === '\\') {
                esc = true;
            } else if (ch === '"') {
                inStr = false;
            }
            continue;
        }
        if (ch === '"') {
            inStr = true;
        } else if (ch === '[') {
            depth++;
        } else if (ch === ']') {
            depth--;
            if (depth === 0) {
                try {
                    return JSON.parse(stdout.slice(start, i + 1));
                } catch {
                    return undefined;
                }
            }
        }
    }
    return undefined;
}

export async function previewDaxCommand(
    uri: vscode.Uri | undefined,
    context: vscode.ExtensionContext,
    output: vscode.OutputChannel
): Promise<void> {
    const target = uri ?? vscode.window.activeTextEditor?.document.uri;
    if (!target) {
        vscode.window.showErrorMessage('Select a Tableau .twb/.twbx file.');
        return;
    }
    const workbookPath = target.fsPath;
    const engineRoot = resolveEngineRoot(workbookPath);
    if (!engineRoot) {
        vscode.window.showErrorMessage('Could not locate migrate.py.');
        return;
    }

    const result = await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: 'Converting DAX formulas',
            cancellable: false,
        },
        () => runEngine(engineRoot, buildDaxScript(workbookPath))
    );

    const rows = extractJsonArray(result.stdout) || [];
    if (!rows.length) {
        output.appendLine(result.stderr || 'No DAX conversions produced.');
    }

    const panel = vscode.window.createWebviewPanel(
        'tableauDaxPreview',
        'DAX Preview',
        vscode.ViewColumn.Beside,
        { enableScripts: true }
    );
    panel.webview.html = renderDaxPreviewHtml(rows);

    const overrides = new OverrideManager(
        OverrideManager.configPathFor(engineRoot)
    );
    panel.webview.onDidReceiveMessage((msg) => {
        if (msg?.type === 'saveOverride' && msg.name) {
            overrides.setOverride(msg.name, msg.dax ?? '');
            vscode.window.showInformationMessage(
                `Saved DAX override for "${msg.name}".`
            );
        }
    });
}

// Silence unused-import warning for fs/path in some build configs.
void fs;
void path;
