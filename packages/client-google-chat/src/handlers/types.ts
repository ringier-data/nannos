import type {
  IContextStore,
  IPendingRequestStore,
  IInFlightTaskStore,
} from '../storage/types.js';

import { A2AClientService } from "../services/a2aClientService.js";
import { FeedbackService } from "../services/feedbackService.js";
import { FileStorageService } from "../services/fileStorageService.js";
import { GoogleChatService } from "../services/googleChatService.js";
import { UserAuthService } from "../services/userAuthService.js";
import { Config } from '../config/config.js';

export interface HandlerDependencies {
  userAuthService: UserAuthService;
  a2aClientService: A2AClientService;
  chatService: GoogleChatService;
  contextStore: IContextStore;
  pendingRequestStore: IPendingRequestStore;
  inFlightTaskStore: IInFlightTaskStore;
  fileStorageService: FileStorageService;
  feedbackService?: FeedbackService;
  config: Config
  ;
}
