import { COMMANDS, CONTROL_OPTIONS, OPTION_DEFINITIONS } from './spec.js';

function optionLabel(definition) {
  return `${definition.names.join(', ')}${definition.value ? ` ${definition.value}` : ''}`;
}

function formatOptionLines(definitions) {
  const entries = definitions.map((definition) => [optionLabel(definition), definition.description]);
  const width = Math.max(24, ...entries.map(([label]) => label.length));
  return entries.map(([label, description]) => `  ${label.padEnd(width)}  ${description}`).join('\n');
}

export function globalHelp(version) {
  const commandLines = Object.entries(COMMANDS)
    .map(([name, spec]) => `  ${name.padEnd(12)} ${spec.description}`)
    .join('\n');
  const controlLines = CONTROL_OPTIONS.map((definition) => ({
    names: definition.names,
    description: definition.description,
  }));

  return `dotdeployer ${version} — strict local deployment preflight

Usage:
  dotdeployer <command> [path] [options]
  dotdeployer <command> --help
  dotdeployer help [command]

Commands:
${commandLines}

Global options:
${formatOptionLines(controlLines)}

Run "dotdeployer <command> --help" for command-specific options and usage.
Examples:
  dotdeployer check . --provider vercel
  dotdeployer audit . --provider vercel --strict --json
  dotdeployer test . --provider vercel --build
  dotdeployer deploy . --provider cloudflare --environment staging

A successful check is a conservative local preflight, not a guarantee of cloud behavior.`;
}

export function commandHelp(command, version) {
  const spec = COMMANDS[command];
  if (!spec) return globalHelp(version);

  const definitions = OPTION_DEFINITIONS.filter((definition) => spec.options.includes(definition.key));
  const controls = CONTROL_OPTIONS.map((definition) => ({
    names: definition.names,
    description: definition.description,
  }));
  const options = [...definitions, ...controls];

  const notes = command === 'audit'
    ? `
Secret values are never printed. Audit does not execute project code.`
    : command === 'deploy'
      ? `
Simulation is the default. A real deploy requires both --execute and --yes and is blocked for production.`
      : command === 'check'
        ? `
The per-file size guard runs before config parsing; build outputs are rescanned when --build is used.`
        : '';
  const positionalPathNote = spec.acceptsPath
    ? `

The optional positional [path] is equivalent to --path; do not supply both or combine either with --repo.`
    : '';

  return `dotdeployer ${version}

${spec.usage}

${spec.description}

Options:
${formatOptionLines(options)}${positionalPathNote}${notes}`;
}
