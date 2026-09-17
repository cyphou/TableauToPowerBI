// Lightweight unit-test runner: installs the `vscode` stub via a require hook,
// then runs the compiled mocha test files. No Extension Host needed.
/* eslint-disable @typescript-eslint/no-var-requires */
import * as path from 'path';
import * as fs from 'fs';

// Install the vscode stub before any test module (which transitively requires
// 'vscode') is loaded.
const Module = require('module');
const vscodeStub = require('./vscode-mock');
const originalLoad = Module._load;
Module._load = function (request: string, parent: unknown, isMain: boolean): unknown {
    if (request === 'vscode') {
        return vscodeStub;
    }
    return originalLoad.call(this, request, parent, isMain);
};

const Mocha = require('mocha');
const mocha = new Mocha({ ui: 'bdd', color: true, timeout: 10000 });

const suiteDir = path.join(__dirname, 'suite');
for (const file of fs.readdirSync(suiteDir)) {
    if (file.endsWith('.test.js')) {
        mocha.addFile(path.join(suiteDir, file));
    }
}

mocha.run((failures: number) => {
    process.exit(failures > 0 ? 1 : 0);
});
