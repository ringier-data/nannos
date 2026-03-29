import { SESv2Client, SendEmailCommand } from '@aws-sdk/client-sesv2';
import { marked } from 'marked';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { base64ToBuffer, getExtensionFromMimeType } from '../utils/fileUtils.js';
import type { A2AArtifact } from './a2aClientService.js';

const logger = Logger.getLogger('EmailOutboundService');

/**
 * Marker inserted at the bottom of every outgoing email.
 * Used to strip quoted reply history from inbound emails.
 */
export const REPLY_MARKER = '—— agent-marker-dont-delete ——';

/**
 * Service for sending reply emails via AWS SES v2.
 * Uses the SES v2 Simple email API for all emails, including attachments.
 */
export class EmailOutboundService {
  private readonly sesClient: SESv2Client;
  private readonly fromEmail: string;

  constructor(config: Config) {
    this.sesClient = new SESv2Client({ region: config.ses.region });
    this.fromEmail = config.ses.fromEmail;
    logger.info(`EmailOutboundService initialized: from=${this.fromEmail}, region=${config.ses.region}`);
  }

  /**
   * Send a reply email with the A2A response.
   */
  async sendReply(opts: {
    to: string;
    subject: string;
    message: string;
    artifacts?: A2AArtifact[];
    originalMessageId?: string;
  }): Promise<void> {
    const { to, subject, message: message, artifacts, originalMessageId } = opts;
    const replySubject = subject.startsWith('Re:') ? subject : `Re: ${subject}`;

    // Convert markdown to HTML
    const htmlBody = await marked(message);

    // Extract file artifacts as SES v2 attachments
    const attachments = this.extractFileArtifacts(artifacts);

    // Append reply marker so inbound processing can strip quoted history
    const hiddenMarker = `<span style="display:none;font-size:0;color:transparent;max-height:0;overflow:hidden;">${REPLY_MARKER}</span>`;
    const textBodyWithMarker = `${REPLY_MARKER}\n${message}\n\n${REPLY_MARKER}`;
    const htmlBodyWithMarker = `${hiddenMarker}\n${htmlBody}\n${hiddenMarker}`;

    await this.sendSimple({
      to,
      subject: replySubject,
      textBody: textBodyWithMarker,
      htmlBody: htmlBodyWithMarker,
      attachments,
      inReplyTo: originalMessageId,
    });

    logger.info(`Reply sent to ${to}: ${replySubject}`);
  }

  /**
   * Send an authentication prompt email with login link.
   */
  async sendAuthPrompt(opts: {
    to: string;
    subject: string;
    authUrl: string;
    originalMessageId?: string;
  }): Promise<void> {
    const { to, subject, authUrl, originalMessageId } = opts;
    const replySubject = subject ? (subject.startsWith('Re:') ? subject : `Re: ${subject}`) : 'Authorization Required';

    const textBody = [
      // English
      'Hello,',
      'To process your request, you need to authorize first.',
      `Please click the following link to authorize: ${authUrl}`,
      'After authorization, your request will be processed automatically.',
      '',
      // German
      'Hallo,',
      'Um Ihre Anfrage zu bearbeiten, müssen Sie sich zuerst autorisieren.',
      `Bitte klicken Sie auf den folgenden Link zur Autorisierung: ${authUrl}`,
      'Nach der Autorisierung wird Ihre Anfrage automatisch bearbeitet.',
      '',
      // French
      'Bonjour,',
      "Pour traiter votre demande, vous devez d'abord vous autoriser.",
      `Veuillez cliquer sur le lien suivant pour vous autoriser : ${authUrl}`,
      'Après autorisation, votre demande sera traitée automatiquement.',
      '',
      // Italian
      'Ciao,',
      'Per elaborare la tua richiesta, devi prima autorizzarti.',
      `Clicca il seguente link per autorizzarti: ${authUrl}`,
      "Dopo l'autorizzazione, la tua richiesta verrà elaborata automaticamente.",
      '',
      'Best regards / Mit freundlichen Grüssen / Cordialement / Cordiali saluti,',
      'Ringier Nannos A2A Email Client',
    ].join('\n');

    const htmlBody = [
      // English
      'Hello,<br>',
      'To process your request, you need to authorize first.<br>',
      `To authorize, please <a href="${authUrl}">click here to begin</a>.<br>`,
      'After completed, your request will be processed automatically.<br>',
      '<br>',
      // German
      'Hallo,<br>',
      'Um Ihre Anfrage zu bearbeiten, müssen Sie sich zuerst autorisieren.<br>',
      `Zur Autorisierung bitte <a href="${authUrl}">hier klicken</a>.<br>`,
      'Nach Abschluss wird Ihre Anfrage automatisch bearbeitet.<br>',
      '<br>',
      // French
      'Bonjour,<br>',
      "Pour traiter votre demande, vous devez d'abord vous autoriser.<br>",
      `Pour vous autoriser, veuillez <a href="${authUrl}">cliquer ici</a>.<br>`,
      'Une fois terminé, votre demande sera traitée automatiquement.<br>',
      '<br>',
      // Italian
      'Ciao,<br>',
      'Per elaborare la tua richiesta, devi prima autorizzarti.<br>',
      `Per autorizzarti, <a href="${authUrl}">clicca qui</a>.<br>`,
      'Una volta completato, la tua richiesta verrà elaborata automaticamente.<br>',
      '<br>',
      'Best regards / Mit freundlichen Grüssen / Cordialement / Cordiali saluti,<br>',
      'Ringier Nannos A2A Email Client',
    ].join('\n');

    const textBodyWithMarker = `${textBody}\n\n${REPLY_MARKER}`;
    const htmlBodyWithMarker = `${htmlBody}<br><br>${REPLY_MARKER}`;

    await this.sendSimple({
      to,
      subject: replySubject,
      textBody: textBodyWithMarker,
      htmlBody: htmlBodyWithMarker,
      inReplyTo: originalMessageId,
    });
    logger.info(`Auth prompt sent to ${to}`);
  }

  /**
   * Send an error notification email.
   */
  async sendErrorNotification(opts: {
    to: string;
    subject: string;
    errorMessage: string;
    originalMessageId?: string;
  }): Promise<void> {
    const { to, subject, errorMessage, originalMessageId } = opts;
    const replySubject = subject
      ? subject.startsWith('Re:')
        ? subject
        : `Re: ${subject}`
      : 'Error Processing Request';

    const textBody = [
      // English
      `An error occurred while processing your request:`,
      `${errorMessage}`,
      'Please try again or contact support.',
      '',
      // German
      'Bei der Bearbeitung Ihrer Anfrage ist ein Fehler aufgetreten:',
      `${errorMessage}`,
      'Bitte versuchen Sie es erneut oder kontaktieren Sie den Support.',
      '',
      // French
      'Une erreur est survenue lors du traitement de votre demande :',
      `${errorMessage}`,
      'Veuillez réessayer ou contacter le support.',
      '',
      // Italian
      "Si è verificato un errore durante l'elaborazione della tua richiesta:",
      `${errorMessage}`,
      'Riprova o contatta il supporto.',
    ].join('\n');

    const textBodyWithMarker = `${textBody}\n\n${REPLY_MARKER}`;

    await this.sendSimple({
      to,
      subject: replySubject,
      textBody: textBodyWithMarker,
      inReplyTo: originalMessageId,
    });
    logger.info(`Error notification sent to ${to}: ${errorMessage.substring(0, 50)}`);
  }

  // ===========================================================================
  // Private helpers
  // ===========================================================================

  /**
   * Send an email using the SES v2 Simple content API.
   * The SDK handles all MIME encoding (charset, transfer-encoding, etc.).
   */
  private async sendSimple(opts: {
    to: string;
    subject: string;
    textBody: string;
    htmlBody?: string;
    attachments?: Array<{ filename: string; mimeType: string; data: Buffer }>;
    inReplyTo?: string;
  }): Promise<void> {
    const headers: Array<{ Name: string; Value: string }> = [];
    if (opts.inReplyTo) {
      headers.push({ Name: 'In-Reply-To', Value: opts.inReplyTo });
      headers.push({ Name: 'References', Value: opts.inReplyTo });
    }

    const sesAttachments = opts.attachments?.map((att) => ({
      RawContent: att.data,
      ContentDisposition: 'ATTACHMENT' as const,
      FileName: att.filename,
      ContentTransferEncoding: 'BASE64' as const,
    }));

    const command = new SendEmailCommand({
      FromEmailAddress: `Nannos <${this.fromEmail}>`,
      Destination: { ToAddresses: [opts.to] },
      Content: {
        Simple: {
          Subject: { Data: opts.subject, Charset: 'UTF-8' },
          Body: {
            Text: { Data: opts.textBody, Charset: 'UTF-8' },
            ...(opts.htmlBody ? { Html: { Data: opts.htmlBody, Charset: 'UTF-8' } } : {}),
          },
          Headers: headers.length > 0 ? headers : undefined,
          Attachments: sesAttachments && sesAttachments.length > 0 ? sesAttachments : undefined,
        },
      },
    });

    await this.sesClient.send(command);
  }

  private extractFileArtifacts(artifacts?: A2AArtifact[]): Array<{ filename: string; mimeType: string; data: Buffer }> {
    const files: Array<{ filename: string; mimeType: string; data: Buffer }> = [];
    if (!artifacts?.length) return files;

    for (const artifact of artifacts) {
      for (const part of artifact.parts) {
        if ((part.kind === 'data' || part.kind === 'file') && part.data && part.mimeType) {
          const buffer = base64ToBuffer(part.data);
          const ext = getExtensionFromMimeType(part.mimeType);
          const filename = part.name || artifact.name || `artifact-${artifact.artifactId}.${ext}`;
          files.push({ filename, mimeType: part.mimeType, data: buffer });
        }
      }
    }
    return files;
  }
}
