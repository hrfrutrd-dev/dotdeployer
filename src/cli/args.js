import { PROVIDER_ALIASES } from '../core/constants.js';
import { COMMANDS, CONTROL_OPTIONS, OPTION_BY_NAME } from './spec.js';

export class CliUsageError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CliUsageError';
    this.code = 'USAGE_ERROR';
  }
}

function defaults(command) {
  return {
    command,
    projectRoot: '.',
    provider: 'auto',
    plan: 'free',
    strict: false,
    build: false,
    install: false,
    execute: false,
    yes: false,
    json: false,
    sarif: false,
    timeoutSeconds: command === 'verify' ? 60 : 600,
    pathProvided: false,
  };
}

function normalizeProvider(value) {
  const normal = value.toLowerCase();
  return PROVIDER_ALIASES[normal] ?? normal;
}

function requireValue(args, option) {
  const value = args.shift();
  if (value === undefined || value === '' || value.startsWith('--')) {
    throw new CliUsageError(`${option} requires a value.`);
  }
  return value;
}

function setOption(options, definition, args, flag) {
  if (!definition.commands.includes(options.command)) {
    const supported = definition.commands.join(', ');
    throw new CliUsageError(`${flag} is only available with ${supported}.`);
  }

  if (definition.value) {
    const value = requireValue(args, flag);
    switch (definition.key) {
      case 'provider':
        options.provider = normalizeProvider(value);
        break;
      case 'plan':
        if (!value.trim()) throw new CliUsageError('--plan cannot be empty.');
        options.plan = value;
        break;
      case 'timeoutSeconds': {
        const seconds = Number(value);
        if (!Number.isInteger(seconds) || seconds < 1 || seconds > 1800) {
          throw new CliUsageError('--timeout must be an integer between 1 and 1800 seconds.');
        }
        options.timeoutSeconds = seconds;
        break;
      }
      case 'projectRoot':
        options.projectRoot = value;
        options.pathProvided = true;
        break;
      default:
        options[definition.key] = value;
    }
  } else {
    options[definition.key] = true;
  }
}

function helpRequest(args) {
  const topic = args.shift();
  if (topic === undefined || topic === '-h' || topic === '--help') return { help: true };
  if (topic === '-V' || topic === '--version') return { version: true };
  if (topic.startsWith('-')) throw new CliUsageError(`Unknown option: ${topic}`);
  if (!Object.hasOwn(COMMANDS, topic)) {
    throw new CliUsageError(`Unknown help topic: ${topic}.`);
  }
  if (args.some((arg) => !['-h', '--help'].includes(arg))) {
    throw new CliUsageError('help accepts at most one command name.');
  }
  return { command: topic, help: true, helpTopic: topic };
}

function validateOptions(options, positional) {
  const spec = COMMANDS[options.command];
  if (!spec.acceptsPath && positional.length) {
    throw new CliUsageError(`${options.command} does not accept a project path.`);
  }
  if (positional.length > 1) throw new CliUsageError('Only one project path may be supplied.');

  if (positional.length) {
    if (options.pathProvided) throw new CliUsageError('Use either a positional path or --path, not both.');
    if (options.repository) throw new CliUsageError('--repo cannot be combined with a positional project path.');
    options.projectRoot = positional[0];
  }
  if (options.repository && options.pathProvided) throw new CliUsageError('--repo cannot be combined with --path.');
  if (options.ref && !options.repository) throw new CliUsageError('--ref requires --repo.');
  if (options.install && !options.build && ['check', 'test'].includes(options.command)) {
    throw new CliUsageError('--install requires --build, emulate, or deploy.');
  }
  if (options.execute && !options.yes) {
    throw new CliUsageError('A real deploy requires both --execute and --yes.');
  }
  if (options.yes && !options.execute) {
    throw new CliUsageError('--yes requires --execute; without it deploy runs in simulation mode.');
  }
  if (options.json && options.sarif) throw new CliUsageError('Use either --json or --sarif, not both.');
  if (options.sarif && options.build) throw new CliUsageError('--sarif cannot be combined with --build.');
}

/** Parse a dotdeployer command line into a validated command and options. */
export function parseArgs(argv = []) {
  const args = [...argv];
  const rawCommand = args.shift();

  if (rawCommand === undefined) return { ...defaults(undefined), help: true };
  if (rawCommand === '-h' || rawCommand === '--help') return { ...defaults(undefined), help: true };
  if (rawCommand === '-V' || rawCommand === '--version') return { ...defaults(undefined), version: true };
  if (rawCommand === 'help') return { ...defaults('help'), ...helpRequest(args) };

  if (!Object.hasOwn(COMMANDS, rawCommand)) {
    throw new CliUsageError(`Unknown command: ${rawCommand}.`);
  }

  const options = defaults(rawCommand);
  const positional = [];
  while (args.length) {
    const arg = args.shift();
    if (arg === '--') {
      positional.push(...args);
      break;
    }

    const control = CONTROL_OPTIONS.find((definition) => definition.names.includes(arg));
    if (control) {
      options[control.key] = true;
      break;
    }

    const definition = OPTION_BY_NAME.get(arg);
    if (definition) {
      setOption(options, definition, args, arg);
    } else if (arg.startsWith('-')) {
      throw new CliUsageError(`Unknown option: ${arg}`);
    } else {
      positional.push(arg);
    }
  }

  if (!options.help && !options.version) validateOptions(options, positional);
  return options;
}
