export type RunEvent =
  | {
      type: 'status';
      stage: 'start' | 'submitting' | 'working' | 'retrying' | 'ensuring_artifact' | 'webiq_search';
      scenario: string;
      title?: string;
      toolbox?: string;
      elapsed_s?: number;
    }
  | {
      type: 'agent_start';
      agent: string;
      role: 'scenario';
      label: string;
    }
  | {
      type: 'activity';
      agent: string;
      kind: string;
      label: string;
      detail?: string;
    }
  | {
      type: 'delta';
      agent: string;
      text: string;
    }
  | {
      type: 'final';
      agent: string;
      text: string;
    }
  | {
      type: 'artifact';
      agent: string;
      id: string | null;
      filename: string;
      url?: string;
      error?: string;
      kind?: 'generated' | 'summary';
      persisted?: boolean;
    }
  | {
      type: 'enrichment';
      provider: 'webiq';
      status: 'ready' | 'empty' | 'failed';
      message: string;
      sources: EnrichmentSource[];
    }
  | {
      type: 'warning';
      agent?: string;
      message: string;
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
      outcome?: 'complete' | 'partial' | 'error';
    };

export type EnrichmentSource = {
  id: string;
  title: string;
  url: string;
  published_at: string | null;
  updated_at?: string | null;
  crawled_at?: string | null;
};

export async function consumeSseStream<TEvent extends { type: string }>(
  input: RequestInfo | URL,
  init: RequestInit,
  onEvent: (event: TEvent) => void
): Promise<void> {
  const response = await fetch(input, init);

  if (!response.ok) {
    throw new Error(
      `Workflow request failed (${response.status}). Check the configuration and retry.`
    );
  }

  if (!response.body) {
    throw new Error('Workflow response did not include a readable stream.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let line = '';
  let data: string[] = [];
  let afterCR = false;
  let terminal = false;
  let frameLength = 0;

  function consumeLine() {
    if (line === '') {
      if (data.length > 0) {
        let event: unknown;
        try {
          event = JSON.parse(data.join('\n'));
        } catch {
          throw new Error('The workflow returned an invalid event. Please retry.');
        }
        if (!event || typeof event !== 'object' || !('type' in event) || typeof event.type !== 'string') {
          throw new Error('The workflow returned an invalid event. Please retry.');
        }
        onEvent(event as TEvent);
        terminal = event.type === 'done';
      }
      data = [];
      frameLength = 0;
    } else if (line === 'data' || line.startsWith('data:')) {
      data.push(line === 'data' ? '' : line.slice(5).replace(/^ /, ''));
    }
    line = '';
  }

  function consumeText(text: string) {
    for (const character of text) {
      if (afterCR) {
        afterCR = false;
        if (character === '\n') continue;
      }
      if (character === '\r' || character === '\n') {
        consumeLine();
        afterCR = character === '\r';
        if (terminal) return;
      } else {
        line += character;
        if (++frameLength > 2_000_000) {
          throw new Error('The workflow event exceeded the supported size.');
        }
      }
    }
  }

  try {
    while (!terminal) {
      const { done, value } = await reader.read();
      consumeText(done ? decoder.decode() : decoder.decode(value, { stream: true }));
      if (done && !terminal) {
        throw new Error('The workflow connection ended before completion. The run may still be active.');
      }
    }
  } finally {
    try {
      await reader.cancel();
    } finally {
      reader.releaseLock();
    }
  }
}
