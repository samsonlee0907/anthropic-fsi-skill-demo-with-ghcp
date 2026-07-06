import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
c = AIProjectClient(endpoint=os.environ["PROJECT_ENDPOINT"], credential=DefaultAzureCredential())
try:
    agents = list(c.agents.list())
    print("AUTH_OK agents_count=", len(agents))
    for a in agents[:10]:
        print(" -", getattr(a,"name",a))
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:400])
