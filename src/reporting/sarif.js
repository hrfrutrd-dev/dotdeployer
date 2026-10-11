import path from 'node:path';
import { VERSION } from '../core/constants.js';

function sarifLevel(severity) {
  if (severity === 'error') return 'error';
  if (severity === 'warning') return 'warning';
  return 'note';
}

function artifactUri(projectRoot, file) {
  if (!file) return undefined;
  if (!path.isAbsolute(file)) return file.split(path.sep).join('/');
  const relative = path.relative(projectRoot ?? process.cwd(), file);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return undefined;
  return relative.split(path.sep).join('/');
}

/** Convert a check or audit report into SARIF 2.1.0 for code-scanning CI. */
export function reportToSarif(report) {
  const issues = Array.isArray(report?.issues) ? report.issues : [];
  const rules = new Map();
  for (const item of issues) {
    if (!rules.has(item.code)) {
      const rule = {
        id: item.code,
        name: item.code,
        shortDescription: { text: item.message },
        defaultConfiguration: { level: sarifLevel(item.severity) },
      };
      if (item.remediation) rule.help = { text: item.remediation };
      rules.set(item.code, rule);
    }
  }

  const results = issues.map((item) => {
    const result = {
      ruleId: item.code,
      level: sarifLevel(item.severity),
      message: { text: item.message },
    };
    const uri = artifactUri(report.projectRoot, item.file);
    if (uri) {
      const physicalLocation = { artifactLocation: { uri } };
      if (Number.isInteger(item.line) && item.line > 0) {
        physicalLocation.region = { startLine: item.line };
      }
      result.locations = [{ physicalLocation }];
    }
    return result;
  });

  return {
    $schema: 'https://json.schemastore.org/sarif-2.1.0.json',
    version: '2.1.0',
    runs: [{
      tool: {
        driver: {
          name: 'dotdeployer',
          version: report?.version ?? VERSION,
          informationUri: 'https://github.com/hrfrutrd-dev/dotdeployer',
          rules: [...rules.values()],
        },
      },
      results,
    }],
  };
}
