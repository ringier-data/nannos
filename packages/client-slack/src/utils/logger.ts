/* eslint-disable @typescript-eslint/no-explicit-any */
import { LoggerOptions, pino } from 'pino';
import { Logger as SlackLogger, LogLevel } from '@slack/bolt';

type PinoLevel = 'trace' | 'debug' | 'info' | 'warn' | 'error' | 'fatal';

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
    this.logger.trace(...msg);
  }

  public debug(...msg: any[]): void {
    this.logger.debug(...msg);
  }

  public info(...msg: any[]): void {
    this.logger.info(...msg);
  }

  public warn(...msg: any[]): void {
    this.logger.warn(...msg);
  }

  public error(...msg: any[]): void {
    this.logger.error(...msg);
  }

  public setLevel(level: PinoLevel | string): void {
    this.logger.level = level;
  }

  public getLevel(): string {
    return this.logger.level;
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
      logger.setLevel(process.env.LOG_LEVEL || 'debug');
      loggers.set(name || 'default', logger);
    }
    return loggers.get(name || 'default')!;
  }
}

/**
 * Slack-compatible logger that wraps a pino Logger
 * and maps between Slack LogLevel and pino log levels.
 */
export class SlackBoltLogger implements SlackLogger {
  private readonly pinoLogger: Logger;

  constructor(name: string) {
    this.pinoLogger = Logger.getLogger(name);
  }

  debug(...msg: any[]): void {
    this.pinoLogger.debug(...msg);
  }

  info(...msg: any[]): void {
    this.pinoLogger.info(...msg);
  }

  warn(...msg: any[]): void {
    this.pinoLogger.warn(...msg);
  }

  error(...msg: any[]): void {
    this.pinoLogger.error(...msg);
  }

  setLevel(level: LogLevel): void {
    this.pinoLogger.setLevel(level);
  }

  getLevel(): LogLevel {
    const levelMapping: Record<string, LogLevel> = {
      fatal: LogLevel.ERROR,
      error: LogLevel.ERROR,
      warn: LogLevel.WARN,
      info: LogLevel.INFO,
      debug: LogLevel.DEBUG,
      trace: LogLevel.DEBUG,
    };
    return levelMapping[this.pinoLogger.getLevel()] ?? LogLevel.DEBUG;
  }

  setName(name: string): void {
    this.pinoLogger.setName(name);
  }
}
