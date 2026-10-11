import { lstat, readdir, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { MAX_FILE_SIZE_BYTES } from './constants.js';
import { DotdeployerError } from './errors.js';

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

/**
 * Scan generated artifacts that are actually expected to be uploaded, including
 * files ignored by Git before the build. The same hard per-file cap applies.
 */
export async function scanGeneratedOutput(outputDirectory) {
  const root = path.resolve(outputDirectory);
  let rootInfo;
  try {
    rootInfo = await lstat(root);
  } catch (cause) {
    throw new DotdeployerError(`Cannot inspect generated output directory: ${cause.message}`, {
      code: 'BUILD_OUTPUT_CHECK_FAILED',
      details: { path: root },
      cause,
    });
  }
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
    throw new DotdeployerError('Generated output path must be a real directory, not a file or symbolic link.', {
      code: 'BUILD_OUTPUT_NOT_DIRECTORY',
      details: { path: root },
    });
  }

  let fileCount = 0;
  async function visit(directory, relativeDirectory) {
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch (cause) {
      throw new DotdeployerError(`Cannot scan generated output: ${cause.message}`, {
        code: 'BUILD_OUTPUT_SCAN_FAILED',
        details: { path: relativeDirectory || '.' },
        cause,
      });
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const relative = relativeDirectory ? path.join(relativeDirectory, entry.name) : entry.name;
      const absolute = path.join(directory, entry.name);
      const info = await lstat(absolute);
      if (info.isSymbolicLink()) {
        const target = await realpath(absolute).catch((cause) => {
          throw new DotdeployerError(`Generated output contains a dangling symbolic link: ${relative}`, {
            code: 'BUILD_OUTPUT_SYMLINK_INVALID', details: { path: relative }, cause,
          });
        });
        if (!isInside(root, target)) {
          throw new DotdeployerError(`Generated output symlink escapes its output directory: ${relative}`, {
            code: 'BUILD_OUTPUT_SYMLINK_INVALID', details: { path: relative },
          });
        }
        const targetInfo = await stat(target);
        if (targetInfo.isDirectory()) {
          throw new DotdeployerError(`Generated output directory symlinks are not supported: ${relative}`, {
            code: 'BUILD_OUTPUT_SYMLINK_INVALID', details: { path: relative },
          });
        }
        fileCount += 1;
        if (targetInfo.size >= MAX_FILE_SIZE_BYTES) {
          throw new DotdeployerError(`Generated output file ${relative} is ${targetInfo.size.toLocaleString('en-US')} bytes; the per-file limit is ${MAX_FILE_SIZE_BYTES.toLocaleString('en-US')} bytes (20 MB).`, {
            code: 'FILE_TOO_LARGE', details: { path: relative, sizeBytes: targetInfo.size, limitBytes: MAX_FILE_SIZE_BYTES },
          });
        }
        continue;
      }
      if (info.isDirectory()) {
        await visit(absolute, relative);
      } else if (info.isFile()) {
        fileCount += 1;
        if (info.size >= MAX_FILE_SIZE_BYTES) {
          throw new DotdeployerError(`Generated output file ${relative} is ${info.size.toLocaleString('en-US')} bytes; the per-file limit is ${MAX_FILE_SIZE_BYTES.toLocaleString('en-US')} bytes (20 MB).`, {
            code: 'FILE_TOO_LARGE', details: { path: relative, sizeBytes: info.size, limitBytes: MAX_FILE_SIZE_BYTES },
          });
        }
      } else {
        throw new DotdeployerError(`Generated output contains an unsupported filesystem entry: ${relative}`, {
          code: 'BUILD_OUTPUT_FILE_TYPE_UNSUPPORTED', details: { path: relative },
        });
      }
    }
  }

  await visit(root, '');
  return { fileCount };
}
