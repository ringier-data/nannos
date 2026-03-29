/* eslint-disable @typescript-eslint/no-explicit-any */
import { LoggerOptions, pino, Level } from 'pino';

const loggers: Map<string, Logger> = new Map();

export class Logger {
  private readonly logger;
  private name?: string = undefined;

  // this class must not have a public constructor. this ensures that the logger is a singleton
  // NOTE: this an anti-pattern to use protected instead of private. with protected we could have a
  // chance to call the constructor from a subclass just for testing purposes.
  protected constructor(options?: LoggerOptions, ...args: any[]) {
    if (!options) {
      options = {};
    }
    options.formatters = {
      level: (label) => ({ level: label === 'warn' ? 'WARNING' : label.toUpperCase() }),
      bindings: () => ({}), // omit `pid` and `hostname` from all log records. these are useless for ECS
    };
    options.timestamp = pino.stdTimeFunctions.isoTime;
    options.messageKey = 'message';
    options.mixin = (mergeObject: any) => {
      mergeObject.name = this.name;
      return mergeObject;
    };
    ((options.transport =
      process.env.ENVIRONMENT === 'local'
        ? {
            target: 'pino-pretty',
            options: { colorize: true },
          }
        : undefined),
      (this.logger = pino(options, ...args)));
    this.logger.useLevelLabels = true;
  }

  public trace(...msg: any[]): void {
    if (Array.isArray(msg)) {
      msg = msg[0];
    }
    this.logger.trace(msg);
  }

  public debug(...msg: any[]): void {
    if (Array.isArray(msg)) {
      msg = msg[0];
    }
    this.logger.debug(msg);
  }

  public info(...msg: any[]): void {
    if (Array.isArray(msg)) {
      msg = msg[0];
    }
    this.logger.info(msg);
  }

  public warn(...msg: any[]): void {
    if (Array.isArray(msg)) {
      msg = msg[0];
    }
    this.logger.warn(msg);
  }

  public error(...msg: any[]): void {
    if (Array.isArray(msg)) {
      msg = msg[0];
    }
    this.logger.error(msg);
  }

  /**
   * Map the Slack LogLevel to pino log level
   */
  public setLevel(level: Level): void {
    this.logger.level = level;
  }

  public setName(name: string): void {
    this.name = name;
  }

  public static getLogger(name?: string, ...args: any[]): Logger {
    if (!loggers.get(name || 'default')) {
      const logger = new Logger(...args);
      if (name) {
        logger.setName(name);
      }
      logger.setLevel((process.env.LOG_LEVEL as Level) || 'debug');
      loggers.set(name || 'default', logger);
    }
    return loggers.get(name || 'default')!;
  }
}
