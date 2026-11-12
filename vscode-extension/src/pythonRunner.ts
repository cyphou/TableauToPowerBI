// Helper for locating the migration engine and running it as a subprocess.
import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

export interface EngineResult {
    code: number;
    stdout: string;
    stderr: string;
    outputDir?: string;
}

/** Resolve the configured Python interpreter path. */
export function getPythonPath(): string {
    const cfg = vscode.workspace.getConfiguration('tableauToPowerBI');
    return cfg.get<string>('pythonPath') || 'python';
}

/**
 * Locate the repository root that contains `migrate.py`.
 * Honours the `engineRoot` setting, then walks up from the given file,
 * then falls back to workspace folders.
 */
export function resolveEngineRoot(startPath?: string): string | undefined {
    const cfg = vscode.workspace.getConfiguration('tableauToPowerBI');
    const configured = cfg.get<string>('engineRoot');
    if (configured && fs.existsSync(path.join(configured, 'migrate.py'))) {
        return configured;
    }

    const candidates: string[] = [];
    if (startPath) {
        candidates.push(path.dirname(startPath));
    }
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
        candidates.push(folder.uri.fsPath);
    }

    for (const start of candidates) {
        let dir = start;
        for (let i = 0; i < 8; i++) {
            if (fs.existsSync(path.join(dir, 'migrate.py'))) {
                return dir;
            }
            const parent = path.dirname(dir);
            if (parent === dir) {
                break;
            }
            dir = parent;
        }
    }
    return undefined;
}

/** Resolve the output directory setting (may be empty → engine default). */
export function getOutputDirectory(): string | undefined {
    const cfg = vscode.workspace.getConfiguration('tableauToPowerBI');
    const out = cfg.get<string>('outputDirectory');
    return out && out.trim().length > 0 ? out : undefined;
}

export async function runSingleWorkbookMigration(
    engineRoot: string,
    workbookPath: string,
    opts: { outputDir?: string; dryRun?: boolean } = {},
    onData?: (chunk: string) => void
): Promise<EngineResult> {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'ttpbi-batch-config-'));
    try {
        const outputRoot = path.resolve(engineRoot, opts.outputDir || 'artifacts/powerbi_projects/migrated');
        fs.mkdirSync(outputRoot, { recursive: true });
        const outputDir = fs.mkdtempSync(path.join(outputRoot, 'migration-'));
        const configPath = path.join(temporary, 'batch.json');
        fs.writeFileSync(configPath, JSON.stringify([{ file: path.resolve(workbookPath) }]),
            { encoding: 'utf8', mode: 0o600 });
        const args = ['migrate.py', '--batch-config', configPath, '--output-dir', outputDir];
        if (opts.dryRun) {
            args.push('--dry-run');
        }
        return { ...await runEngine(engineRoot, args, onData), outputDir };
    } finally {
        fs.rmSync(temporary, { recursive: true, force: true });
    }
}

/**
 * Run the migration engine as a subprocess.
 * `onData` receives streamed stdout chunks for progress reporting.
 */
export function runEngine(
    engineRoot: string,
    args: string[],
    onData?: (chunk: string) => void
): Promise<EngineResult> {
    return new Promise((resolve) => {
        const python = getPythonPath();
        const child = cp.spawn(python, args, {
            cwd: engineRoot, env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        });
        let stdout = '';
        let stderr = '';

        child.stdout.setEncoding('utf8');
        child.stderr.setEncoding('utf8');
        child.stdout.on('data', (text: string) => {
            stdout += text;
            onData?.(text);
        });
        child.stderr.on('data', (text: string) => {
            stderr += text;
        });
        child.on('close', (code) => {
            resolve({ code: code ?? -1, stdout, stderr });
        });
        child.on('error', (err) => {
            resolve({ code: -1, stdout, stderr: stderr + String(err) });
        });
    });
}
