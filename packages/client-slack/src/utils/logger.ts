/* eslint-disable @typescript-eslint/no-explicit-any */
import { LoggerOptions, pino } from 'pino';
import { Logger as SlackLogger, LogLevel } from '@slack/bolt';

const loggers: Map<string, Logger> = new Map();

export class Logger implements SlackLogger {
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
  public setLevel(level: LogLevel): void {
    this.logger.level = Object.values(LogLevel).includes(level) ? level : 'error';
  }

  /**
   * Map the pino log level to Slack LogLevel
   */
  public getLevel(): LogLevel {
    const levelMapping: { [key: string]: LogLevel } = {
      fatal: LogLevel.ERROR,
      error: LogLevel.ERROR,
      warn: LogLevel.WARN,
      info: LogLevel.INFO,
      debug: LogLevel.DEBUG,
      trace: LogLevel.DEBUG,
    };
    return levelMapping[this.logger.level];
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
      logger.setLevel((process.env.LOG_LEVEL as LogLevel) || 'debug');
      loggers.set(name || 'default', logger);
    }
    return loggers.get(name || 'default')!;
  }
}
