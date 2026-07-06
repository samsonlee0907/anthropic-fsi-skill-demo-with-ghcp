"""Foundry client factory (managed identity in Container Apps, az login locally)."""
from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

from .config import PROJECT_ENDPOINT


@lru_cache(maxsize=1)
def get_project_client() -> AIProjectClient:
    return AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())


@lru_cache(maxsize=1)
def get_openai_client():
    return get_project_client().get_openai_client()
