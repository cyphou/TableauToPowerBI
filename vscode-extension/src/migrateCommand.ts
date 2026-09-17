// One-click migration command: runs migrate.py and opens the generated .pbip.
import * as vscode from 'vscode';
import * as path from 'path';
import {
    resolveEngineRoot,
    getOutputDirectory,
    buildMigrateArgs,
    runEngine,
} from './pythonRunner';
import { StatusBar } from './statusBar';

/** Parse a fidelity percentage from engine stdout, if present. */
export function parseFidelity(stdout: string): number | undefined {
    const m = stdout.match(/fidelity[^\d]*(\d+(?:\.\d+)?)\s*%/i);
    return m ? parseFloat(m[1]) : undefined;
}

/** Parse the generated .pbip output path from engine stdout, if present. */
export function parseOutputPath(stdout: string): string | undefined {
    const m =
        stdout.match(/(?:wrote|generated|output)[^\n]*?([^\s'"]+\.pbip)/i) ||
        stdout.match(/([A-Za-z]:[\\/][^\s'"]+\.pbip)/) ||
        stdout.match(/([^\s'"]+\.pbip)/);
    return m ? m[1] : undefined;
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
    const engineRoot = resolveEngineRoot(workbookPath);
    if (!engineRoot) {
        vscode.window.showErrorMessage(
            'Could not locate migrate.py. Set "tableauToPowerBI.engineRoot" in settings.'
        );
        return;
    }

    const args = buildMigrateArgs(workbookPath, {
        outputDir: getOutputDirectory(),
    });

    output.clear();
    output.show(true);
    output.appendLine(`Migrating ${path.basename(workbookPath)}…`);
    status.setState('migrating', path.basename(workbookPath));

    const result = await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: 'Migrating Tableau workbook to Power BI',
            cancellable: false,
        },
        () =>
            runEngine(engineRoot, args, (chunk) => {
                output.append(chunk);
            })
    );

    if (result.code === 0) {
        status.setState('done');
        const fidelity = parseFidelity(result.stdout);
        if (fidelity !== undefined) {
            status.setFidelity(fidelity);
        }
        const outPath = parseOutputPath(result.stdout);
        output.appendLine('\n✔ Migration complete.');
        const choice = await vscode.window.showInformationMessage(
            'Migration complete.',
            'Open Output Folder'
        );
        if (choice === 'Open Output Folder' && outPath) {
            const folder = path.dirname(
                path.isAbsolute(outPath) ? outPath : path.join(engineRoot, outPath)
            );
            vscode.commands.executeCommand(
                'revealFileInOS',
                vscode.Uri.file(folder)
            );
        }
    } else {
        status.setState('error');
        output.appendLine(`\n✖ Migration failed (exit ${result.code}).`);
        if (result.stderr) {
            output.appendLine(result.stderr);
        }
        vscode.window.showErrorMessage(
            'Migration failed. See the Tableau→Power BI output channel.'
        );
    }
}
