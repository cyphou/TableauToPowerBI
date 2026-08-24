// Minimal stand-in for the `vscode` module so pure functions can be unit-tested
// without launching the Extension Host.
/* eslint-disable @typescript-eslint/no-explicit-any */

class TreeItem {
    label: any;
    collapsibleState: any;
    contextValue: any;
    iconPath: any;
    constructor(label: any, collapsibleState: any) {
        this.label = label;
        this.collapsibleState = collapsibleState;
    }
}

class ThemeIcon {
    id: string;
    constructor(id: string) {
        this.id = id;
    }
}

class EventEmitter {
    event = (): { dispose(): void } => ({ dispose() {} });
    fire(): void {}
    dispose(): void {}
}

const noopDisposable = { dispose() {} };

const stub: any = {
    TreeItem,
    ThemeIcon,
    EventEmitter,
    TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
    StatusBarAlignment: { Left: 1, Right: 2 },
    ViewColumn: { One: 1, Beside: 2 },
    ProgressLocation: { Notification: 15, Window: 10 },
    Uri: {
        file: (p: string) => ({ fsPath: p, path: p, scheme: 'file' }),
    },
    workspace: {
        getConfiguration: () => ({ get: (_k: string, d?: any) => d }),
        workspaceFolders: [] as any[],
    },
    window: {
        activeTextEditor: undefined,
        createStatusBarItem: () => ({
            text: '',
            tooltip: '',
            command: undefined,
            show() {},
            hide() {},
            dispose() {},
        }),
        createOutputChannel: () => ({
            append() {},
            appendLine() {},
            clear() {},
            show() {},
            dispose() {},
        }),
        showInformationMessage: async () => undefined,
        showWarningMessage: async () => undefined,
        showErrorMessage: async () => undefined,
        withProgress: async (_o: any, task: any) => task({ report() {} }),
        createWebviewPanel: () => ({
            webview: {
                html: '',
                onDidReceiveMessage: () => noopDisposable,
                postMessage() {},
            },
            onDidDispose: () => noopDisposable,
            reveal() {},
            dispose() {},
        }),
        registerTreeDataProvider: () => noopDisposable,
    },
    commands: {
        registerCommand: () => noopDisposable,
        executeCommand: async () => undefined,
    },
};

export = stub;
