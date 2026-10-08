# Learning Coach — five-agent runtime

A Microsoft Foundry hosted learning coach built with Agent Framework. The existing project runs five real in-process agents: **coordinator, researcher, verifier, notes and visualizer**. The coordinator answers directly by default and requests a bounded tool path only when a capability is needed.

```text
learner → coordinator decide ── answer → validate → final four-field JSON
                    │
                    └─ tools → check reason + current-turn evidence
                                    ↓
                          selected research / quiz
                                    ↓
                     independent notes + visuals in parallel
                                    ↓
                        coordinator respond → validate
                                    ↓
                       commit → final four-field JSON
```

## What is implemented

- A normal direct answer completes the turn after one coordinator call, without a routine second `respond` call; format failures may still need the bounded retry below. Tool requests carry an enumerated reason and a quote from the current learner message; the program checks their consistency and quote origin, while semantic necessity still needs model evaluation. Existing program-managed quiz answers must go through grading.
- Real search adapters: Wikipedia by default; optional Tavily when `TAVILY_API_KEY` is configured. Citations and quotations must come from retrieved records. Search failures are explicit; evidence grades are not factual guarantees.
- Structured outputs and local Schema validation for every role, at most one format retry within the remaining budget, per-call/turn timeouts and a model-call budget. Native provider JSON mode is optional; runtime validation is always active.
- Session-scoped task and quiz progress, including pause/stop, exactly one question at a time, private answer keys and retry deduplication. Generated quiz answers still require teaching-quality evaluation.
- Recoverable quiz routing: an unambiguous answer to a pending question is graded even if a structurally valid `decide` chooses the wrong route. The program rebuilds a minimal continuation from saved quiz state, without another `decide` call, research or visuals; opt-in notes may record the trusted answer evidence. Pause/end/stop take priority, ambiguous answers are not forced, and real model failures still use the existing fallback.
- Opt-in SQLite learning notes with quote evidence and provenance. `/memory on` enables, `/memory off` pauses, `/forget` deletes this session's saved notes and visual artifacts. Uncalibrated mastery stays unknown; observations do not imply validated ability.
- Four declarative visuals rendered by application-owned HTML/JavaScript: comparisons, processes, flashcards and linear functions. The model cannot supply executable code. Artifacts expire after 24 hours.
- One validated public response: `schema_version`, `intent`, `message`, `next_step`. Only accepted final intent is logged; no `log_intent` tool. Internal agent outputs never stream to the learner.

`main.py` builds the pipeline and exposes it through `ResponsesHostServer`. The coordinator prompt is `instructions.md`; specialists use the existing `agents/` directory. `learning_coach/` owns routing, contracts, evidence validation, memory and rendering.

Research is not a prerequisite for drawing a visual. The verifier runs only after real source material is retrieved. Emotion, boundary and clarification turns, explicit stopping or pausing, and disabled learning memory prevent the corresponding work from starting. Optional research, notes and visual capabilities reach a result or explicit failure state before the final reply; quiz or core failures use the fixed fallback. Independent notes and visual generation are awaited together, not detached background writes. Missing evidence does not trigger an unbounded attempt to run every role.

Protocol transcripts use an in-memory store. Automatic cloud telemetry and message capture are disabled; application diagnostics omit message contents. The repository has a local chat prototype in `web/`, unchanged by this routing update; phase progress, word-by-word streaming and production UI acceptance remain unfinished. This is a **single-process local implementation**: authenticated accounts, cross-session learner identity, distributed session recovery and deployment are not included. Foundry gateway access to custom artifact URLs requires deployment validation.

Each model call records `agent_stage` with role, phase, attempt, outcome and elapsed milliseconds. These logs exclude message contents and describe individual model calls, not full-turn latency or search-network time.

## Run from the repository root

Use the existing Foundry endpoint, model deployment and credentials documented in the source `.env.example`. Run the same pipeline interactively:

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python src/agent-framework-agent-with-local-tools-responses/demo.py
```

Start the Responses host and artifact route in a separate terminal when needed:

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python src/agent-framework-agent-with-local-tools-responses/main.py
```

Local state defaults to the source directory's `.learning-coach/`. Set `LEARNING_COACH_DATA_DIR` for another local directory, and `LEARNING_COACH_PUBLIC_BASE_URL` for the actual artifact-serving URL. Set `LEARNING_COACH_NATIVE_JSON=1` only after checking the deployed model's schema support. No new cloud resources are created by these changes.

## Tests and acceptance

`jsonschema==4.26.0` is explicitly pinned for runtime validation and in the `dev` group. From the repository root:

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python -m unittest discover -s tests -v
```

The suite exercises real SDK interfaces with fake model/search responses, including the single-call direct-answer path, selective tool routing, storage, evidence, quiz replay and rendering. It does not call a live model, establish teaching accuracy or measure production latency. The host still sends one complete validated result; this change does not implement SSE progress messages or word-by-word answers. Validate every saved real final response separately:

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python scripts/validate_responses.py final-responses.jsonl --format raw-jsonl --failures failures.jsonl
```

See [implementation and limits](docs/multi-agent.md), [architecture](docs/edu-agent-architecture.md) and [behavioral acceptance](docs/acceptance-v1.5.md). Earlier Prompt drafts and runtime reviews are historical records, not the current capability specification.

## Option 1: Azure Developer CLI (`azd`)

### Prerequisites

1. **Azure Developer CLI (`azd`)** — [Install azd](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd)
2. Install the AI agent extension:
   ```bash
   azd ext install microsoft.foundry
   ```
3. Authenticate:
   ```bash
   azd auth login
   ```

### Initialize the agent project

The following initialization command creates the upstream tools sample. If you already have this education repository, skip initialization and use its existing project; the upstream manifest does not include these v1.5 education changes. To start from the upstream sample in a new folder:

```bash
mkdir my-tools-agent && cd my-tools-agent

azd ai agent init -m https://github.com/microsoft-foundry/foundry-samples/blob/main/samples/python/hosted-agents/agent-framework/responses/02-tools/azure.yaml
```

Follow the prompts to configure your Foundry project and model deployment. If you don't have an existing Foundry project, `azd ai agent init` will guide you through creating one.

### Provision Azure resources (if needed)

If you don't already have a Foundry project and model deployment:

```bash
azd provision
```

### Run the agent locally

```bash
azd ai agent run
```

The agent host will start on `http://localhost:8088`.

### Invoke the local agent

In a separate terminal, from the project directory:

```bash
azd ai agent invoke --local "请围绕一元一次方程考考我，一次只出一道题。"
```

### Deploy to Foundry

Once tested locally, deploy to Microsoft Foundry:

```bash
azd deploy
```

For the full deployment guide, see [Deploy a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent).

### Invoke the deployed agent

```bash
azd ai agent invoke "请围绕一元一次方程考考我，一次只出一道题。"
```

## Option 2: VS Code (Foundry Toolkit)

### Prerequisites

1. **VS Code** with the **[Foundry Toolkit](https://marketplace.visualstudio.com/items?itemName=ms-windows-ai-studio.windows-ai-studio)** extension installed.
2. For debugging Python in VS Code, install the **[Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)** extension pack.

### Set up the Python virtual environment

- With Python 3.13 or later and [pipx](https://pipx.pypa.io/stable/installation/), install uv outside the project environment, then let uv create and synchronize the locked environment:

  ```bash
   pipx install uv==0.11.7
   uv sync --project src/agent-framework-agent-with-local-tools-responses --locked --group dev --python 3.13
  ```
- Open the Command Palette (`Ctrl+Shift+P`), run **Python: Select Interpreter**, and select `src/agent-framework-agent-with-local-tools-responses/.venv` created by uv. Run the commands above from the repository root.

### Run and debug the agent

Press **F5** to start the agent. The agent starts and the **Agent Inspector** opens automatically. Chat with the agent in the Inspector. During protocol acceptance, inspect the raw final JSON. The separate `web/` local chat prototype reads the completed response and renders its visible fields; it is unchanged by this update and still requires production acceptance.

### Or run manually, then open the Inspector

1. Set the required environment variables and sign in to Azure with the Azure CLI (`az login`).
2. Start the agent: `uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python src/agent-framework-agent-with-local-tools-responses/main.py` (listens on `http://localhost:8088`).
3. Command Palette (`Ctrl+Shift+P`) → **Foundry Toolkit: Open Agent Inspector**, then send a message to test.

### Deploy to Foundry

1. Open the Command Palette (`Ctrl+Shift+P`) and run **Foundry Toolkit: Deploy Hosted Agent**. The extension opens a **Deploy Hosted Agent** wizard and reads `agent.yaml` to auto-populate settings.
2. If prompted, complete **Foundry Project Setup** to select subscription and project.
3. On the **Basics** tab, choose deployment method (**Code** or **Container**) and confirm the agent name.
4. On **Review + Deploy**, confirm runtime details, pick **CPU and Memory** size, and click **Deploy**.
5. After deployment, invoke the agent in the Agent Playground and stream live logs from the **Logs** tab.

## Next steps

- [Quickstart: Create a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/quickstarts/quickstart-hosted-agent) — end-to-end walkthrough using `azd`
- [Tool catalog](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/tool-catalog) — browse available tools to extend your agent (Bing Search, Azure AI Search, file search, code interpreter, and more)
- [Manage hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent) — monitor and manage deployed agents
- [Basic agent](../01-basic/) — minimal agent with no tools
- [Use Foundry Toolbox](../04-foundry-toolbox/) — sample with Azure Foundry Toolbox integration
