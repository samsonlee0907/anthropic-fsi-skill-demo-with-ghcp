"use client";

import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { consumeSseStream, type EnrichmentSource, type RunEvent } from '../lib/sse';
import { nextRunStatus, suggestedNewsQuery, webiqHeaders, type OverallStatus } from '../lib/run-state';
import { fetchArtifact } from '../lib/artifacts';
import { DeploymentMetadata } from './DeploymentMetadata';
import { SourceCitation } from './SourceCitation';

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? '').replace(/\/+$/, '');

type ScenarioKey = 'equity-research' | 'ib-pitch' | 'pe-lbo';

type Scenario = {
  key: ScenarioKey;
  title: string;
  tagline: string;
  toolbox: string;
  agent: string;
  skills: string[];
  default_prompt: string;
  edgar_prompt?: string;
};

type Toolbox = {
  name: string;
  description: string;
  tools?: string[];
};

type HealthResponse = {
  status: string;
  project_endpoint: string;
  environment_name?: string | null;
  model_deployment_name?: string | null;
};

type Artifact = {
  id: string;
  filename: string;
  url: string;
  kind?: 'generated' | 'summary';
};

type Activity = {
  kind: string;
  label: string;
  detail?: string;
};

type AgentRun = {
  agent: string;
  role: 'scenario';
  label: string;
  status: 'running' | 'done' | 'error';
  output: string;
  activities: Activity[];
  elapsedS?: number;
  artifacts: Artifact[];
  error?: string;
};

type RunMeta = {
  scenario: string;
  title: string;
  toolbox: string;
};

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
  partial: 'Partial result',
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
  const [webiqQuery, setWebiqQuery] = useState('');
  // Held only in component memory: never persisted to storage, cookies, URLs or logs.
  const [webiqKey, setWebiqKey] = useState('');
  const [showWebiqKey, setShowWebiqKey] = useState(false);
  const [webiqFailed, setWebiqFailed] = useState(false);
  const [webiqNote, setWebiqNote] = useState('');
  const [sources, setSources] = useState<EnrichmentSource[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [statusMessage, setStatusMessage] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const outcomeRef = useRef<OverallStatus>('idle');

  const selectedScenario = useMemo(
    () => scenarios.find((scenario) => scenario.key === selectedKey) ?? null,
    [scenarios, selectedKey]
  );

  const selectedToolbox = useMemo(
    () => (selectedScenario ? toolboxes.find((tb) => tb.name === selectedScenario.toolbox) ?? null : null),
    [toolboxes, selectedScenario]
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
    updatePrompt(scenario.default_prompt);
    setRunError(null);
  }

  function updatePrompt(value: string) {
    setPrompt(value);
    setWebiqQuery('');
  }

  async function runWorkflow(withoutWebiq = false) {
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
    outcomeRef.current = 'running';
    setWebiqFailed(false);
    setWebiqNote('');
    setSources([]);
    setWarnings([]);
    setStatusMessage('Connecting to the backend...');

    try {
      if (!withoutWebiq && webiqQuery.trim() && !webiqKey.trim()) {
        throw new Error('This explicit news query requires a WebIQ API key. Enter a key or clear the news query. No request was sent.');
      }
      const extraHeaders = webiqHeaders(withoutWebiq || !webiqQuery.trim() ? '' : webiqKey, API_BASE_URL, window.location.href);
      const usingWebiq = Boolean(extraHeaders['X-WebIQ-Key']);
      if (usingWebiq && (!webiqQuery.trim() || webiqQuery.trim().length > 500)) {
        throw new Error('Enter a WebIQ news query for the company in your mandate (up to 500 characters).');
      }
      await consumeSseStream<RunEvent>(
        `${API_BASE_URL}/api/run`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
            ...extraHeaders
          },
          body: JSON.stringify({
            scenario: selectedScenario.key,
            message: prompt.trim() || undefined,
            ...(usingWebiq ? { webiq_query: webiqQuery.trim() } : {})
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
      setStatusMessage('The workflow could not complete. Review the error before starting another run.');
      setAgentRuns((agents) => agents.map((agent) => agent.status === 'running'
        ? { ...agent, status: 'error', error: 'The connection stopped. The remote run may still be active.' }
        : agent));
      setFinishedAt(Date.now());
    } finally {
      if (!controller.signal.aborted) {
        setIsRunning(false);
        abortRef.current = null;
      }
    }
  }

  function handleRunEvent(event: RunEvent) {
    outcomeRef.current = nextRunStatus(outcomeRef.current, event);
    setOverallStatus(outcomeRef.current === 'partial' && event.type !== 'done' ? 'running' : outcomeRef.current);
    switch (event.type) {
      case 'status':
        if (event.title) {
          setRunMeta({ scenario: event.scenario, title: event.title, toolbox: event.toolbox ?? '' });
        }
        if (typeof event.elapsed_s === 'number') {
          const secs = event.elapsed_s;
          setAgentRuns((agents) =>
            agents.map((agent) =>
              agent.status === 'running' ? { ...agent, elapsedS: secs } : agent
            )
          );
        }
        setStatusMessage({
          start: 'Starting the scenario.',
          submitting: 'Submitting the hosted-agent request.',
          working: 'Waiting for the hosted agent. Intermediate tool activity is unavailable.',
          retrying: 'Retrying the agent after a timeout or rate limit.',
          ensuring_artifact: 'Attempting to recover the missing workbook or deck.',
          webiq_search: 'Preparing optional WebIQ enrichment before starting the agent.'
        }[event.stage]);
        break;
      case 'enrichment':
        setWebiqNote(event.message);
        setSources(event.sources);
        setWebiqFailed(event.status === 'failed');
        break;
      case 'warning':
        setWarnings((current) => current.includes(event.message) ? current : [...current, event.message]);
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
      case 'activity':
        upsertAgent(event.agent, (agent) => {
          const last = agent.activities[agent.activities.length - 1];
          if (last && last.kind === event.kind && last.detail === (event.detail ?? '')) {
            return agent;
          }
          return {
            ...agent,
            activities: [
              ...agent.activities,
              { kind: event.kind, label: event.label, detail: event.detail }
            ]
          };
        });
        break;
      case 'delta':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          output: `${agent.output}${event.text}`
        }));
        break;
      case 'final':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          output: event.text
        }));
        break;
      case 'artifact':
        if (!event.id || !event.url) {
          setWarnings((current) => [...current, `Could not publish ${event.filename}. Please retry the workflow.`]);
          break;
        }
        {
          const artifact: Artifact = { id: event.id, filename: event.filename, url: event.url, kind: event.kind };
          upsertAgent(event.agent, (agent) => ({
            ...agent,
            artifacts: agent.artifacts.some((existing) => existing.id === artifact.id)
              ? agent.artifacts
              : [...agent.artifacts, artifact]
          }));
        }
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
        }
        break;
      case 'agent_end':
        upsertAgent(event.agent, (agent) => ({
          ...agent,
          status: agent.status === 'error' ? 'error' : 'done'
        }));
        break;
      case 'done':
        setFinishedAt(Date.now());
        setStatusMessage(outcomeRef.current === 'complete' ? 'Workflow complete.' :
          outcomeRef.current === 'partial' ? 'Workflow finished with a partial result. Review the warnings.' :
          'Workflow failed. Review the error before retrying.');
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
        role: 'scenario',
        label: formatAgentName(agentName),
        status: 'running',
        output: '',
        activities: [],
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
              <strong>FSI Agent Demo Portal</strong>
              <small>Powered by Microsoft Foundry Agent Service</small>
            </span>
          </div>
          <div className="healthCluster" aria-label="Backend status">
            <span className={health?.status === 'ok' ? 'statusLight online' : 'statusLight'} />
            <span>{health?.status === 'ok' ? 'API connected' : 'API pending'}</span>
          </div>
        </nav>

        <section className="heroContent" aria-labelledby="studio-title">
          <div>
            <p className="heroLabel">Microsoft Foundry demo portal</p>
            <h1 id="studio-title">FSI Agent Demo Portal</h1>
            <p className="heroCopy">
              Select an FSI workflow, tailor the mandate, and watch a dedicated scenario agent load
              the relevant Anthropic-derived skills, pull live figures from SEC EDGAR filings and
              web search, run code interpreter, and produce narrative output plus downloadable
              Excel and PowerPoint artifacts.
            </p>
          </div>
          <aside className="heroPanel" aria-label="Run readiness">
            <span className={`runBadge ${overallStatus}`}>{workflowLabels[overallStatus]}</span>
            <dl>
              <DeploymentMetadata environmentName={health?.environment_name}
                modelDeploymentName={health?.model_deployment_name} />
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
              <p>Three production-style workflows, each mapped to one Foundry scenario agent and its skill toolbox.</p>
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
                    disabled={isRunning}
                    aria-pressed={scenario.key === selectedKey}
                    type="button"
                  >
                    <span className="scenarioTitleRow">
                      <strong>{scenario.title}</strong>
                      <span>{scenario.key === selectedKey ? 'Selected' : 'Open'}</span>
                    </span>
                    <span className="scenarioTagline">{scenario.tagline}</span>
                    <span className="chipRow" aria-label={`${scenario.title} skills`}>
                      {scenario.skills.slice(0, 4).map((skill) => (
                        <span className="chip" key={skill}>
                          {formatSkillName(skill)}
                        </span>
                      ))}
                      {scenario.skills.length > 4 ? (
                        <span className="chip">+{scenario.skills.length - 4} more</span>
                      ) : null}
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
                <div className="skillLibrary" aria-label="Skills and tools available to this agent">
                  <span className="skillLibraryLabel">Skills in toolbox</span>
                  <span className="chipRow">
                    {selectedScenario.skills.map((skill) => (
                      <span className="chip" key={skill}>
                        {formatSkillName(skill)}
                      </span>
                    ))}
                  </span>
                  {selectedToolbox?.tools && selectedToolbox.tools.length > 0 ? (
                    <>
                      <span className="skillLibraryLabel">Tools in toolbox</span>
                      <span className="chipRow toolChipRow">
                        {selectedToolbox.tools.map((tool) => (
                          <span className="chip toolChip" key={tool}>
                            {formatToolName(tool)}
                          </span>
                        ))}
                      </span>
                    </>
                  ) : null}
                </div>
                <label htmlFor="workflow-prompt">Workflow mandate</label>
                <div className="promptPresets" role="group" aria-label="Prompt presets">
                  <button
                    className="presetButton"
                    type="button"
                    disabled={isRunning}
                    onClick={() => updatePrompt(selectedScenario.default_prompt)}
                  >
                    Default (Microsoft)
                  </button>
                  {selectedScenario.edgar_prompt ? (
                    <button
                      className="presetButton edgarPreset"
                      type="button"
                      disabled={isRunning}
                      onClick={() => updatePrompt(selectedScenario.edgar_prompt ?? '')}
                      title="Loads the same workflow for a different real public company, sourced from SEC EDGAR filings"
                    >
                      🏛️ Try another company
                    </button>
                  ) : null}
                </div>
                <textarea
                  id="workflow-prompt"
                  value={prompt}
                  onChange={(event) => updatePrompt(event.target.value)}
                  disabled={isRunning}
                  placeholder="Describe the client objective, constraints, and desired deliverables."
                  rows={9}
                />
                <details className="webiqConfig">
                  <summary>
                    Optional WebIQ news enrichment
                    <span className="webiqState">{webiqKey.trim() ? ' — key entered (not verified)' : ' — no key entered'}</span>
                  </summary>
                  <p>A key alone never starts a search: WebIQ is called only when you also enter a news query and run this workflow. Leave the query blank for no WebIQ calls. SEC EDGAR and toolbox web search remain available without it.</p>
                  <label htmlFor="webiq-key">WebIQ API key</label>
                  <div className="webiqKeyRow">
                    <input id="webiq-key" type={showWebiqKey ? 'text' : 'password'} value={webiqKey}
                      onChange={(event) => setWebiqKey(event.target.value)}
                      disabled={isRunning} maxLength={4096} autoComplete="off" autoCorrect="off"
                      autoCapitalize="none" spellCheck={false} aria-describedby="webiq-key-privacy" />
                    <button className="presetButton" type="button" aria-pressed={showWebiqKey}
                      onClick={() => setShowWebiqKey((value) => !value)}>{showWebiqKey ? 'Hide key' : 'Show key'}</button>
                    <button className="presetButton" type="button" disabled={isRunning}
                      onClick={() => { setWebiqKey(''); setShowWebiqKey(false); }}>Clear key</button>
                  </div>
                  <p id="webiq-key-privacy">Kept only in this tab&apos;s memory and sent to the backend in a request header for that run. Never saved to browser storage, cookies, URLs or run history; reloading the page clears it.</p>
                  <label htmlFor="webiq-query">News query — confirm it matches the company in your mandate</label>
                  <input id="webiq-query" value={webiqQuery}
                    onChange={(event) => setWebiqQuery(event.target.value)}
                    disabled={isRunning} maxLength={500}
                    placeholder={suggestedNewsQuery(prompt) || 'Company name or ticker and recent news topic'} />
                  <p>Only this query is sent to WebIQ, not your full mandate. Editing the mandate resets the query;
                    prompts without an explicit ticker require you to enter it. Retrieved passages are untrusted context.</p>
                </details>
                <div className="composerActions">
                  <p>AI-generated from public SEC filings and web sources — not investment advice.</p>
                  <button className="primaryButton" disabled={isRunning} onClick={() => runWorkflow()} type="button">
                    {isRunning ? (
                      <>
                        <span className="buttonSpinner" aria-hidden="true" /> Workflow running…
                      </>
                    ) : agentRuns.length > 0 ? (
                      'Start a new run'
                    ) : (
                      'Run scenario workflow'
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
            {webiqFailed && !isRunning ? (
              <button type="button" className="presetButton" onClick={() => {
                setWebiqKey('');
                setShowWebiqKey(false);
                runWorkflow(true);
              }}>Clear key and run without WebIQ</button>
            ) : null}
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
                    {toolbox.tools && toolbox.tools.length > 0 ? (
                      <span className="chipRow toolChipRow" aria-label={`${toolbox.name} tools`}>
                        {toolbox.tools.map((tool) => (
                          <span className="chip toolChip" key={tool}>
                            {formatToolName(tool)}
                          </span>
                        ))}
                      </span>
                    ) : null}
                  </article>
                ))
              ) : (
                <p className="mutedCopy">Toolbox details appear after the API responds.</p>
              )}
            </div>
          </aside>
        </div>

        <section className="timelineSection" aria-labelledby="timeline-title">
          <p role="status" aria-live="polite">{statusMessage}</p>
          {webiqNote ? <p className="webiqNotice">{webiqNote}</p> : null}
          {warnings.map((warning) => <div className="warningNotice" key={warning} role="status">{warning}</div>)}
          {sources.length > 0 ? (
            <details className="sourceList">
              <summary>WebIQ sources retrieved ({sources.length}) — untrusted enrichment</summary>
              <ul>{sources.map((source) => (
                <li key={source.id}>
                  <SourceCitation source={source} />
                </li>
              ))}</ul>
            </details>
          ) : null}
          <div className="sectionHeading compact">
            <div>
              <h2 id="timeline-title">Agent timeline</h2>
              <p>
                {runMeta
                  ? `${runMeta.title}: status updates appear while running; narrative output arrives when complete.`
                  : 'Run events will appear here as the scenario agent starts, streams, and completes.'}
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
                <p>Status updates appear while running. The narrative and artifacts arrive when the agent finishes.</p>
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
  return (
    <article className={`agentCard ${agentRun.status}`}>
      <header className="agentHeader">
        <div>
          <span className="rolePill">Scenario agent</span>
          <h3>{agentRun.label}</h3>
          <p>{formatAgentName(agentRun.agent)}</p>
        </div>
        <StatusIndicator status={agentRun.status} />
      </header>

      {agentRun.error ? <Alert tone="error" title="Agent error" message={agentRun.error} /> : null}

      {agentRun.activities.length > 0 ? (
        <ol className="activityFeed" aria-label={`${agentRun.label} activity`}>
          {agentRun.activities.map((activity, index) => {
            const isLast = index === agentRun.activities.length - 1;
            const live = agentRun.status === 'running' && isLast;
            return (
              <li className={`activityItem ${live ? 'live' : 'doneStep'}`} key={`${activity.kind}-${index}`}>
                <span className="activityDot" aria-hidden="true" />
                <span className="activityLabel">
                  {activityIcon(activity.kind)} {activity.label}
                  {activity.detail ? <code className="activityDetail">{activity.detail}</code> : null}
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}

      {agentRun.output.trim().length > 0 ? (
        <div className="agentOutput markdownBody" tabIndex={0} aria-label={`${agentRun.label} analysis`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
            a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
          }}>{agentRun.output}</ReactMarkdown>
        </div>
      ) : agentRun.status === 'running' ? (
        <div className="agentOutput agentOutputEmpty">
          {agentRun.activities.length > 0
            ? 'Working through the analysis…'
            : 'Starting the workflow…'}
          {typeof agentRun.elapsedS === 'number' ? ` (${agentRun.elapsedS}s)` : ''}
        </div>
      ) : (
        <div className="agentOutput agentOutputEmpty">No narrative returned.</div>
      )}

      {agentRun.artifacts.length > 0 ? (
        <div className="artifactRow" aria-label={`${agentRun.label} artifacts`}>
          {agentRun.artifacts.map((artifact) => (
            <ArtifactDownload artifact={artifact} key={artifact.id} />
          ))}
        </div>
      ) : null}
    </article>
  );
}

function ArtifactDownload({ artifact }: { artifact: Artifact }) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');

  async function download() {
    setDownloading(true);
    setError('');
    try {
      const blob = await fetchArtifact(API_BASE_URL, artifact.url);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = artifact.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      setError(getErrorMessage(reason, 'Download failed. Please retry.'));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="artifactDownload">
      <button className="artifactChip" type="button" disabled={downloading} onClick={download}>
        {downloading ? 'Downloading...' : artifact.kind === 'summary' ? 'Download summary only: ' : 'Download: '}
        {artifact.filename}
      </button>
      {error ? <p role="alert">{error} Click the download button to retry.</p> : null}
    </div>
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

function activityIcon(kind: string): string {
  switch (kind) {
    case 'function_call':
      return '📘';
    case 'mcp_call':
    case 'mcp_list_tools':
      return '🏛️';
    case 'web_search_call':
      return '🌐';
    case 'code_interpreter_call':
      return '🧮';
    case 'file_search_call':
      return '🗂️';
    default:
      return '⚙️';
  }
}

function formatAgentName(agentName: string): string {
  return agentName
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatSkillName(skill: string): string {
  return skill.replace(/[-_]+/g, ' ').replace(/\bxls\b/i, 'XLS');
}

const TOOL_LABELS: Record<string, string> = {
  code_interpreter: 'Code Interpreter',
  web_search: 'Web Search',
  sec_edgar_mcp: 'SEC EDGAR MCP',
  sec_edgar: 'SEC EDGAR MCP'
};

function formatToolName(tool: string): string {
  return (
    TOOL_LABELS[tool] ??
    tool.replace(/[-_]+/g, ' ').replace(/\bmcp\b/i, 'MCP').replace(/\b\w/g, (c) => c.toUpperCase())
  );
}
