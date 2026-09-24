import type {
  IContextStore,
  IPendingRequestStore,
  IInFlightTaskStore,
  IUserAuthStorage,
  IScheduledRunStore,
} from '../storage/types.js';

import { A2AClientService } from "../services/a2aClientService.js";
import { FeedbackService } from "../services/feedbackService.js";
import { FileStorageService } from "../services/fileStorageService.js";
import { GoogleChatService } from "../services/googleChatService.js";
import type { IUserAuthService } from "../services/userAuthService.js";
import { Config } from '../config/config.js';
import type { ScheduledRunResumeService } from '../services/scheduledRunResumeService.js';

export interface HandlerDependencies {
  userAuthService: IUserAuthService;
  a2aClientService: A2AClientService;
  chatService: GoogleChatService;
  contextStore: IContextStore;
  pendingRequestStore: IPendingRequestStore;
  inFlightTaskStore: IInFlightTaskStore;
  fileStorageService: FileStorageService;
  userAuthStorage: IUserAuthStorage;
  scheduledRunStore: IScheduledRunStore;
  feedbackService?: FeedbackService;
  config: Config;
  /**
   * Answers a scheduled run parked on its owner's authorization. Optional because it
   * needs CONSOLE_BACKEND_URL; without it a parked job still asks but cannot be
   * restarted from the card.
   */
  scheduledRunResumeService?: ScheduledRunResumeService;
}
