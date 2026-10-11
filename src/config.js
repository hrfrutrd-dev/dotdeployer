import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { parseDocument } from 'yaml';
import { parse as parseJsonc, printParseErrorCode } from 'jsonc-parser';
import { parse as parseToml } from 'smol-toml';
import { issue } from './errors.js';

export function parseJsoncStrict(text, filePath, issues, { jsonc = false } = {}) {
  if (!jsonc) {
    try {
      return JSON.parse(text);
    } catch (error) {
      issues.push(issue('error', 'CONFIG_PARSE_ERROR', `Invalid JSON: ${error.message}`, filePath,
        'Fix the JSON syntax and rerun dotdeployer check.'));
      return undefined;
    }
  }

  const parseErrors = [];
  const value = parseJsonc(text, parseErrors, { allowTrailingComma: true, disallowComments: false });
  if (parseErrors.length) {
    const first = parseErrors[0];
    issues.push(issue('error', 'CONFIG_PARSE_ERROR', `Invalid JSONC: ${printParseErrorCode(first.error)} at offset ${first.offset}.`, filePath,
      'Fix the JSONC syntax and rerun dotdeployer check.'));
    return undefined;
  }
  return value;
}

export async function parseConfigFile(projectRoot, filePath, issues, format = undefined) {
  const absolute = path.resolve(projectRoot, filePath);
  let text;
  try {
    text = await readFile(absolute, 'utf8');
  } catch (error) {
    issues.push(issue('error', 'CONFIG_READ_ERROR', `Cannot read configuration: ${error.message}`, filePath));
    return undefined;
  }

  const extension = format ?? path.extname(filePath).toLowerCase();
  try {
    if (extension === '.yaml' || extension === '.yml') {
      const document = parseDocument(text, { uniqueKeys: true, prettyErrors: false, strict: true });
      if (document.errors.length) {
        for (const error of document.errors.slice(0, 3)) {
          issues.push(issue('error', 'CONFIG_PARSE_ERROR', error.message, filePath));
        }
        return undefined;
      }
      return document.toJS({ maxAliasCount: 100 });
    }
    if (extension === '.toml') {
      return parseToml(text);
    }
    return parseJsoncStrict(text, filePath, issues, { jsonc: extension === '.jsonc' });
  } catch (error) {
    issues.push(issue('error', 'CONFIG_PARSE_ERROR', `Invalid ${extension.slice(1).toUpperCase()} configuration: ${error.message}`, filePath,
      'Check the configuration syntax against the hosting provider documentation.'));
    return undefined;
  }
}

export function assertObjectConfig(value, filePath, issues, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    issues.push(issue('error', 'CONFIG_SHAPE_INVALID', `${label} configuration must be an object.`, filePath));
    return false;
  }
  return true;
}
