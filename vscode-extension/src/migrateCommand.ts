// One-click migration command for a single selected workbook.
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import {
    resolveEngineRoot,
    getOutputDirectory,
    runSingleWorkbookMigration,
    EngineResult,
} from './pythonRunner';
import { StatusBar } from './statusBar';

/** Parse a fidelity percentage from engine stdout, if present. */
export function parseFidelity(stdout: string): number | undefined {
    const m = stdout.match(/fidelity[^\d]*(\d+(?:\.\d+)?)\s*%/i);
    return m ? parseFloat(m[1]) : undefined;
}

/** Parse the generated .pbip output path from engine stdout, if present. */
export function parseOutputPath(stdout: string): string | undefined {
    const matches = [...stdout.matchAll(/^[^\r\n]*?\.pbip file created:[ \t]*(.+?\.pbip)[ \t]*$/gim)];
    return matches.length ? matches[matches.length - 1][1] : undefined;
}

export function classifyMigrationResult(engineRoot: string, result: EngineResult): {
    status: 'complete' | 'partial' | 'failed'; projectPath?: string;
} {
    const reported = parseOutputPath(result.stdout);
    let projectPath = reported ? path.resolve(engineRoot, reported) : undefined;
    if (projectPath && result.outputDir) {
        const relative = path.relative(result.outputDir, projectPath);
        if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
            projectPath = undefined;
        }
    }
    if (result.code === 0) {
        return projectPath && fs.existsSync(projectPath)
            ? { status: 'complete', projectPath } : { status: 'failed' };
    }
    if (projectPath && fs.existsSync(projectPath)) {
        try {
            const directory = path.dirname(projectPath);
            const partial = JSON.parse(fs.readFileSync(path.join(directory, 'partial_generation.json'), 'utf8'));
            const failure = JSON.parse(fs.readFileSync(path.join(directory, 'migration_failure.json'), 'utf8'));
            if (partial.status === 'partial' && partial.validation_status === 'FAIL' &&
                failure.completion_marker === 'diagnostic_only') {
                return { status: 'partial', projectPath };
            }
        } catch { }
    }
    return { status: 'failed' };
}

export async function migrateCommand(
    uri: vscode.Uri | undefined,
    output: vscode.OutputChannel,
    status: StatusBar
): Promise<void> {
    const target = uri ?? vscode.window.activeTextEditor?.document.uri;
    if (!target) {
        vscode.window.showErrorMessage(
            'Open or select a Tableau .twb/.twbx file to migrate.'
        );
        return;
    }
    const workbookPath = target.fsPath;
    if (!/\.twbx?$/i.test(workbookPath)) {
        vscode.window.showErrorMessage('Select a Tableau .twb or .twbx workbook to migrate.');
        return;
    }
    const engineRoot = resolveEngineRoot(workbookPath);
    if (!engineRoot) {
        vscode.window.showErrorMessage(
            'Could not locate migrate.py. Set "tableauToPowerBI.engineRoot" in settings.'
        );
        return;
    }

    output.clear();
    output.show(true);
    output.appendLine(`Migrating ${path.basename(workbookPath)}…`);
    status.setState('migrating', path.basename(workbookPath));

    let result: EngineResult;
    try {
        result = await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Migrating Tableau workbook to Power BI',
                cancellable: false,
            },
            () => runSingleWorkbookMigration(engineRoot, workbookPath,
                { outputDir: getOutputDirectory() }, (chunk) => output.append(chunk))
        );
    } catch (error) {
        status.setState('error');
        output.appendLine(`\nMigration could not start: ${String(error)}`);
        vscode.window.showErrorMessage('Migration could not start. See the Tableau→Power BI output channel.');
        return;
    }

    const outcome = classifyMigrationResult(engineRoot, result);
    if (outcome.status === 'complete') {
        status.setState('done');
        const fidelity = parseFidelity(result.stdout);
        if (fidelity !== undefined) {
            status.setFidelity(fidelity);
        }
        output.appendLine('\n✔ Migration complete.');
        const outputFolder = outcome.projectPath ? path.dirname(outcome.projectPath) : result.outputDir;
        const choice = await vscode.window.showInformationMessage(
            'Migration generation complete. Desktop opening, refresh and visual parity have not been verified.',
            ...(outputFolder ? ['Open Output Folder'] : [])
        );
        if (choice === 'Open Output Folder' && outputFolder) {
            vscode.commands.executeCommand(
                'revealFileInOS',
                vscode.Uri.file(outputFolder)
            );
        }
    } else if (outcome.status === 'partial') {
        status.setState('error');
        output.appendLine(`\nPartial diagnostic project retained (exit ${result.code}); validation failed.`);
        const choice = await vscode.window.showWarningMessage(
            'A partial diagnostic project was generated. It is not validated or ready for deployment.',
            'Open Output Folder'
        );
        if (choice === 'Open Output Folder' && outcome.projectPath) {
            vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(path.dirname(outcome.projectPath)));
        }
    } else {
        status.setState('error');
        output.appendLine(result.code === 0
            ? '\n✖ Migration did not produce an identifiable current-run PBIP.'
            : `\n✖ Migration failed (exit ${result.code}).`);
        if (result.stderr) {
            output.appendLine(result.stderr);
        }
        const choice = await vscode.window.showErrorMessage(
            'Migration failed. See the Tableau→Power BI output channel and diagnostic files.',
            ...(result.outputDir ? ['Open Output Folder'] : [])
        );
        if (choice === 'Open Output Folder' && result.outputDir) {
            vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(result.outputDir));
        }
    }
}
