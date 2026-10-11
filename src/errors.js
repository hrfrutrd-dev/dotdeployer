export class DotdeployerError extends Error {
  constructor(message, { code = 'DOTDEPLOYER_ERROR', details = undefined, cause = undefined } = {}) {
    super(message, { cause });
    this.name = 'DotdeployerError';
    this.code = code;
    this.details = details;
  }
}

export function issue(severity, code, message, file = undefined, remediation = undefined) {
  const result = { severity, code, message };
  if (file) result.file = file;
  if (remediation) result.remediation = remediation;
  return result;
}

export function summarizeIssues(issues = []) {
  return {
    errors: issues.filter((item) => item.severity === 'error').length,
    warnings: issues.filter((item) => item.severity === 'warning').length,
    infos: issues.filter((item) => item.severity === 'info').length,
  };
}
