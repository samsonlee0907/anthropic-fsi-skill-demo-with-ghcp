"use client";

import { useEffect, useMemo, useRef, useState } from 'react';
import { consumeSseStream, type RunEvent } from '../lib/sse';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? '';

type ScenarioKey = 'equity-research' | 'ib-pitch' | 'pe-lbo';

type Scenario = {
  key: ScenarioKey;
  title: string;
  tagline: string;
  toolbox: string;
  orchestrator: string;
  agents: string[];
  default_prompt: string;
};

type Toolbox = {
  name: string;
  description: string;
};

type HealthResponse = {
  status: string;
  project_endpoint: string;
};

type Artifact = {
  id: string;
  filename: string;
  url: string;
};

type AgentRun = {
  agent: string;
  role: 'specialist' | 'orchestrator';
  label: string;
  status: 'running' | 'done' | 'error';
  output: string;
  artifacts: Artifact[];
  error?: string;
};

type RunMeta = {
  scenario: string;
  title: string;
  toolbox: string;
};

type OverallStatus = 'idle' | 'running' | 'complete' | 'error';

type ScenariosResponse = {
  scenarios: Scenario[];
};

type ToolboxesResponse = {
  toolboxes: Toolbox[];
};

const workflowLabels: Record<OverallStatus, string> = {
  idle: 'Ready',
  running: 'Running',
  complete: 'Complete',
  error: 'Attention needed'
};

export function Studio() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [toolboxes, setToolboxes] = useState<Toolbox[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [selectedKey, setSelectedKey] = useState<ScenarioKey | null>(null);
  const [prompt, setPrompt] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [overallStatus, setOverallStatus] = useState<OverallStatus>('idle');
  const [runMeta, setRunMeta] = useState<RunMeta | null>(null);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [finishedAt, setFinishedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(Date.now());
  const abortRef = useRef<AbortController | null>(null);

  const selectedScenario = useMemo(
    () => scenarios.find((scenario) => scenario.key === selectedKey) ?? null,
    [scenarios, selectedKey]
  );

  const elapsedMs = startedAt ? (isRunning ? clock : finishedAt ?? clock) - startedAt : 0;

  useEffect(() => {
    let isActive = true;

    async function loadInitialData() {
      setIsLoading(true);
      setLoadError(null);

      const [scenarioResult, toolboxResult, healthResult] = await Promise.allSettled([
        fetchJson<ScenariosResponse>(`${API_BASE_URL}/api/scenarios`),
        fetchJson<ToolboxesResponse>(`${API_BASE_URL}/api/toolboxes`),
        fetchJson<HealthResponse>(`${API_BASE_URL}/api/health`)
      ]);

      if (!isActive) {
        return;
      }

      if (scenarioResult.status === 'fulfilled') {
        setScenarios(scenarioResult.value.scenarios);
      } else {
        setLoadError(getErrorMessage(scenarioResult.reason, 'Unable to load scenarios.'));
      }

      if (toolboxResult.status === 'fulfilled') {
        setToolboxes(toolboxResult.value.toolboxes);
      }

      if (healthResult.status === 'fulfilled') {
        setHealth(healthResult.value);
      }

      setIsLoading(false);
    }

    loadInitialData();

    return () => {
      isActive = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!isRunning) {
      return;
    }

    const interval = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [isRunning]);

  function selectScenario(scenario: Scenario) {
    setSelectedKey(scenario.key);
    setPrompt(scenario.default_prompt);
    setRunError(null);
  }

  async function runWorkflow() {
    if (!selectedScenario || isRunning) {
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    const startTime = Date.now();

    setIsRunning(true);
    setOverallStatus('running');
    setRunError(null);
    setRunMeta(null);
    setAgentRuns([]);
    setStartedAt(startTime);
    setFinishedAt(null);
    setClock(startTime);

    try {
      await consumeSseStream<RunEvent>(
        `${API_BASE_URL}/api/run`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream'
          },
          body: JSON.stringify({
            scenario: selectedScenario.key,
            message: prompt.trim() || undefined
          }),
          signal: controller.signal
        },
        handleRunEvent
      );
    } catch (error) {
      if (controller.signal.aborted) {
        return;
      }

      setRunError(getErrorMessage(error, 'The workflow stream stopped unexpectedly.'));
      setOverallStatus('error');
      setFinishedAt(Date.now());
    } finally {
      if (!controller.signal.aborted) {
        setIsRunning(false);
        abortRef.current = null;
      }
    }
  }

  function handleRunEvent(event: RunEvent) {
    switch (event.type) {
      case 'status':
        setRunMeta({ scenario: event.scenario, title: event.title, toolbox: event.toolbox });
        setOverallStatus('running');
        break;
      case 'agent_start':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          role: event.role,
          label: event.label,
          status: 'running',
          error: undefined
        }));
        break;
      case 'delta':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          output: `${agent.output}${event.text}`
        }));
        break;
      case 'artifact':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          artifacts: agent.artifacts.some((artifact) => artifact.id === event.id)
            ? agent.artifacts
            : [...agent.artifacts, { id: event.id, filename: event.filename, url: event.url }]
        }));
        break;
      case 'error':
        if (event.agent) {
          upsertAgent(event.agent, (agent) => ({
            ...agent,
            status: 'error',
            error: event.message
          }));
        } else {
          setRunError(event.message);
          setOverallStatus('error');
        }
        break;
      case 'agent_end':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          status: agent.status === 'error' ? 'error' : 'done'
        }));
        break;
      case 'done':
        setOverallStatus('complete');
        setFinishedAt(Date.now());
        setIsRunning(false);
        break;
    }
  }

  function upsertAgent(agentName: string, updater: (agent: AgentRun) => AgentRun) {
    setAgentRuns((currentAgents) => {
      const existingIndex = currentAgents.findIndex((agent) => agent.agent === agentName);

      if (existingIndex >= 0) {
        return currentAgents.map((agent, index) => (index === existingIndex ? updater(agent) : agent));
      }

      const fallbackAgent: AgentRun = {
        agent: agentName,
        role: 'specialist',
        label: formatAgentName(agentName),
        status: 'running',
        output: '',
        artifacts: []
      };

      return [...currentAgents, updater(fallbackAgent)];
    });
  }

  return (
    <main className="studioShell">
      <header className="hero">
        <nav className="topNav" aria-label="Portal">
          <div className="brandLockup">
            <span className="brandMark" aria-hidden="true">
              <svg viewBox="0 0 32 32" role="img">
                <path d="M16 3 28 9.8v12.4L16 29 4 22.2V9.8L16 3Z" />
                <path d="M10.2 12.2 16 8.9l5.8 3.3v6.7L16 22.3l-5.8-3.4v-6.7Z" />
              </svg>
            </span>
            <span>
              <strong>FSI Multi-Agent Studio</strong>
              <small>Powered by Azure AI Foundry Agent Service</small>
            </span>
          </div>
          <div className="healthCluster" aria-label="Backend status">
            <span className={health?.status === 'ok' ? 'statusLight online' : 'statusLight'} />
            <span>{health?.status === 'ok' ? 'API connected' : 'API pending'}</span>
          </div>
        </nav>

        <section className="heroContent" aria-labelledby="studio-title">
          <div>
            <p className="heroLabel">Azure AI Foundry demo portal</p>
            <h1 id="studio-title">Coordinate specialist financial agents from one controlled workspace.</h1>
            <p className="heroCopy">
              Select an FSI workflow, tailor the mandate, and watch analysts, modeling agents, and the
              orchestrator produce live narrative outputs and downloadable artifacts.
            </p>
          </div>
          <aside className="heroPanel" aria-label="Run readiness">
            <span className={`runBadge ${overallStatus}`}>{workflowLabels[overallStatus]}</span>
            <dl>
              <div>
                <dt>Runtime</dt>
                <dd>{startedAt ? formatElapsed(elapsedMs) : '00:00'}</dd>
              </div>
              <div>
                <dt>Backend</dt>
                <dd>{API_BASE_URL || 'same-origin'}</dd>
              </div>
            </dl>
          </aside>
        </section>
      </header>

      <section className="workspace" aria-label="Studio workspace">
        {loadError ? <Alert tone="error" title="Connection issue" message={loadError} /> : null}

        <section className="sectionBlock" aria-labelledby="scenarios-title">
          <div className="sectionHeading">
            <div>
              <h2 id="scenarios-title">Scenario library</h2>
              <p>Three production-style workflows mapped to Foundry agents and toolboxes.</p>
            </div>
            {isLoading ? <span className="subtleStatus">Loading scenarios…</span> : null}
          </div>

          <div className="scenarioGrid">
            {isLoading
              ? Array.from({ length: 3 }).map((_, index) => <div className="scenarioSkeleton" key={index} />)
              : scenarios.map((scenario) => (
                  <button
                    className={`scenarioCard ${scenario.key === selectedKey ? 'selected' : ''}`}
                    key={scenario.key}
                    onClick={() => selectScenario(scenario)}
                    type="button"
                  >
                    <span className="scenarioTitleRow">
                      <strong>{scenario.title}</strong>
                      <span>{scenario.key === selectedKey ? 'Selected' : 'Open'}</span>
                    </span>
                    <span className="scenarioTagline">{scenario.tagline}</span>
                    <span className="chipRow" aria-label={`${scenario.title} agents`}>
                      {scenario.agents.map((agent) => (
                        <span className="chip" key={agent}>
                          {formatAgentName(agent)}
                        </span>
                      ))}
                      <span className="chip toolboxChip">{scenario.toolbox}</span>
                    </span>
                  </button>
                ))}
          </div>
        </section>

        <div className="contentGrid">
          <section className="runPanel" aria-labelledby="run-title">
            <div className="sectionHeading compact">
              <div>
                <h2 id="run-title">Run workspace</h2>
                <p>{selectedScenario ? selectedScenario.title : 'Select a scenario to open the editable mandate.'}</p>
              </div>
              {runMeta ? <span className="toolboxBadge">{runMeta.toolbox}</span> : null}
            </div>

            {selectedScenario ? (
              <div className="promptComposer">
                <label htmlFor="workflow-prompt">Workflow mandate</label>
                <textarea
                  id="workflow-prompt"
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  placeholder="Describe the client objective, constraints, and desired deliverables."
                  rows={9}
                />
                <div className="composerActions">
                  <p>All data is illustrative and synthetic — not investment advice.</p>
                  <button className="primaryButton" disabled={isRunning} onClick={runWorkflow} type="button">
                    {isRunning ? (
                      <>
                        <span className="buttonSpinner" aria-hidden="true" /> Workflow running…
                      </>
                    ) : agentRuns.length > 0 ? (
                      'Start a new run'
                    ) : (
                      'Run multi-agent workflow'
                    )}
                  </button>
                </div>
              </div>
            ) : (
              <div className="emptyState">
                <strong>Choose a scenario to begin.</strong>
                <p>The prompt will load here with the scenario’s default financial-services mandate.</p>
              </div>
            )}

            {runError ? <Alert tone="error" title="Workflow error" message={runError} /> : null}
          </section>

          <aside className="toolboxPanel" aria-labelledby="toolboxes-title">
            <div className="sectionHeading compact">
              <div>
                <h2 id="toolboxes-title">Toolboxes</h2>
                <p>Capabilities available to the backend agents.</p>
              </div>
            </div>
            <div className="toolboxList">
              {toolboxes.length > 0 ? (
                toolboxes.map((toolbox) => (
                  <article className="toolboxItem" key={toolbox.name}>
                    <strong>{toolbox.name}</strong>
                    <p>{toolbox.description}</p>
                  </article>
                ))
              ) : (
                <p className="mutedCopy">Toolbox details appear after the API responds.</p>
              )}
            </div>
          </aside>
        </div>

        <section className="timelineSection" aria-labelledby="timeline-title" aria-live="polite">
          <div className="sectionHeading compact">
            <div>
              <h2 id="timeline-title">Agent timeline</h2>
              <p>
                {runMeta
                  ? `${runMeta.title} is streaming through the specialist agents and final orchestrator.`
                  : 'Run events will appear here as each agent starts, streams, and completes.'}
              </p>
            </div>
            <div className="elapsedPill" title="Elapsed time">
              <span className={isRunning ? 'pulseDot' : 'pulseDot still'} />
              {formatElapsed(elapsedMs)}
            </div>
          </div>

          <div className="timeline">
            {agentRuns.length === 0 ? (
              <div className="timelineEmpty">
                <strong>No agent events yet.</strong>
                <p>When the workflow starts, specialist output and artifacts will stream into this timeline.</p>
              </div>
            ) : (
              agentRuns.map((agentRun) => <AgentCard agentRun={agentRun} key={agentRun.agent} />)
            )}
          </div>
        </section>
      </section>
    </main>
  );
}

function AgentCard({ agentRun }: { agentRun: AgentRun }) {
  const isOrchestrator = agentRun.role === 'orchestrator';

  return (
    <article className={`agentCard ${agentRun.status} ${isOrchestrator ? 'orchestrator' : ''}`}>
      <header className="agentHeader">
        <div>
          <span className="rolePill">{isOrchestrator ? 'Final synthesis' : 'Specialist'}</span>
          <h3>{agentRun.label}</h3>
          <p>{formatAgentName(agentRun.agent)}</p>
        </div>
        <StatusIndicator status={agentRun.status} />
      </header>

      {agentRun.error ? <Alert tone="error" title="Agent error" message={agentRun.error} /> : null}

      <pre className="agentOutput">
        {agentRun.output.trim().length > 0 ? agentRun.output : 'Awaiting streamed output…'}
      </pre>

      {agentRun.artifacts.length > 0 ? (
        <div className="artifactRow" aria-label={`${agentRun.label} artifacts`}>
          {agentRun.artifacts.map((artifact) => (
            <a className="artifactChip" download href={`${API_BASE_URL}${artifact.url}`} key={artifact.id}>
              <span aria-hidden="true">▣</span>
              {artifact.filename}
            </a>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function StatusIndicator({ status }: { status: AgentRun['status'] }) {
  if (status === 'running') {
    return (
      <span className="agentStatus running">
        <span className="spinner" aria-hidden="true" /> Running
      </span>
    );
  }

  if (status === 'error') {
    return <span className="agentStatus error">Error</span>;
  }

  return <span className="agentStatus done">✓ Done</span>;
}

function Alert({ tone, title, message }: { tone: 'error'; title: string; message: string }) {
  return (
    <div className={`alert ${tone}`} role="alert">
      <strong>{title}</strong>
      <span>{message}</span>
    </div>
  );
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json'
    }
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

function getErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(totalSeconds / 60)
    .toString()
    .padStart(2, '0');
  const seconds = (totalSeconds % 60).toString().padStart(2, '0');
  return `${minutes}:${seconds}`;
}

function formatAgentName(agentName: string): string {
  return agentName
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
