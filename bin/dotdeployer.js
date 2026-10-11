#!/usr/bin/env node
import { main } from '../src/cli/index.js';

main()
  .then((exitCode) => {
    process.exitCode = Number.isInteger(exitCode) ? exitCode : 0;
  })
  .catch((error) => {
    const code = error?.code ?? 'UNEXPECTED_ERROR';
    process.stderr.write(`dotdeployer: ${code}: ${error?.message ?? String(error)}\n`);
    process.exitCode = 2;
  });
