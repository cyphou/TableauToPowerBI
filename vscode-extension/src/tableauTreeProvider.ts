// Tree view that renders the structure of a Tableau workbook from extraction JSON.
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

type NodeKind =
    | 'root'
    | 'section'
    | 'datasource'
    | 'table'
    | 'column'
    | 'worksheet'
    | 'field'
    | 'dashboard'
    | 'parameter';

export class TableauNode extends vscode.TreeItem {
    constructor(
        public readonly label: string,
        public readonly kind: NodeKind,
        collapsibleState: vscode.TreeItemCollapsibleState,
        public readonly children: TableauNode[] = []
    ) {
        super(label, collapsibleState);
        this.contextValue = kind;
        this.iconPath = iconForKind(kind);
    }
}

function iconForKind(kind: NodeKind): vscode.ThemeIcon {
    switch (kind) {
        case 'datasource':
            return new vscode.ThemeIcon('database');
        case 'table':
            return new vscode.ThemeIcon('table');
        case 'column':
            return new vscode.ThemeIcon('symbol-field');
        case 'worksheet':
            return new vscode.ThemeIcon('graph');
        case 'field':
            return new vscode.ThemeIcon('symbol-variable');
        case 'dashboard':
            return new vscode.ThemeIcon('dashboard');
        case 'parameter':
            return new vscode.ThemeIcon('settings-gear');
        case 'section':
            return new vscode.ThemeIcon('folder');
        default:
            return new vscode.ThemeIcon('file');
    }
}

/**
 * Build the tree node hierarchy from an extraction-JSON object.
 * Exposed (pure) for unit testing without VS Code activation.
 */
export function buildTreeFromExtraction(data: any): TableauNode[] {
    const Collapsed = vscode.TreeItemCollapsibleState.Collapsed;
    const None = vscode.TreeItemCollapsibleState.None;
    const sections: TableauNode[] = [];

    const datasources = asArray(data?.datasources);
    if (datasources.length) {
        const dsNodes = datasources.map((ds: any) => {
            const tables = asArray(ds?.tables).map((t: any) => {
                const cols = asArray(t?.columns).map(
                    (c: any) => new TableauNode(nameOf(c), 'column', None)
                );
                return new TableauNode(
                    nameOf(t),
                    'table',
                    cols.length ? Collapsed : None,
                    cols
                );
            });
            return new TableauNode(
                nameOf(ds),
                'datasource',
                tables.length ? Collapsed : None,
                tables
            );
        });
        sections.push(new TableauNode('Datasources', 'section', Collapsed, dsNodes));
    }

    const worksheets = asArray(data?.worksheets);
    if (worksheets.length) {
        const wsNodes = worksheets.map((ws: any) => {
            const fields = asArray(ws?.fields).map(
                (f: any) => new TableauNode(nameOf(f), 'field', None)
            );
            return new TableauNode(
                nameOf(ws),
                'worksheet',
                fields.length ? Collapsed : None,
                fields
            );
        });
        sections.push(new TableauNode('Worksheets', 'section', Collapsed, wsNodes));
    }

    const dashboards = asArray(data?.dashboards);
    if (dashboards.length) {
        const dbNodes = dashboards.map(
            (db: any) => new TableauNode(nameOf(db), 'dashboard', None)
        );
        sections.push(new TableauNode('Dashboards', 'section', Collapsed, dbNodes));
    }

    const parameters = asArray(data?.parameters);
    if (parameters.length) {
        const pNodes = parameters.map(
            (p: any) => new TableauNode(nameOf(p), 'parameter', None)
        );
        sections.push(new TableauNode('Parameters', 'section', Collapsed, pNodes));
    }

    return sections;
}

function asArray(v: any): any[] {
    return Array.isArray(v) ? v : [];
}

function nameOf(obj: any): string {
    if (typeof obj === 'string') {
        return obj;
    }
    return obj?.name || obj?.caption || obj?.label || '(unnamed)';
}

export class TableauTreeProvider implements vscode.TreeDataProvider<TableauNode> {
    private _onDidChangeTreeData = new vscode.EventEmitter<TableauNode | undefined>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;
    private roots: TableauNode[] = [];

    getTreeItem(element: TableauNode): vscode.TreeItem {
        return element;
    }

    getChildren(element?: TableauNode): TableauNode[] {
        return element ? element.children : this.roots;
    }

    setExtraction(data: any): void {
        this.roots = buildTreeFromExtraction(data);
        this._onDidChangeTreeData.fire(undefined);
    }

    /** Load extraction JSON files written by the engine into a directory. */
    loadFromDirectory(dir: string): boolean {
        const merged: any = {};
        const files = [
            'datasources.json',
            'worksheets.json',
            'dashboards.json',
            'parameters.json',
        ];
        let found = false;
        for (const f of files) {
            const p = path.join(dir, f);
            if (fs.existsSync(p)) {
                try {
                    const key = f.replace('.json', '');
                    merged[key] = JSON.parse(fs.readFileSync(p, 'utf-8'));
                    found = true;
                } catch {
                    /* ignore malformed */
                }
            }
        }
        if (found) {
            this.setExtraction(merged);
        }
        return found;
    }

    clear(): void {
        this.roots = [];
        this._onDidChangeTreeData.fire(undefined);
    }
}
