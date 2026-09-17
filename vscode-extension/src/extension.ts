// Extension entry point: registers commands, tree view, and status bar.
import * as vscode from 'vscode';
import { TableauTreeProvider } from './tableauTreeProvider';
import { StatusBar } from './statusBar';
import { migrateCommand } from './migrateCommand';
import { assessCommand } from './assessCommand';
import { previewDaxCommand } from './daxPreviewPanel';

export function activate(context: vscode.ExtensionContext): void {
    const output = vscode.window.createOutputChannel('Tableau → Power BI');
    const status = new StatusBar();
    const tree = new TableauTreeProvider();

    const treeView = vscode.window.registerTreeDataProvider(
        'tableauToPowerBI.workbookTree',
        tree
    );

    context.subscriptions.push(
        output,
        status,
        treeView,
        vscode.commands.registerCommand(
            'tableauToPowerBI.assess',
            (uri?: vscode.Uri) => assessCommand(uri, output, status)
        ),
        vscode.commands.registerCommand(
            'tableauToPowerBI.migrate',
            (uri?: vscode.Uri) => migrateCommand(uri, output, status)
        ),
        vscode.commands.registerCommand(
            'tableauToPowerBI.previewDax',
            (uri?: vscode.Uri) => previewDaxCommand(uri, context, output)
        ),
        vscode.commands.registerCommand('tableauToPowerBI.refreshTree', () => {
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                const dir = require('path').dirname(editor.document.uri.fsPath);
                tree.loadFromDirectory(dir);
            }
        })
    );
}

export function deactivate(): void {
    /* no-op */
}
