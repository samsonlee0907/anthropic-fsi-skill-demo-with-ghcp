"""Server-side artifact egress for the v3 FSI hosted agents.

Why this exists
---------------
A deployed Foundry hosted agent produces its ``.xlsx`` / ``.pptx`` deliverables in the
code_interpreter sandbox (``/mnt/data``). Getting those bytes back to the portal is the
hard part of the hosted design:

* The ``ResponsesHostServer`` HTTP wrapper passes plain text through but **strips
  code_interpreter file citations / annotations**, so a caller talking to the deployed
  agent over ``/responses`` cannot recover the artifact file IDs.
* Shipping the file as **base64 in the response text** is unreliable: the model resists
  emitting large base64 and, when forced, Azure content-filters the blob and the SDK
  crashes parsing the filter verdict.

So each hosted agent harvests its code_interpreter output files **in-container** — where
the sandbox ``container_id`` is still present on the response object — uploads them to the
private ``artifacts`` blob container using its managed identity, and appends one sentinel
line per file to the response text::

    <<<ARTIFACT name=<filename> blob=<container>/<blobpath>>>>

The thin BFF parses these sentinels, streams the blob privately over
``/api/artifacts/...`` (no public access, no SAS), and strips the sentinel line before
showing the text to the user.

This is wired as an :class:`~agent_framework.AgentMiddleware`. It runs **inside**
``agent.run(stream=False)``, so it observes the full :class:`AgentResponse` (with the CI
``container_id`` intact) before the host converts it to client-facing output items. The
BFF must therefore invoke the deployed agent with ``stream=False``.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Final

from agent_framework import AgentMiddleware, Content
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger("fsi.hosted.v3.egress")

ARTIFACTS_CONTAINER: Final = os.environ.get("ARTIFACTS_CONTAINER", "artifacts")

# Content parts emitted by the native code_interpreter tool that carry a sandbox
# container id in their raw representation.
_CI_CONTENT_TYPES: Final = ("code_interpreter_tool_call", "code_interpreter_tool_result")


def _read_bytes(binary) -> bytes:
    """Extract raw bytes from an OpenAI ``containers.files.content.retrieve`` result."""
    if hasattr(binary, "content"):
        return binary.content
    if hasattr(binary, "read"):
        return binary.read()
    return bytes(binary)


def iter_container_ids(response) -> list[str]:
    """Every distinct code_interpreter sandbox container id referenced by ``response``.

    The container id is always present on the ``raw_representation`` of a
    ``code_interpreter_tool_call`` / ``_result`` content part when code actually ran —
    this is far more reliable than the file citation the model only emits when it *cites*
    the file in prose.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for message in getattr(response, "messages", None) or []:
        for content in getattr(message, "contents", None) or []:
            if getattr(content, "type", None) not in _CI_CONTENT_TYPES:
                continue
            raw = getattr(content, "raw_representation", None)
            cid = getattr(raw, "container_id", None)
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
    return ids


class ArtifactEgressMiddleware(AgentMiddleware):
    """Harvest code_interpreter files, upload to Blob, append download sentinels.

    Parameters
    ----------
    project_endpoint:
        Foundry project endpoint used to build the OpenAI client for the Containers
        files API (``containers.files.list`` / ``content.retrieve``).
    blob_endpoint:
        Storage account blob endpoint (e.g. ``https://stxzqm33pk.blob.core.windows.net``).
        When falsy, files are still harvested and named in the sentinel but not uploaded
        (useful for local wiring tests without Storage RBAC).
    credential:
        Azure credential (managed identity in the deployed container, ``az login``
        locally). A single :class:`DefaultAzureCredential` is created if omitted.
    """

    def __init__(
        self,
        *,
        project_endpoint: str,
        blob_endpoint: str | None,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        self._pe = project_endpoint
        self._blob_endpoint = blob_endpoint or None
        self._cred = credential or DefaultAzureCredential()
        self._oai = None
        self._blob = None

    def _oai_client(self):
        if self._oai is None:
            self._oai = AIProjectClient(
                endpoint=self._pe, credential=self._cred
            ).get_openai_client()
        return self._oai

    def _blob_service(self):
        if self._blob is None and self._blob_endpoint:
            # Imported lazily so the module still imports where azure-storage-blob
            # is absent (e.g. a caller that only wants iter_container_ids).
            from azure.storage.blob import BlobServiceClient

            self._blob = BlobServiceClient(
                account_url=self._blob_endpoint, credential=self._cred
            )
        return self._blob

    async def process(self, context, call_next):  # type: ignore[override]
        await call_next()
        # Egress only works on the non-streaming host path, where the full response
        # (with CI container ids) is materialised before client conversion.
        if getattr(context, "stream", False):
            return
        response = getattr(context, "result", None)
        if response is None:
            return
        try:
            sentinels = self._harvest_and_upload(response)
        except Exception as exc:  # never let egress break the agent's answer
            logger.warning("artifact egress failed: %s", exc)
            return
        messages = getattr(response, "messages", None)
        if sentinels and messages:
            messages[-1].contents.append(
                Content(type="text", text="\n" + "\n".join(sentinels))
            )

    def _harvest_and_upload(self, response) -> list[str]:
        oai = self._oai_client()
        blob = self._blob_service()
        run_prefix = uuid.uuid4().hex[:12]
        sentinels: list[str] = []
        seen: set[tuple[str, str]] = set()

        for cid in iter_container_ids(response):
            try:
                listing = oai.containers.files.list(container_id=cid)
            except Exception as exc:
                logger.warning("list container %s failed: %s", cid, exc)
                continue
            for f in getattr(listing, "data", None) or []:
                fid = getattr(f, "id", None)
                # Only files the assistant wrote (skip user-uploaded inputs).
                if not fid or getattr(f, "source", None) != "assistant":
                    continue
                if (cid, fid) in seen:
                    continue
                fname = Path(getattr(f, "path", None) or fid).name
                try:
                    data = _read_bytes(
                        oai.containers.files.content.retrieve(fid, container_id=cid)
                    )
                except Exception as exc:
                    logger.warning("retrieve file %s failed: %s", fid, exc)
                    continue
                seen.add((cid, fid))

                blobpath = f"{run_prefix}/{fname}"
                if blob is not None:
                    try:
                        blob.get_blob_client(
                            ARTIFACTS_CONTAINER, blobpath
                        ).upload_blob(data, overwrite=True)
                    except Exception as exc:
                        logger.warning("blob upload %s failed: %s", blobpath, exc)
                        continue
                    sentinels.append(
                        f"<<<ARTIFACT name={fname} blob={ARTIFACTS_CONTAINER}/{blobpath}>>>"
                    )
                    logger.info(
                        "published artifact %s -> %s/%s (%d bytes)",
                        fname,
                        ARTIFACTS_CONTAINER,
                        blobpath,
                        len(data),
                    )
                else:
                    # No storage configured: still surface the filename so the wiring
                    # can be validated without RBAC.
                    sentinels.append(f"<<<ARTIFACT name={fname} blob=>>>")
                    logger.info("harvested artifact %s (%d bytes, not uploaded)", fname, len(data))
        return sentinels
