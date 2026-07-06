import inspect, azure.ai.projects.models as m
names = [n for n in dir(m) if not n.startswith("_")]
tools = [n for n in names if "Tool" in n]
agentdefs = [n for n in names if "Agent" in n or "Definition" in n]
print("TOOLS:", ", ".join(tools))
print("AGENTDEFS:", ", ".join(agentdefs))
conn = [n for n in names if "Connected" in n or "Reference" in n or "Handoff" in n]
print("CONNECTED/REF:", ", ".join(conn))
