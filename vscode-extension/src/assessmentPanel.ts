// Webview panel that renders the 9-category migration assessment.
import * as vscode from 'vscode';

export interface AssessmentCheck {
    name: string;
    severity: string;
    detail?: string;
    recommendation?: string;
}

export interface AssessmentCategory {
    name: string;
    worst_severity?: string;
    checks?: AssessmentCheck[];
}

export interface AssessmentData {
    workbook_name?: string;
    overall_score?: number;
    summary?: string;
    strategy?: string;
    categories?: AssessmentCategory[];
}

const SEVERITY_COLOR: Record<string, string> = {
    pass: '#2da44e',
    info: '#0969da',
    warn: '#bf8700',
    fail: '#cf222e',
};

const SEVERITY_LABEL: Record<string, string> = {
    pass: 'PASS',
    info: 'INFO',
    warn: 'WARN',
    fail: 'FAIL',
};

function escapeHtml(text: string): string {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function badge(severity: string): string {
    const sev = (severity || 'pass').toLowerCase();
    const color = SEVERITY_COLOR[sev] || '#57606a';
    const label = SEVERITY_LABEL[sev] || sev.toUpperCase();
    return `<span class="badge" style="background:${color}">${label}</span>`;
}

/**
 * Render the assessment HTML body. Pure function exposed for testing.
 */
export function renderAssessmentHtml(data: AssessmentData): string {
    const title = escapeHtml(data.workbook_name || 'Workbook');
    const score =
        typeof data.overall_score === 'number'
            ? `${Math.round(data.overall_score)}%`
            : 'n/a';
    const strategy = data.strategy
        ? `<p class="strategy">Recommended strategy: <strong>${escapeHtml(
              data.strategy
          )}</strong></p>`
        : '';

    const categories = (data.categories || [])
        .map((cat) => {
            const checks = (cat.checks || [])
                .map((chk) => {
                    const detail = chk.detail
                        ? `<div class="detail">${escapeHtml(chk.detail)}</div>`
                        : '';
                    const rec = chk.recommendation
                        ? `<div class="rec">💡 ${escapeHtml(chk.recommendation)}</div>`
                        : '';
                    return `<li>${badge(chk.severity)} <span class="check-name">${escapeHtml(
                        chk.name
                    )}</span>${detail}${rec}</li>`;
                })
                .join('');
            const worst = cat.worst_severity || 'pass';
            return `<details class="category"><summary>${badge(
                worst
            )} ${escapeHtml(cat.name)}</summary><ul>${checks}</ul></details>`;
        })
        .join('');

    return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<style>
body { font-family: var(--vscode-font-family); padding: 16px; color: var(--vscode-foreground); }
h1 { font-size: 1.3em; margin-bottom: 4px; }
.score { font-size: 2em; font-weight: bold; }
.summary { opacity: 0.85; }
.strategy { margin: 8px 0 16px; }
.badge { color: #fff; padding: 1px 7px; border-radius: 9px; font-size: 0.72em; font-weight: 700; }
.category { margin: 6px 0; border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 6px 10px; }
.category summary { cursor: pointer; font-weight: 600; }
.category ul { list-style: none; padding-left: 4px; }
.category li { margin: 6px 0; }
.check-name { font-weight: 500; }
.detail { font-size: 0.85em; opacity: 0.8; margin: 2px 0 0 38px; }
.rec { font-size: 0.85em; margin: 2px 0 0 38px; }
</style></head>
<body>
<h1>Migration Assessment — ${title}</h1>
<div class="score">${score}</div>
<p class="summary">${escapeHtml(data.summary || '')}</p>
${strategy}
${categories || '<p>No assessment categories available.</p>'}
</body></html>`;
}

export class AssessmentPanel {
    private static current: AssessmentPanel | undefined;
    private readonly panel: vscode.WebviewPanel;

    private constructor(panel: vscode.WebviewPanel) {
        this.panel = panel;
        this.panel.onDidDispose(() => {
            AssessmentPanel.current = undefined;
        });
    }

    static show(data: AssessmentData): AssessmentPanel {
        const column = vscode.window.activeTextEditor?.viewColumn;
        if (AssessmentPanel.current) {
            AssessmentPanel.current.update(data);
            AssessmentPanel.current.panel.reveal(column);
            return AssessmentPanel.current;
        }
        const panel = vscode.window.createWebviewPanel(
            'tableauAssessment',
            'Tableau Assessment',
            column ?? vscode.ViewColumn.One,
            { enableScripts: false }
        );
        AssessmentPanel.current = new AssessmentPanel(panel);
        AssessmentPanel.current.update(data);
        return AssessmentPanel.current;
    }

    update(data: AssessmentData): void {
        this.panel.title = `Assessment — ${data.workbook_name || 'Workbook'}`;
        this.panel.webview.html = renderAssessmentHtml(data);
    }
}
