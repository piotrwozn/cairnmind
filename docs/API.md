# Cairnmind 0.9.0: API, Ollama and Copilot connections

`cairnmind` opens the project's terminal workspace. A named connection defines the client, the model and the limits, and a project assistant uses one of those connections. You can assign several assistants to a project and explicitly start the one you want. Cairnmind's own agent handles direct APIs; Copilot has a separate adapter built on the official Python SDK. The interface is in English by default; **More → Settings → Language → Polski** switches it to Polish.

## Available connections

| Profile | Protocol / sign-in | Available models |
|---|---|---|
| Codex CLI | Native client with its own ChatGPT sign-in or key configuration | Models supported by the installed Codex |
| Claude Code | Native client with its own sign-in / configuration | Models supported by the installed Claude Code |
| OpenAI API | Responses API, separate API key | Provider's model list or an explicit model ID |
| Anthropic API | Messages API, separate API key | Provider's model list or an explicit model ID |
| OpenAI-compatible API | Chat Completions API with a custom base URL | Server's model list or an explicit model ID |
| Ollama | Local `/api/chat` and `/api/tags` | Models installed on the selected Ollama instance |
| GitHub Copilot | Official SDK and a Copilot account / entitlement | Models available to that account |

A ChatGPT subscription, a Claude subscription, GitHub Copilot access and direct API access each have separate sign-ins and billing. Choosing a Claude model in Copilot uses the Copilot connection and the Copilot quota. An OpenAI API profile does not automatically inherit ChatGPT subscription entitlements. GitHub Models is a different service from the Copilot adapter.

Cairnmind's own coding agent requires a model that supports tool calling. An embedding model is not such an agent. A missing model or missing tool support is reported explicitly; Cairnmind never switches providers in the background.

## Setting up a connection in the terminal

1. Run `cairnmind`, open **Connections** and choose **Add connection**. The menu is also available via `Ctrl+B`, then `Space`.
2. Pick the connection type, give it a name and enter the model. Enter a custom base URL for an OpenAI-compatible API. Limits, USD rates and key references live under **Advanced limits and credentials**. Save forms with `Ctrl+S`; a validation error does not discard the values you entered.
3. In the named connection's menu, choose **Connect account**, **Connect GitHub account** or **Set API key**, as appropriate for the provider. Codex and Claude Code keep their own sign-in procedures. **Check connection** shows the locally available state; for Copilot it does not confirm a working account session.
4. **Choose model** can fetch metadata from the provider and save the selected model. It does not start a conversation or edit the project.
5. **Test model and tools** is a separate request that you confirm explicitly. It may consume paid tokens or account quota; the test does not edit the project.
6. In the project list, `Enter` opens the project preview. On the project screen, choose an assigned assistant or **Add assistant**, then **Start work**. Only the selected assistant starts. **Start work** in the global menu first shows the project screen for confirmation.

Opening the terminal UI, selecting a profile and restoring a view do not generate any model response. A running API panel restores the local history; the model starts working only after you send a message. A profile change takes effect on the next start; an existing process keeps its configuration.

When the service is started from the terminal UI, memory tasks that need a local model wait for **Start work** or **More → Queue → Process memory**. This action enables the queue for the selected project only. It does not start the queues of other projects. **Process memory** also lets you resume memory processing on its own after a connection error, without restarting the assistant. Services and sessions that were already running continue working.

The default base URLs are `https://api.openai.com/v1`, `https://api.anthropic.com/v1` and `http://127.0.0.1:11434` for Ollama. A custom URL must not contain a password, a key or query parameters. Remote connections require HTTPS; plain HTTP is allowed only for loopback/localhost addresses. Do not disable TLS verification to work around a corporate certificate problem.

### Keys and environment variables

**Set API key** opens an empty, masked field. A saved key is never displayed again. By default the key is kept in the memory of the current broker process and stops being available when that process exits. Simply detaching the terminal does not stop the broker.

The **Remember on this computer** option is off by default. It stores the key in the private file `credentials/api-keys.json` in Cairnmind's local data directory. The directory has `0700` permissions and the file `0600`; **the file is not encrypted**. It is never included in the knowledge base, the administrative export or a project snapshot. Removing a locally stored key is a separate action in the connection menu.

Instead of storing a key, you can enter a reference of the form `env:VARIABLE_NAME` in the connection's advanced settings, for example `env:OPENAI_API_KEY`. The variable must be visible to the broker process that runs the workspace. Changing the variable in a new shell does not update a broker that is already running. Keys are not passed to system commands executed by the agent.

Full conversations and command output may contain private data. Redaction of known credentials is not a promise that every secret typed into project content or a conversation will be removed automatically. Check the scope of a snapshot before sharing memory.

### Ollama

Choose **Ollama — local models** and enter the address of a running local service. The key field can stay empty. The model list comes from that Ollama instance. Cairnmind does not download a missing model automatically.

An Ollama profile has an explicit **Ollama context tokens** field in its advanced settings: **32768 tokens** by default, range **4096–262144**. The value is sent to Ollama; Cairnmind does not guess the model's actual context window. A larger value increases RAM/VRAM usage. A context that is too large is rejected before generation instead of truncating the memory package; adjust the profile or reduce the scope of the work.

The coding model in the profile is independent of the local knowledge agent and embedding model configured in Cairnmind. A shared GPU has limited memory; measure the actual latency and capabilities of the chosen model locally. Local model APIs have no per-token provider fee, but the interface does not turn missing pricing data into a fictitious cost estimate.

### GitHub Copilot

In **Connections**, choose a named **GitHub Copilot** connection:

- **Connect GitHub account** opens an operational sign-in terminal. If the runtime is missing, it first offers to prepare it, with a separate confirmation for the download. Device codes, sign-in URLs and the output of this terminal never become project memory sources.
- **Prepare Copilot runtime** lets you separately download the pinned official runtime, verify its checksums and persist a local integrity confirmation. Opening Cairnmind on its own does not download the runtime.
- **Choose model** shows the models granted to the account. Organization entitlements and administrator policies may restrict Copilot usage.

The adapter uses the official Python SDK `github-copilot-sdk`, pinned to a compatible version in the lockfile. Cairnmind controls the tool set and the approvals. Tools with unconfirmed permissions are never approved automatically. Usage information is shown only when the SDK reports it; it is not an independent reading of the account balance.

Signing in with an account does not require your own API key. The Cairnmind runtime has a separate credentials directory and may ask for consent to store them locally without encryption; the decision is yours. Version 0.7.2 fixed how this consent is passed to the runtime. Inherited `COPILOT_PROVIDER_*` and `COPILOT_OFFLINE` variables are removed so that they cannot switch this profile to a different API provider. File permissions and the runtime's SHA-256 checksums are checked again even after an earlier successful verification.

## Agent work and permissions

The API/Copilot panel shows the history, the streamed response, tool calls and their results, and the memory state. **Send** or `Ctrl+Enter` starts a turn. `Ctrl+C` or **Interrupt** stops the current operation. The `Ctrl+B` workspace prefix and the panel shortcuts still work; press `Ctrl+B` twice to pass a native `Ctrl+B` through.

The agent can browse, create, edit and delete files in the selected worktree. File tools validate paths and links, and changing an existing file requires a match with the hash of the version that was read. Direct writes to `.git/` and to the managed `.memory/devbrain/` directory are blocked. If a second client changes a file, the agent must re-read it instead of overwriting the newer work.

Every system command, including running tests, requires **one-time approval**. The approval screen shows the exact command, the working directory, the session and the generation. The approval applies only to that specific operation; changing any parameter requires a new approval. The command runs with the user's permissions; this mechanism is not system-level sandboxing. Native Codex and Claude Code processes keep their own tool policies.

The default limits of Cairnmind's own engine are 20 model requests per turn, 4096 output tokens per request and 120 seconds per command. A Copilot profile defaults to 20 tool calls and 900 seconds per turn. The forms let you change the supported limits. The number of Copilot's internal requests is not claimed to be known when the SDK does not expose it.

Reaching a limit stops the turn; you continue by sending another message. Token counts come from the provider's report. Cost is an estimate based on the profile's optional rates; without rates or usage data it is shown as **No data**. It is not a guaranteed cap on the amount charged by the provider.

## Memory and recovery after interruption

Cairnmind's local data directory must be located outside project directories, for example in `~/.local/share/cairnmind`. A new agent refuses to start when its store is inside the selected project; saving a persistent key also checks the other registered directories. This keeps configuration files and local credentials from becoming the agent's working files.

Every engine uses the shared project memory, with an actual `resume_context` read and an exact ACK from the model. Delivery of context by the UI or the SDK is not an ACK. A partial package, a mismatched readback or a change in relevant state blocks writes and any actions that require restored context.

The `ack_resume` schema requires a `readback` with `plan` and `task` fields: the exact `id` and `version` of the corresponding active item in the package, or `null` when there is no such item. Version 0.7.2 gives this shape directly to API and Copilot models; the acknowledgement is still verified by the actual memory server.

Messages, complete tool calls and their results are persisted in a private, integrity-checked journal. The start of an action is recorded before it executes, and its result before the model's next step. Conversations and evidence are archived as Cairnmind sources and are available through the memory inspector and in portable memory.

`Ctrl+B d` detaches the interface and keeps the process running. Shutting down WSL terminates the processes. A later **Sessions → Resume saved session** restores memory and requires a new ACK handshake. The journal does not grant earlier command approvals again.

An action that was started without a confirmed result is in an uncertain state. **Resolve** (`Ctrl+R`) lists such actions and requires a note after you check for yourself, for example whether a file was created or a test finished. `Ctrl+S` saves the note; a save error keeps the text you typed. Saving the note does not re-run the command. Only a separate message continues the work. Do not repeat a paid request or a command just because the connection dropped; the previous operation may have completed.

When compacting context, the engine keeps the sources and a checkpoint, and builds the new context from the current memory and the required sources. Memory does not remove context limits or model costs. A snapshot carries saved conversations, decisions and evidence; it does not carry keys, connection profiles, active processes or approvals. Imported test results remain historical.

## Post-installation check

First restore a known project and check the existing Codex/Claude panels. Then configure the new profile you want, check its state, run an explicit model test and a short turn with a file read and a rejected command. Verify that a second client restores a saved decision together with its source. Detach and reopen `cairnmind`.

Automated tests use controlled HTTP/SDK responses, a real ArcadeDB and tmux. They do not consume paid quota. A real connection, models on a specific GPU and Copilot/OpenAI/Anthropic accounts require a separate, explicit check by the user. A missing account, model or available quota is a **not_run** result, not a passed test.
