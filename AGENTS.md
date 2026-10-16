# Coding agent instructions

Edu-Agent is a Microsoft Foundry hosted agent. See [README.md](README.md) for the project layout and [docs/design/architecture.md](docs/design/architecture.md) for layering rules.

## Commands

```bash
uv run --project agent --locked --group dev python -m unittest discover -s tests   # tests (fake models)
uv run --project agent --locked python agent/main.py                               # Responses API on :8088
python scripts/build_space.py                                                      # package the Hugging Face Space
azd deploy                                                                         # deploy to Foundry
```

## Rules

- `agent/learning_coach/server.py` is the only place that creates model clients, databases and adapters.
- `domain/` and `service/` must not import `api/`, `db/`, `adapters/` or `server.py` (enforced by `tests/test_architecture.py`).
- Prompts live in `agent/prompts/`; follow [docs/design/prompt-house-rules.md](docs/design/prompt-house-rules.md) when editing them.
- Never commit `agent/.env` or anything under `.azure/`.

## References

- [Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Microsoft Foundry Skill](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/use-microsoft-foundry-skill)
