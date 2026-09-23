export async function fetchArtifact(apiBase: string, path: string): Promise<Blob> {
  if (!/^\/api\/artifacts\/[A-Za-z0-9_-]+$/.test(path)) {
    throw new Error('This artifact has an invalid download address.');
  }
  const response = await fetch(`${apiBase}${path}`, { cache: 'no-store' });
  if (!response.ok) {
    if (response.status === 404) throw new Error('This artifact is no longer available.');
    if (response.status === 503) throw new Error('Artifact storage is temporarily unavailable. Please retry.');
    throw new Error(`Download failed (${response.status}). Please retry.`);
  }
  return response.blob();
}
