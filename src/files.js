import { execFileSync } from 'node:child_process';
import { lstat, readFile, readdir, readlink, realpath, stat as statPath } from 'node:fs/promises';
import path from 'node:path';
import { MAX_FILE_SIZE_BYTES, IGNORED_DIRS_WITHOUT_GIT } from './constants.js';
import { DotdeployerError } from './errors.js';

function runGit(args, cwd) {
  try {
    return execFileSync('git', ['-C', cwd, ...args], {
      encoding: 'buffer',
      stdio: ['ignore', 'pipe', 'ignore'],
      maxBuffer: 64 * 1024 * 1024,
      timeout: 15_000,
    });
  } catch {
    return null;
  }
}

function splitNullDelimited(buffer) {
  return buffer.toString('utf8').split('\0').filter(Boolean);
}

async function getGitFiles(root) {
  const top = runGit(['rev-parse', '--show-toplevel'], root);
  if (!top) return null;

  const gitRoot = path.resolve(top.toString('utf8').trim());
  const rootRelativeToGit = path.relative(gitRoot, root);
  if (rootRelativeToGit.startsWith('..') || path.isAbsolute(rootRelativeToGit)) {
    throw new DotdeployerError('Project path is outside its Git work tree.', {
      code: 'PROJECT_OUTSIDE_GIT_WORKTREE',
      details: { root, gitRoot },
    });
  }

  // Include committed files and non-ignored untracked files. This approximates
  // what a Git-based deploy can see while respecting .gitignore.
  const output = runGit(['ls-files', '--full-name', '--cached', '--others', '--exclude-standard', '-z'], root);
  if (!output) return null;

  const prefix = rootRelativeToGit ? `${rootRelativeToGit.split(path.sep).join('/')}/` : '';
  const entries = new Set();
  for (const repoPath of splitNullDelimited(output)) {
    if (prefix && !repoPath.startsWith(prefix)) continue;
    const relative = prefix ? repoPath.slice(prefix.length) : repoPath;
    if (relative) entries.add(relative.split('/').join(path.sep));
  }
  return [...entries].sort();
}

async function walkWithoutGit(root) {
  const files = [];
  async function visit(directory, relDirectory) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const rel = relDirectory ? path.join(relDirectory, entry.name) : entry.name;
      if (entry.isDirectory() && IGNORED_DIRS_WITHOUT_GIT.has(entry.name)) continue;
      if (entry.isDirectory()) {
        await visit(path.join(directory, entry.name), rel);
      } else {
        files.push(rel);
      }
    }
  }
  await visit(root, '');
  return files;
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

/**
 * Inventory deployable source files and enforce the hard per-file limit.
 * This must be the first content-sensitive phase of preflight.
 */
export async function scanProjectFiles(projectRoot) {
  const root = path.resolve(projectRoot);
  let rootStat;
  try {
    rootStat = await lstat(root);
  } catch (cause) {
    const missing = cause?.code === 'ENOENT' || cause?.code === 'ENOTDIR';
    throw new DotdeployerError(missing ? `Project directory does not exist: ${root}` : `Cannot access project directory ${root}: ${cause.message}`, {
      code: missing ? 'PROJECT_NOT_FOUND' : 'PROJECT_ACCESS_FAILED',
      details: { path: root },
      cause,
    });
  }
  if (!rootStat.isDirectory()) {
    throw new DotdeployerError(`Project path is not a directory: ${root}`, {
      code: 'PROJECT_NOT_DIRECTORY',
      details: { path: root },
    });
  }

  let listed;
  try {
    listed = await getGitFiles(root) ?? await walkWithoutGit(root);
  } catch (cause) {
    if (cause instanceof DotdeployerError) throw cause;
    throw new DotdeployerError(`Cannot enumerate deployable files: ${cause.message}`, {
      code: 'FILE_SCAN_FAILED',
      details: { path: root },
      cause,
    });
  }
  const listedSet = new Set(listed);
  const files = [];
  for (const rel of listed) {
    const absolute = path.resolve(root, rel);
    if (!isInside(root, absolute)) {
      throw new DotdeployerError(`File path escapes the project directory: ${rel}`, {
        code: 'PATH_ESCAPE',
        details: { path: rel },
      });
    }

    let stat;
    try {
      stat = await lstat(absolute);
    } catch (error) {
      if (error?.code === 'ENOENT') continue; // deleted index entry
      throw new DotdeployerError(`Cannot inspect project file ${rel}: ${error.message}`, {
        code: 'FILE_SCAN_FAILED',
        details: { path: rel },
        cause: error,
      });
    }

    if (stat.isSymbolicLink()) {
      const linkTarget = await readlink(absolute);
      const resolvedTarget = path.resolve(path.dirname(absolute), linkTarget);
      if (!isInside(root, resolvedTarget)) {
        throw new DotdeployerError(`Symbolic link points outside the project: ${rel} -> ${linkTarget}`, {
          code: 'EXTERNAL_SYMLINK',
          details: { path: rel, target: linkTarget },
        });
      }
      // Ensure a dangling or non-deployed target cannot make local builds differ.
      try {
        const realTarget = await realpath(absolute);
        if (!isInside(root, realTarget)) {
          throw new DotdeployerError(`Symbolic link resolves outside the project: ${rel}`, {
            code: 'EXTERNAL_SYMLINK',
            details: { path: rel, target: linkTarget },
          });
        }
        const targetStat = await statPath(realTarget);
        if (targetStat.isDirectory()) {
          throw new DotdeployerError(`Directory symbolic links are not supported by the deploy-context checker: ${rel}`, {
            code: 'SYMLINK_DIRECTORY_UNSUPPORTED',
            details: { path: rel, target: linkTarget },
          });
        }
        const targetRel = path.relative(root, realTarget);
        if (!listedSet.has(targetRel)) {
          throw new DotdeployerError(`Symbolic link target is not included in the Git deployment context: ${rel} -> ${targetRel}`, {
            code: 'SYMLINK_TARGET_NOT_DEPLOYED',
            details: { path: rel, target: targetRel },
          });
        }
        if (targetStat.size >= MAX_FILE_SIZE_BYTES) {
          throw new DotdeployerError(`Preflight stopped: symlink target ${targetRel} is ${targetStat.size.toLocaleString('en-US')} bytes; the per-file limit is ${MAX_FILE_SIZE_BYTES.toLocaleString('en-US')} bytes (20 MB).`, {
            code: 'FILE_TOO_LARGE',
            details: { path: targetRel, sizeBytes: targetStat.size, limitBytes: MAX_FILE_SIZE_BYTES, filesScanned: files.length },
          });
        }
      } catch (error) {
        if (error instanceof DotdeployerError) throw error;
        throw new DotdeployerError(`Dangling symbolic link is not supported: ${rel}`, {
          code: 'DANGLING_SYMLINK',
          details: { path: rel, target: linkTarget },
          cause: error,
        });
      }
    }

    if (stat.isDirectory()) {
      throw new DotdeployerError(`Git submodule or directory entry is not supported as a deployable file: ${rel}`, {
        code: 'GIT_SUBMODULE_UNSUPPORTED',
        details: { path: rel },
      });
    }
    if (!stat.isFile() && !stat.isSymbolicLink()) {
      throw new DotdeployerError(`Unsupported special filesystem entry: ${rel}`, {
        code: 'UNSUPPORTED_FILE_TYPE',
        details: { path: rel },
      });
    }

    const size = stat.size;
    if (size >= MAX_FILE_SIZE_BYTES) {
      const actual = `${size.toLocaleString('en-US')} bytes`;
      const limit = `${MAX_FILE_SIZE_BYTES.toLocaleString('en-US')} bytes`;
      throw new DotdeployerError(
        `Preflight stopped: ${rel} is ${actual}; the per-file limit is ${limit} (20 MB).`,
        {
          code: 'FILE_TOO_LARGE',
          details: { path: rel, sizeBytes: size, limitBytes: MAX_FILE_SIZE_BYTES, filesScanned: files.length },
        },
      );
    }

    if (stat.isFile() && size <= 256) {
      const prefix = await readFile(absolute, 'utf8').catch(() => '');
      if (prefix.startsWith('version https://git-lfs.github.com/spec/v1')) {
        throw new DotdeployerError(`Git LFS pointer found at ${rel}; the actual LFS object size is not available in this local deployment context.`, {
          code: 'GIT_LFS_OBJECT_UNVERIFIED',
          details: { path: rel, pointerBytes: size },
        });
      }
    }

    if (stat.isFile() || stat.isSymbolicLink()) {
      files.push({ path: rel.split(path.sep).join('/'), absolute, sizeBytes: size });
    }
  }
  return files;
}
