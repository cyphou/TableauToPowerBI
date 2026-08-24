// Helper for locating the migration engine and running it as a subprocess.
import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

export interface EngineResult {
    code: number;
    stdout: string;
    stderr: string;
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

/**
 * Build the argument vector for `migrate.py`.
 * Exposed for testing.
 */
export function buildMigrateArgs(
    workbookPath: string,
    opts: { assess?: boolean; dryRun?: boolean; outputDir?: string } = {}
): string[] {
    const args = ['migrate.py', workbookPath];
    if (opts.assess) {
        args.push('--assess');
    }
    if (opts.dryRun) {
        args.push('--dry-run');
    }
    if (opts.outputDir) {
        args.push('--output-dir', opts.outputDir);
    }
    return args;
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
        const child = cp.spawn(python, args, { cwd: engineRoot });
        let stdout = '';
        let stderr = '';

        child.stdout.on('data', (d: Buffer) => {
            const text = d.toString();
            stdout += text;
            onData?.(text);
        });
        child.stderr.on('data', (d: Buffer) => {
            stderr += d.toString();
        });
        child.on('close', (code) => {
            resolve({ code: code ?? -1, stdout, stderr });
        });
        child.on('error', (err) => {
            resolve({ code: -1, stdout, stderr: stderr + String(err) });
        });
    });
}
