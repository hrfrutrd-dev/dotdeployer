import { execFileSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DotdeployerError } from './errors.js';

function normalizeGithubRepository(repository) {
  const value = String(repository ?? '').trim();
  const shorthand = /^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/.exec(value);
  if (shorthand) return `https://github.com/${shorthand[1]}/${shorthand[2].replace(/\.git$/, '')}.git`;

  const https = /^https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(value);
  if (https) return `https://github.com/${https[1]}/${https[2]}.git`;

  const ssh = /^git@github\.com:([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?$/.exec(value);
  if (ssh) return `git@github.com:${ssh[1]}/${ssh[2]}.git`;

  throw new DotdeployerError('Repository must be a GitHub URL, git@github.com SSH URL, or owner/repository shorthand.', {
    code: 'GITHUB_REPOSITORY_INVALID',
  });
}

function validateRef(ref) {
  if (ref === undefined || ref === null || ref === '') return undefined;
  const value = String(ref);
  // Ref is passed as one argv value (never through a shell); reject option-like
  // and malformed refs anyway so errors stay clear and predictable.
  if (value.startsWith('-') || value.includes('..') || value.includes('@{') || /[\s\0~^:?*\[\\]/.test(value)) {
    throw new DotdeployerError(`Invalid Git ref: ${value}`, { code: 'GIT_REF_INVALID' });
  }
  return value;
}

export async function withProjectSource({ projectRoot = process.cwd(), repository, ref }, callback) {
  if (!repository) return callback(path.resolve(projectRoot));

  const url = normalizeGithubRepository(repository);
  const branch = validateRef(ref);
  const tempParent = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-source-'));
  const checkout = path.join(tempParent, 'repository');
  try {
    const args = ['clone', '--depth', '1'];
    if (branch) args.push('--branch', branch);
    args.push('--', url, checkout);
    try {
      execFileSync('git', args, {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        maxBuffer: 16 * 1024 * 1024,
        timeout: 10 * 60 * 1000,
        env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
      });
    } catch (cause) {
      const stderr = cause?.stderr?.toString?.('utf8')?.trim();
      throw new DotdeployerError(`Could not clone ${url}${branch ? ` at ${branch}` : ''}.${stderr ? ` ${stderr}` : ''}`, {
        code: 'GIT_CLONE_FAILED',
        cause,
        details: { repository: url, ref: branch },
      });
    }
    return await callback(checkout);
  } finally {
    await rm(tempParent, { recursive: true, force: true });
  }
}
