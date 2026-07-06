export type RunEvent =
  | {
      type: 'status';
      stage: 'start';
      scenario: string;
      title: string;
      toolbox: string;
    }
  | {
      type: 'agent_start';
      agent: string;
      role: 'specialist' | 'orchestrator';
      label: string;
    }
  | {
      type: 'delta';
      agent: string;
      text: string;
    }
  | {
      type: 'artifact';
      agent: string;
      id: string;
      filename: string;
      url: string;
    }
  | {
      type: 'error';
      agent?: string;
      message: string;
    }
  | {
      type: 'agent_end';
      agent: string;
    }
  | {
      type: 'done';
    };

export async function consumeSseStream<TEvent extends { type: string }>(
  input: RequestInfo | URL,
  init: RequestInit,
  onEvent: (event: TEvent) => void
): Promise<void> {
  const response = await fetch(input, init);

  if (!response.ok) {
    const details = await safeReadText(response);
    throw new Error(
      `Workflow request failed (${response.status} ${response.statusText})${details ? `: ${details}` : ''}`
    );
  }

  if (!response.body) {
    throw new Error('Workflow response did not include a readable stream.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();

    if (done) {
      buffer += decoder.decode();
      flushBuffer(buffer, onEvent);
      return;
    }

    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? '';

    for (const frame of frames) {
      emitFrame(frame, onEvent);
    }
  }
}

async function safeReadText(response: Response): Promise<string> {
  try {
    return (await response.text()).trim();
  } catch {
    return '';
  }
}

function flushBuffer<TEvent extends { type: string }>(
  buffer: string,
  onEvent: (event: TEvent) => void
): void {
  if (buffer.trim().length > 0) {
    emitFrame(buffer, onEvent);
  }
}

function emitFrame<TEvent extends { type: string }>(
  frame: string,
  onEvent: (event: TEvent) => void
): void {
  const payload = extractDataPayload(frame);

  if (!payload) {
    return;
  }

  try {
    onEvent(JSON.parse(payload) as TEvent);
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'Unknown parse error';
    throw new Error(`Invalid SSE data frame: ${reason}`);
  }
}

function extractDataPayload(frame: string): string | null {
  const dataLines = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.replace(/^data:\s?/, ''));

  if (dataLines.length === 0) {
    return null;
  }

  const payload = dataLines.join('\n').trim();
  return payload.length > 0 ? payload : null;
}
