import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { checkProject } from '../../src/commands/check.js';
import { copyDeployContext } from '../../src/commands/build.js';
import { scanProjectFiles } from '../../src/project/files.js';

async function tempProject(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-files-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

test('fails closed on a Git LFS pointer whose real object size cannot be checked', async (t) => {
  const root = await tempProject(t);
  await writeFile(path.join(root, 'large-model.bin'), [
    'version https://git-lfs.github.com/spec/v1',
    'oid sha256:0123456789abcdef',
    'size 500000000',
    '',
  ].join('\n'));

  const report = await checkProject({ projectRoot: root, provider: 'generic' });
  assert.equal(report.ok, false);
  assert.equal(report.issues[0].code, 'GIT_LFS_OBJECT_UNVERIFIED');
});

test('rewrites absolute in-project symlinks inside isolated build copies', async (t) => {
  const root = await tempProject(t);
  const destination = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-copy-'));
  t.after(() => rm(destination, { recursive: true, force: true }));
  const sourceFile = path.join(root, 'source.txt');
  await writeFile(sourceFile, 'original');
  await symlink(sourceFile, path.join(root, 'alias.txt'));

  const files = await scanProjectFiles(root);
  await copyDeployContext(root, destination, files);
  const copiedSource = path.join(destination, 'source.txt');
  const copiedAlias = path.join(destination, 'alias.txt');
  await writeFile(copiedSource, 'modified in build');

  const { readFile, readlink } = await import('node:fs/promises');
  assert.equal(await readFile(copiedAlias, 'utf8'), 'modified in build');
  assert.notEqual(await readlink(copiedAlias), sourceFile);
  assert.equal(await readFile(sourceFile, 'utf8'), 'original');
});

test('rejects symbolic links that escape the checked project', async (t) => {
  const root = await tempProject(t);
  const outside = path.join(path.dirname(root), `dotdeployer-outside-${path.basename(root)}`);
  await mkdir(outside, { recursive: true });
  t.after(() => rm(outside, { recursive: true, force: true }));
  await writeFile(path.join(outside, 'secret.txt'), 'secret');
  await symlink(path.join(outside, 'secret.txt'), path.join(root, 'external-secret.txt'));

  const report = await checkProject({ projectRoot: root, provider: 'generic' });
  assert.equal(report.ok, false);
  assert.equal(report.issues[0].code, 'EXTERNAL_SYMLINK');
});
