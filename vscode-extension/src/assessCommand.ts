// Assess command: runs the engine in assessment mode and shows the panel.
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { resolveEngineRoot, getPythonPath, runEngine } from './pythonRunner';
import { AssessmentPanel, AssessmentData } from './assessmentPanel';
import { StatusBar } from './statusBar';

/**
 * Extract a JSON assessment object embedded in engine stdout.
 * The engine may print other log lines; we locate the first balanced
 * top-level JSON object that contains a "categories" key. Pure/testable.
 */
export function extractAssessmentJson(stdout: string): AssessmentData | undefined {
    const start = stdout.indexOf('{');
    if (start < 0) {
        return undefined;
    }
    for (let i = start; i < stdout.length; i++) {
        if (stdout[i] !== '{') {
            continue;
        }
        let depth = 0;
        let inStr = false;
        let esc = false;
        for (let j = i; j < stdout.length; j++) {
            const ch = stdout[j];
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
            } else if (ch === '{') {
                depth++;
            } else if (ch === '}') {
                depth--;
                if (depth === 0) {
                    const candidate = stdout.slice(i, j + 1);
                    try {
                        const parsed = JSON.parse(candidate);
                        if (parsed && typeof parsed === 'object' && 'categories' in parsed) {
                            return parsed as AssessmentData;
                        }
                    } catch {
                        /* keep scanning */
                    }
                    break;
                }
            }
        }
    }
    return undefined;
}

/** Build the python args that emit assessment JSON to stdout. */
export function buildAssessScript(workbookPath: string): string[] {
    // Run extraction + assessment and dump JSON. Kept inline so the extension
    // does not depend on a specific engine output-file location.
    const code = [
        'import json,sys,os',
        'sys.path.insert(0, os.getcwd())',
        'sys.path.insert(0, os.path.join(os.getcwd(), "tableau_export"))',
        'from tableau_export.extract_tableau_data import extract_workbook',
        'from powerbi_import.assessment import run_assessment',
        `data = extract_workbook(${JSON.stringify(workbookPath)})`,
        'name = os.path.splitext(os.path.basename(' +
            JSON.stringify(workbookPath) +
            '))[0]',
        'rep = run_assessment(data, workbook_name=name)',
        'print(json.dumps(rep.to_dict()))',
    ].join('\n');
    return ['-c', code];
}

function findLatestAssessmentFile(root: string): string | undefined {
    const dir = path.join(root, 'artifacts');
    if (!fs.existsSync(dir)) {
        return undefined;
    }
    let best: { p: string; mtime: number } | undefined;
    const walk = (d: string, depth: number) => {
        if (depth > 4) {
            return;
        }
        for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
            const full = path.join(d, entry.name);
            if (entry.isDirectory()) {
                walk(full, depth + 1);
            } else if (entry.name === 'assessment.json') {
                const m = fs.statSync(full).mtimeMs;
                if (!best || m > best.mtime) {
                    best = { p: full, mtime: m };
                }
            }
        }
    };
    walk(dir, 0);
    return best?.p;
}

export async function assessCommand(
    uri: vscode.Uri | undefined,
    output: vscode.OutputChannel,
    status: StatusBar
): Promise<AssessmentData | undefined> {
    const target = uri ?? vscode.window.activeTextEditor?.document.uri;
    if (!target) {
        vscode.window.showErrorMessage('Select a Tableau .twb/.twbx file to assess.');
        return undefined;
    }
    const workbookPath = target.fsPath;
    const engineRoot = resolveEngineRoot(workbookPath);
    if (!engineRoot) {
        vscode.window.showErrorMessage(
            'Could not locate migrate.py. Set "tableauToPowerBI.engineRoot".'
        );
        return undefined;
    }

    status.setState('assessing');
    output.appendLine(`Assessing ${path.basename(workbookPath)}…`);

    const result = await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: 'Assessing Tableau workbook',
            cancellable: false,
        },
        () => runEngine(engineRoot, buildAssessScript(workbookPath))
    );

    let data = extractAssessmentJson(result.stdout);
    if (!data) {
        const file = findLatestAssessmentFile(engineRoot);
        if (file) {
            try {
                data = JSON.parse(fs.readFileSync(file, 'utf-8'));
            } catch {
                /* ignore */
            }
        }
    }

    status.setState('idle');
    if (!data) {
        output.appendLine(result.stderr || 'No assessment produced.');
        vscode.window.showWarningMessage(
            'Assessment did not produce a result. See output for details.'
        );
        return undefined;
    }
    AssessmentPanel.show(data);
    return data;
}

// Re-export for callers that only need the python path.
export { getPythonPath };
