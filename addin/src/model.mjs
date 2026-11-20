// All email/API strings render through textContent. No email HTML is executed.
export const labels = Object.freeze({approve: 'Approve', reject: 'Reject', route: 'Change routing', addressed: 'Mark addressed', followup: 'Follow-up', close: 'Close', reopen: 'Reopen', retry: 'Prepare retry', retry_notice: 'Retry failed notification'});
export const needsReason = new Set(['reject', 'route', 'close', 'reopen', 'reconcile', 'addressed']);

export function actionPayload(ticket, command, reason = '', category = '') {
  if (!ticket || !ticket.actions.includes(command)) throw new Error('Refresh the ticket before acting.');
  if (needsReason.has(command) && !reason.trim()) throw new Error('Please record a reason or evidence.');
  if (reason.length > 3000) throw new Error('Reason must be at most 3000 characters.');
  return {action: command, version: ticket.version, reason, ...(command === 'route' ? {category} : {})};
}

export function currentMessageId(office) {
  const item = office.context.mailbox.item;
  return item && typeof item.internetMessageId === 'string' ? item.internetMessageId : null;
}

export class Selection {
  generation = 0;
  next() { return ++this.generation; }
  isCurrent(generation) { return this.generation === generation; }
}
