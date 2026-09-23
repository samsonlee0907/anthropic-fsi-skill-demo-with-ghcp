type DeploymentMetadataProps = {
  environmentName?: string | null;
  modelDeploymentName?: string | null;
};

export function DeploymentMetadata({ environmentName, modelDeploymentName }: DeploymentMetadataProps) {
  return (
    <>
      <div>
        <dt>Environment</dt>
        <dd className="deploymentValue">{environmentName?.trim() || 'Not reported'}</dd>
      </div>
      <div>
        <dt>Model deployment</dt>
        <dd className="deploymentValue" title="Configured deployment name reported by the backend; not a model-readiness check.">
          {modelDeploymentName?.trim() || 'Not reported'}
        </dd>
      </div>
    </>
  );
}
