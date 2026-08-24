// Manages DAX overrides persisted to the engine's config.json.
import * as fs from 'fs';
import * as path from 'path';

export interface OverrideConfig {
    dax_overrides?: Record<string, string>;
    [key: string]: unknown;
}

/**
 * Apply or clear a single DAX override within a config object.
 * Pure function — exposed for testing. Returns a new config object.
 */
export function applyOverride(
    config: OverrideConfig,
    measureName: string,
    dax: string | null
): OverrideConfig {
    const next: OverrideConfig = { ...config };
    const overrides: Record<string, string> = { ...(next.dax_overrides || {}) };
    if (dax === null) {
        delete overrides[measureName];
    } else {
        overrides[measureName] = dax;
    }
    next.dax_overrides = overrides;
    return next;
}

export class OverrideManager {
    constructor(private readonly configPath: string) {}

    /** Compute the config.json path next to a workbook or in the engine root. */
    static configPathFor(engineRoot: string): string {
        return path.join(engineRoot, 'config.json');
    }

    read(): OverrideConfig {
        if (!fs.existsSync(this.configPath)) {
            return {};
        }
        try {
            return JSON.parse(fs.readFileSync(this.configPath, 'utf-8'));
        } catch {
            return {};
        }
    }

    write(config: OverrideConfig): void {
        fs.writeFileSync(
            this.configPath,
            JSON.stringify(config, null, 2),
            'utf-8'
        );
    }

    /** Save an override (or clear it when dax is null). */
    setOverride(measureName: string, dax: string | null): OverrideConfig {
        const updated = applyOverride(this.read(), measureName, dax);
        this.write(updated);
        return updated;
    }

    getOverride(measureName: string): string | undefined {
        return this.read().dax_overrides?.[measureName];
    }

    listOverrides(): Record<string, string> {
        return this.read().dax_overrides || {};
    }
}
