"""Reasoning agents.

Each module here owns the LLM prompting and structured-output contract for one
node in `app/graph/nodes.py`. Agents decide *how* to use tools; the tools in
`app/tools/` do the deterministic API work. Populated from Milestone 2 onward.
"""
