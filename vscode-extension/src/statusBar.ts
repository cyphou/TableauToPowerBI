// Status bar item showing migration state and fidelity score.
import * as vscode from 'vscode';

export type MigrationState = 'idle' | 'assessing' | 'migrating' | 'done' | 'error';

export class StatusBar {
    private readonly item: vscode.StatusBarItem;

    constructor() {
        this.item = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Left,
            100
        );
        this.item.command = 'tableauToPowerBI.migrate';
        this.setState('idle');
        this.item.show();
    }

    setState(state: MigrationState, detail?: string): void {
        this.item.text = StatusBar.render(state, detail);
        this.item.tooltip = StatusBar.tooltip(state, detail);
    }

    setFidelity(score: number): void {
        this.item.text = `$(check) Tableau→PBI ${Math.round(score)}%`;
        this.item.tooltip = `Last migration fidelity: ${Math.round(score)}%`;
    }

    /** Pure renderer for the status text (testable). */
    static render(state: MigrationState, detail?: string): string {
        switch (state) {
            case 'assessing':
                return '$(sync~spin) Assessing…';
            case 'migrating':
                return `$(sync~spin) Migrating${detail ? ' ' + detail : '…'}`;
            case 'done':
                return '$(check) Migration complete';
            case 'error':
                return '$(error) Migration failed';
            default:
                return '$(table) Tableau→PBI';
        }
    }

    static tooltip(state: MigrationState, detail?: string): string {
        switch (state) {
            case 'assessing':
                return 'Assessing Tableau workbook readiness…';
            case 'migrating':
                return detail
                    ? `Migrating ${detail}`
                    : 'Migrating to Power BI…';
            case 'done':
                return 'Migration completed. Click to migrate another workbook.';
            case 'error':
                return 'Migration failed — see the output channel for details.';
            default:
                return 'Tableau to Power BI migration. Click to migrate.';
        }
    }

    dispose(): void {
        this.item.dispose();
    }
}
